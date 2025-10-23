#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON quiz server with spec-compliant messaging.

Start:
  python server.py --config <config_path>

Spec-compliant behaviors:
- All messages use "message_type"
- READY -> prints 'info' on clients
- QUESTION -> server constructs 'trivia_question' and includes 'short_question' and 'time_limit'
- ANSWER -> client sends once; server scores, increments points if correct, and replies RESULT
- LEADERBOARD -> server sends state string with correct ranking rules and pluralization
- FINISHED -> server sends final_standings including winners line(s)

Fatal errors (stderr + exit(1)):
- Missing --config or path: "server.py: Configuration not provided"
- Config file missing: "server.py: File <path> does not exist"
- Bind failure: "server.py: Binding to port <port> was unsuccessful"
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
import ipaddress

# Global state
PLAYERS: Dict[str, Dict[str, Any]] = {}      # pid -> {"w": writer, "name": str, "score": int}
CURRENT_ANSWERS: Dict[str, str] = {}         # username -> last answer
LOCK = asyncio.Lock()
READY = asyncio.Event()

# Config
CFG: Dict[str, Any] = {}
REQUIRED_PLAYERS: int = 2
QUESTION_FORMATS: Dict[str, str] = {}

# Import questions module (staff will replace this file in tests)
import questions as qmod


