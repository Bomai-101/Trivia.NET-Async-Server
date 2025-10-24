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

# import questions.py exactly as required
import questions as qmod

# ---------------- Global state ----------------
PLAYERS: Dict[str, Dict[str, Any]] = {}      # pid -> {"w": writer, "name": str, "score": int}
CURRENT_ANSWERS: Dict[str, str] = {}         # username -> last answer
LOCK = asyncio.Lock()
READY = asyncio.Event()

CFG: Dict[str, Any] = {}
REQUIRED_PLAYERS: int = 2
QUESTION_FORMATS: Dict[str, str] = {}

# -------------------------------------------------------------------
# Answer-evaluation helpers (mirrored from the client's auto solver)
# -------------------------------------------------------------------

def _eval_plus_minus(expr: str) -> str | None:
    """
    Safely evaluate expressions like:
    "12 + 3 - 4 + 5"
    Returns the integer result as string, or None on failure.
    Supports only + and - with spaces.
    """
    tokens = expr.split()
    if not tokens:
        return None
    try:
        total = int(tokens[0])
    except Exception:
        return None
    i = 1
    while i < len(tokens) - 1:
        op = tokens[i]
        try:
            val = int(tokens[i + 1])
        except Exception:
            return None
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        else:
            return None
        i += 2
    return str(total)

def _roman_to_int(s: str) -> int:
    """
    Convert a Roman numeral string like 'XLV' to an integer (45).
    Supports subtractive pairs (IV, IX, etc.).
    """
    ROMAN_MAP = {
        "M": 1000, "CM": 900, "D": 500, "CD": 400,
        "C": 100, "XC": 90, "L": 50, "XL": 40,
        "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
    }
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

def _usable_ipv4_addresses(cidr: str) -> str | None:
    """
    Given e.g. '192.168.1.0/24', return number of usable hosts in that subnet.
    Formula for normal subnets: (2^(32-prefix) - 2), except /31 and /32 → 0 usable.
    """
    try:
        prefix = int(cidr.split("/")[1])
    except Exception:
        return None
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    usable = (1 << host_bits) - 2
    return str(usable)

def _ip_to_int(a: int, b: int, c: int, d: int) -> int:
    """
    Pack 4 octets into a 32-bit integer.
    """
    return ((a << 24) |
            (b << 16) |
            (c << 8)  |
            d)

def _int_to_ip(n: int) -> str:
    """
    Convert 32-bit integer back to dotted IPv4 string.
    """
    a = (n >> 24) & 255
    b = (n >> 16) & 255
    c = (n >> 8) & 255
    d = n & 255
    return f"{a}.{b}.{c}.{d}"

def _network_and_broadcast_pair(cidr: str) -> Tuple[str, str] | None:
    """
    Return (network_ip, broadcast_ip) for a CIDR like '192.168.1.37/24'.
    This mirrors the logic the client uses to answer the 'Network and Broadcast' question.
    """
    try:
        addr_str, prefix_str = cidr.split("/")
        prefix = int(prefix_str)
        octets = addr_str.split(".")
        if len(octets) != 4:
            return None
        a, b, c, d = [int(x) for x in octets]
    except Exception:
        return None

    if prefix < 0 or prefix > 32:
        return None

    # convert IP to 32-bit int
    ip_int = _ip_to_int(a, b, c, d)

    # build mask: first prefix bits are 1s, the rest 0s
    if prefix == 0:
        mask = 0
    else:
        mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)

    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return (net_ip, bcast_ip)

def compute_correct_answer(question_type: str, short_question: str):
    """
    Given the question_type (e.g. 'Mathematics') and the short_question
    (e.g. '12 + 3 - 4'), produce the canonical correct answer in the same
    format the client would send back in auto mode.
    """
    qt = question_type.strip()
    if not isinstance(short_question, str) or not short_question:
        return None

    if qt == "Mathematics":
        # e.g. "12 + 3 - 4 + 5" -> "16"
        return _eval_plus_minus(short_question)

    if qt == "Roman Numerals":
        # e.g. "XLV" -> "45"
        return str(_roman_to_int(short_question))

    if qt == "Usable IP Addresses of a Subnet":
        # e.g. "192.168.1.0/24" -> "254"
        return _usable_ipv4_addresses(short_question)

    if qt == "Network and Broadcast Address of a Subnet":
        # e.g. "192.168.1.37/24" -> ("192.168.1.0", "192.168.1.255")
        pair = _network_and_broadcast_pair(short_question)
        if pair is None:
            return None
        net_ip, bcast_ip = pair
        # The client responds as "NETWORK and BROADCAST"
        # so for correctness comparison, we return exactly that string.
        return f"{net_ip} and {bcast_ip}"

    return None

def format_feedback(template: str, answer: str, correct_answer: str) -> str:
    """
    Fill {answer} and {correct_answer} placeholders if they appear in the template.
    """
    try:
        return template.format(answer=answer, correct_answer=correct_answer)
    except Exception:
        return template

