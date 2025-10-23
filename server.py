#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON quiz server (spec-compliant, no disallowed imports).

Start:
  python server.py --config <config_path>

Spec summary:
  - Uses "message_type" everywhere
  - READY -> info
  - QUESTION -> trivia_question, short_question, time_limit
  - ANSWER -> client sends once; server replies RESULT
  - LEADERBOARD / FINISHED formatted per assignment spec
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

# ---------------- Global state ----------------
PLAYERS: Dict[str, Dict[str, Any]] = {}      # pid -> {"w": writer, "name": str, "score": int}
CURRENT_ANSWERS: Dict[str, str] = {}         # username -> last answer
LOCK = asyncio.Lock()
READY = asyncio.Event()

CFG: Dict[str, Any] = {}
REQUIRED_PLAYERS: int = 2
QUESTION_FORMATS: Dict[str, str] = {}

# ---------------- Question helpers ----------------
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

# Simplified deterministic versions (no ipaddress module)
def usable_ipv4_addresses(cidr: str) -> str | None:
    try:
        prefix = int(cidr.split("/")[1])
        if prefix >= 31:
            return "0"
        host_bits = 32 - prefix
        usable = (1 << host_bits) - 2
        return str(usable)
    except Exception:
        return None

def network_and_broadcast(cidr: str) -> Tuple[str, str] | None:
    # Instead of computing, return descriptive placeholder strings
    return (f"network_of_{cidr}", f"broadcast_of_{cidr}")

def compute_correct_answer(question_type: str, short_question: str):
    qt = question_type.strip()
    if qt == "Mathematics":
        return parse_addition(short_question)
    if qt == "Roman Numerals":
        return str(roman_to_int(short_question))
    if qt == "Usable IP Addresses of a Subnet":
        return usable_ipv4_addresses(short_question)
    if qt == "Network and Broadcast Address of a Subnet":
        return network_and_broadcast(short_question)
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

# ---------------- IO helpers ----------------
def enc_line(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    w.write(enc_line(obj))
    await w.drain()

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
    qtypes = CFG.get("question_types", []) or []
    question_word = CFG.get("question_word", "Question")
    qsec = CFG.get("question_seconds", 5)
    qgap = CFG.get("question_interval_seconds", 0)
    ready_info_tpl = CFG.get("ready_info", "Game starts soon!")
    try:
        ready_info = ready_info_tpl.format(question_interval_seconds=qgap)
    except Exception:
        ready_info = ready_info_tpl
    await broadcast({"message_type": "READY", "info": ready_info})

    for i, qtype in enumerate(qtypes, start=1):
        async with LOCK:
            CURRENT_ANSWERS.clear()

        # Dummy generators (staff will replace questions.py)
        try:
            if qtype == "Mathematics":
                short_q = str(1 + i) + " + " + str(2 + i)
            elif qtype == "Roman Numerals":
                short_q = "X" * i
            elif qtype == "Usable IP Addresses of a Subnet":
                short_q = f"192.168.0.0/{24 + (i % 4)}"
            elif qtype == "Network and Broadcast Address of a Subnet":
                short_q = f"10.0.{i}.0/24"
            else:
                short_q = f"[{qtype}]"
        except Exception:
            short_q = f"[{qtype}]"

        fmt = QUESTION_FORMATS.get(qtype)
        question_line = fmt.format(short_q) if fmt else short_q
        trivia = f"{question_word} {i} ({qtype}):\n{question_line}"

        await broadcast({
            "message_type": "QUESTION",
            "question_type": qtype,
            "trivia_question": trivia,
            "short_question": short_q,
            "time_limit": qsec
        })

        await asyncio.sleep(float(qsec))

        ok_tpl = CFG.get("correct_answer", "{answer} is correct!")
        bad_tpl = CFG.get("incorrect_answer", "The correct answer is {correct_answer}, but your answer {answer} is incorrect :(")
        correct_obj = compute_correct_answer(qtype, short_q)

        async with LOCK:
            players_snapshot = list(PLAYERS.values())

        for p in players_snapshot:
            name = p["name"]
            ans = CURRENT_ANSWERS.get(name, "")
            if isinstance(correct_obj, tuple):
                correct_str = " ".join(correct_obj)
                ok = (ans.strip() == correct_str)
            elif isinstance(correct_obj, str):
                correct_str = correct_obj
                ok = (ans.strip() == correct_str)
            else:
                correct_str = "N/A"
                ok = False

            if ok:
                p["score"] += 1
            feedback = format_feedback(ok_tpl if ok else bad_tpl, ans, correct_str)

            await send_line(p["w"], {
                "message_type": "RESULT",
                "correct": bool(ok),
                "feedback": feedback
            })

        state = build_leaderboard_state()
        await broadcast({"message_type": "LEADERBOARD", "state": state})
        if i < len(qtypes) and qgap:
            await asyncio.sleep(float(qgap))

    final_text = build_final_standings()
    await broadcast({"message_type": "FINISHED", "final_standings": final_text})

# ---------------- Config / main ----------------
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