# ---------------- IO helpers ----------------
def enc_line(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    w.write(enc_line(obj))
    await w.drain()


# ---------------- Scoring helpers ----------------
ROMAN_MAP = {
    "M": 1000, "CM": 900, "D": 500, "CD": 400,
    "C": 100, "XC": 90, "L": 50, "XL": 40,
    "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
}

def roman_to_int(s: str) -> int:
    i = 0
    n = 0
    s = s.strip().upper()
    while i < len(s):
        if i + 1 < len(s) and s[i:i+2] in ROMAN_MAP:
            n += ROMAN_MAP[s[i:i+2]]
            i += 2
        else:
            n += ROMAN_MAP.get(s[i], 0)
            i += 1
    return n

def parse_addition(short_q: str) -> str | None:
    if "+" not in short_q:
        return None
    try:
        a, b = short_q.split("+", 1)
        return str(int(a.strip()) + int(b.strip()))
    except Exception:
        return None

def usable_ipv4_addresses(cidr: str) -> str | None:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        if isinstance(net, ipaddress.IPv6Network):
            return None
        host_bits = 32 - net.prefixlen
        if net.prefixlen >= 31:
            return "0"
        usable = (1 << host_bits) - 2
        return str(usable)
    except Exception:
        return None

def network_and_broadcast(cidr: str) -> Tuple[str, str] | None:
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        if isinstance(net, ipaddress.IPv6Network):
            return None
        return (str(net.network_address), str(net.broadcast_address))
    except Exception:
        return None

def compute_correct_answer(question_type: str, short_question: str) -> str | Tuple[str, str] | None:
    qt = question_type.strip()
    if qt == "Mathematics":
        return parse_addition(short_question)
    if qt == "Roman Numerals":
        try:
            return str(roman_to_int(short_question))
        except Exception:
            return None
    if qt == "Usable IP Addresses of a Subnet":
        return usable_ipv4_addresses(short_question)
    if qt == "Network and Broadcast Address of a Subnet":
        pair = network_and_broadcast(short_question)
        return pair
    return None

def format_feedback(template: str, answer: str, correct_answer: str) -> str:
    try:
        return template.format(answer=answer, correct_answer=correct_answer)
    except Exception:
        return template


# ---------------- Leaderboard / standings ----------------
def pluralize_points(n: int) -> str:
    singular = CFG.get("points_noun_singular", "point")
    plural = CFG.get("points_noun_plural", "points")
    return singular if n == 1 else plural

def sorted_players() -> List[Tuple[str, int]]:
    # sort by score desc, then name asc (lexicographically)
    items = [(p["name"], p["score"]) for p in PLAYERS.values()]
    items.sort(key=lambda t: (-t[1], t[0]))
    return items

def build_leaderboard_state() -> str:
    items = sorted_players()
    lines: List[str] = []
    rank = 0
    prev_score = None
    count = 0
    for name, score in items:
        count += 1
        if prev_score is None or score != prev_score:
            rank = count
            prev_score = score
        lines.append(f"{rank}. {name}: {score} {pluralize_points(score)}")
    return "\n".join(lines)

def build_final_standings() -> str:
    heading = CFG.get("final_standings_heading", "Final standings:")
    one_winner_tpl = CFG.get("one_winner", "{} is the sole victor!")
    multiple_winners_tpl = CFG.get("multiple_winners", "Say congratulations to {}!")

    items = sorted_players()
    lines = [heading]

    rank = 0
    prev_score = None
    count = 0
    top_score = items[0][1] if items else 0
    winners: List[str] = []
    for name, score in items:
        count += 1
        if prev_score is None or score != prev_score:
            rank = count
            prev_score = score
        lines.append(f"{rank}. {name}: {score} {pluralize_points(score)}")
        if score == top_score:
            winners.append(name)

    winners.sort()
    if len(winners) == 1:
        lines.append(one_winner_tpl.format(winners[0]))
    elif len(winners) > 1:
        lines.append(multiple_winners_tpl.format(", ".join(winners)))
    else:
        lines.append(one_winner_tpl.format("N/A"))

    return "\n".join(lines)


# ---------------- Client handling ----------------
async def handle_client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    addr = w.get_extra_info("peername")
    pid = f"{addr[0]}:{addr[1]}" if addr else "unknown"
    try:
        while True:
            line = await r.readline()
            if not line:
                break
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue
            mtype = str(msg.get("message_type", "")).upper()

            if mtype == "HI":
                username = msg.get("username", pid)
                async with LOCK:
                    PLAYERS[pid] = {"w": w, "name": username, "score": 0}
                    if len(PLAYERS) >= REQUIRED_PLAYERS:
                        READY.set()

            elif mtype == "ANSWER":
                username = PLAYERS.get(pid, {}).get("name", pid)
                ans = str(msg.get("answer", ""))
                async with LOCK:
                    CURRENT_ANSWERS[username] = ans

            elif mtype == "BYE":
                break
    except Exception:
        pass
    finally:
        async with LOCK:
            if pid in PLAYERS:
                del PLAYERS[pid]
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass


async def broadcast(msg: Dict[str, Any]) -> None:
    async with LOCK:
        targets = list(PLAYERS.values())
    for p in targets:
        try:
            await send_line(p["w"], msg)
        except Exception:
            pass


# ---------------- Coordinator ----------------
async def coordinator() -> None:
    await READY.wait()

    question_types = CFG.get("question_types", []) or []
    question_word = CFG.get("question_word", "Question")
    qsec = CFG.get("question_seconds", 5)
    qgap = CFG.get("question_interval_seconds", 0)
    total_rounds = len(question_types)

    ready_info_tpl = str(CFG.get("ready_info", "Game starts soon!"))
    try:
        ready_info = ready_info_tpl.format(question_interval_seconds=qgap)
    except Exception:
        ready_info = ready_info_tpl

    await broadcast({"message_type": "READY", "info": ready_info})

    for i, qtype in enumerate(question_types, start=1):
        async with LOCK:
            CURRENT_ANSWERS.clear()

        # short_question from questions.py (staff version will be used in grading)
        try:
            if qtype == "Mathematics":
                short_q = str(qmod.generate_mathematics_question())
            elif qtype == "Roman Numerals":
                short_q = str(qmod.generate_roman_numerals_question())
            elif qtype == "Usable IP Addresses of a Subnet":
                short_q = str(qmod.generate_usable_addresses_question())
            elif qtype == "Network and Broadcast Address of a Subnet":
                short_q = str(qmod.generate_network_broadcast_question())
            else:
                short_q = f"[{qtype}]"
        except Exception:
            short_q = f"[{qtype}]"

        fmt = QUESTION_FORMATS.get(qtype)
        if fmt:
            try:
                question_line = fmt.format(short_q)
            except Exception:
                question_line = short_q
        else:
            question_line = short_q

        trivia_question = f"{question_word} {i} ({qtype}):\n{question_line}"

        await broadcast({
            "message_type": "QUESTION",
            "question_type": qtype,
            "trivia_question": trivia_question,
            "short_question": short_q,
            "time_limit": qsec
        })

        try:
            await asyncio.sleep(float(qsec))
        except Exception:
            await asyncio.sleep(0)

        ok_tpl = str(CFG.get("correct_answer", "{answer} is the correct answer!"))
        bad_tpl = str(CFG.get("incorrect_answer", "The correct answer is {correct_answer}, but your answer {answer} is incorrect :("))

        correct_obj = compute_correct_answer(qtype, short_q)

        async with LOCK:
            players_snapshot = list(PLAYERS.values())

        for p in players_snapshot:
            name = p["name"]
            ans = CURRENT_ANSWERS.get(name, "")

            if isinstance(correct_obj, tuple):
                correct_answer_str = " ".join(correct_obj)
                ok = (ans.strip() == correct_answer_str)
            elif isinstance(correct_obj, str):
                correct_answer_str = correct_obj
                ok = (ans.strip() == correct_answer_str)
            else:
                correct_answer_str = "N/A"
                ok = False

            if ok:
                p["score"] += 1

            feedback = format_feedback(ok_tpl if ok else bad_tpl, ans, correct_answer_str)

            try:
                await send_line(p["w"], {
                    "message_type": "RESULT",
                    "correct": bool(ok),
                    "feedback": feedback
                })
            except Exception:
                pass

        state = build_leaderboard_state()
        await broadcast({"message_type": "LEADERBOARD", "state": state})

        if i < total_rounds and qgap:
            try:
                await asyncio.sleep(float(qgap))
            except Exception:
                await asyncio.sleep(0)

    final_text = build_final_standings()
    await broadcast({"message_type": "FINISHED", "final_standings": final_text})


# ---------------- Config and main ----------------
def load_server_config(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "--config":
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)
    if len(args) < 2:
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"server.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    global CFG, REQUIRED_PLAYERS, QUESTION_FORMATS
    CFG = load_server_config(cfg_path)
    REQUIRED_PLAYERS = int(CFG.get("players", 2))
    QUESTION_FORMATS = CFG.get("question_formats", {}) or {}

    port = int(CFG.get("port", 5050))
    try:
        srv = await asyncio.start_server(handle_client, "127.0.0.1", port)
    except OSError:
        print(f"server.py: Binding to port {port} was unsuccessful", file=sys.stderr)
        sys.exit(1)

    print(f"[server] listening on 127.0.0.1:{port}")
    asyncio.create_task(coordinator())
    async with srv:
        await srv.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