# -------------------------------------------------------------------
# Leaderboard / standings
# -------------------------------------------------------------------

def pluralize_points(n: int) -> str:
    singular = CFG.get("points_noun_singular", "point")
    plural = CFG.get("points_noun_plural", "points")
    return singular if n == 1 else plural

def sorted_players() -> List[Tuple[str, int]]:
    items = [(p["name"], p["score"]) for p in PLAYERS.values()]
    # Sort: score desc, then name asc
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

# -------------------------------------------------------------------
# IO helpers
# -------------------------------------------------------------------

def enc_line(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    w.write(enc_line(obj))
    await w.drain()

# -------------------------------------------------------------------
# Questions adapter (calls qmod from questions.py)
# -------------------------------------------------------------------

def get_short_question_for(question_type: str) -> str:
    """
    Ask questions.py (qmod) to generate the short form of the question.
    We do NOT generate here; this is required by the assignment spec.
    """
    try:
        if question_type == "Mathematics":
            res = qmod.generate_mathematics_question()
        elif question_type == "Roman Numerals":
            res = qmod.generate_roman_numerals_question()
        elif question_type == "Usable IP Addresses of a Subnet":
            res = qmod.generate_usable_addresses_question()
        elif question_type == "Network and Broadcast Address of a Subnet":
            res = qmod.generate_network_broadcast_question()
        else:
            return f"[{question_type}]"
        return str(res) if res is not None else f"[{question_type}]"
    except Exception:
        return f"[{question_type}]"

# -------------------------------------------------------------------
# Client handling (HI / ANSWER / BYE)
# -------------------------------------------------------------------

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

# broadcast helper
async def broadcast(msg: Dict[str, Any]) -> None:
    async with LOCK:
        targets = list(PLAYERS.values())
    for p in targets:
        try:
            await send_line(p["w"], msg)
        except Exception:
            pass

# -------------------------------------------------------------------
# Coordinator (game flow)
# -------------------------------------------------------------------

async def coordinator() -> None:
    # wait until enough players have joined
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

    # tell players: game starting
    await broadcast({
        "message_type": "READY",
        "info": ready_info
    })

    for i, qtype in enumerate(qtypes, start=1):
        # clear collected answers for this round
        async with LOCK:
            CURRENT_ANSWERS.clear()

        # generate question body from questions.py
        short_q = get_short_question_for(qtype)

        # build human-readable line using config format
        fmt = QUESTION_FORMATS.get(qtype)
        if fmt:
            try:
                question_line = fmt.format(short_q)
            except Exception:
                question_line = short_q
        else:
            question_line = short_q

        # e.g. "Question 1 (Mathematics):\nEvaluate 12 + 3 - 4 + 5"
        # note: spec example uses "Question 1 (Type)\n...", colon is optional
        trivia = f"{question_word} {i} ({qtype}):\n{question_line}"

        # send QUESTION to all players
        await broadcast({
            "message_type": "QUESTION",
            "question_type": qtype,
            "trivia_question": trivia,
            "short_question": short_q,
            "time_limit": qsec
        })

        # wait question_seconds so clients can reply
        try:
            await asyncio.sleep(float(qsec))
        except Exception:
            await asyncio.sleep(0)

        # build responses and scoring
        ok_tpl = CFG.get(
            "correct_answer",
            "{answer} is correct!"
        )
        bad_tpl = CFG.get(
            "incorrect_answer",
            "The correct answer is {correct_answer}, but your answer {answer} is incorrect :("
        )

        correct_full = compute_correct_answer(qtype, short_q)
        # correct_full is either:
        #   - "42" (Mathematics, Roman, usable addresses)
        #   - "192.168.1.0 and 192.168.1.255" (network/broadcast)
        #   - None (if something failed)

        async with LOCK:
            players_snapshot = list(PLAYERS.values())

        for p in players_snapshot:
            name = p["name"]
            ans = CURRENT_ANSWERS.get(name, "").strip()

            if correct_full is None:
                ok = False
                correct_str = "N/A"
            else:
                correct_str = correct_full
                ok = (ans == correct_full)

            # award point if correct
            if ok:
                p["score"] += 1

            # fill feedback template
            feedback = format_feedback(
                ok_tpl if ok else bad_tpl,
                ans,
                correct_str
            )

            # send RESULT to this single player
            await send_line(p["w"], {
                "message_type": "RESULT",
                "correct": bool(ok),
                "feedback": feedback
            })

        # broadcast updated leaderboard
        state = build_leaderboard_state()
        await broadcast({
            "message_type": "LEADERBOARD",
            "state": state
        })

        # wait between questions if configured
        if i < len(qtypes) and qgap:
            try:
                await asyncio.sleep(float(qgap))
            except Exception:
                await asyncio.sleep(0)

    # game finished: broadcast final standings
    final_text = build_final_standings()
    await broadcast({
        "message_type": "FINISHED",
        "final_standings": final_text
    })

# -------------------------------------------------------------------
# Config / main
# -------------------------------------------------------------------

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
