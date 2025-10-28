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

import questions as qmod

# ---------------- Global state ----------------
PLAYERS: Dict[str, Dict[str, Any]] = {}      # pid -> {"w": writer, "name": str, "score": int}
CURRENT_ANSWERS: Dict[str, str] = {}         # username -> last answer (this round)
SEEN_USERS: set[str] = set()                 # usernames that have ever sent HI

LOCK = asyncio.Lock()
READY = asyncio.Event()

CFG: Dict[str, Any] = {}
REQUIRED_PLAYERS: int = 2
QUESTION_FORMATS: Dict[str, str] = {}

# -------------------------------------------------------------------
# Answer-evaluation helpers (mirrors client auto-answer logic)
# -------------------------------------------------------------------

def _eval_plus_minus(expr: str) -> str | None:
    """
    Evaluate simple + / - expressions with spaces, e.g. "12 + 3 - 4 + 5".
    Return result as string, or None on failure.
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
    Convert a Roman numeral (supports subtractives) to integer.
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
    "a.b.c.d/prefix" -> usable IPv4 host count.
    For /31 and /32 we return "0".
    Formula: (2^(32-prefix) - 2)
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
    return ((a << 24) |
            (b << 16) |
            (c << 8)  |
            d)

def _int_to_ip(n: int) -> str:
    a = (n >> 24) & 255
    b = (n >> 16) & 255
    c = (n >> 8) & 255
    d = n & 255
    return f"{a}.{b}.{c}.{d}"

def _network_and_broadcast_pair(cidr: str) -> Tuple[str, str] | None:
    """
    Return (network_ip, broadcast_ip) for CIDR like "192.168.1.37/24".
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

    ip_int = _ip_to_int(a, b, c, d)

    if prefix == 0:
        mask = 0
    else:
        mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)

    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return (net_ip, bcast_ip)

def compute_correct_answer(question_type: str, short_question: str) -> str | None:
    """
    Map question_type + short_question -> canonical correct answer string.
    Must match what the auto client would send.
    """
    qt = question_type.strip()
    if not isinstance(short_question, str) or not short_question:
        return None

    if qt == "Mathematics":
        # "63 - 41 + 19 - 41 + 39" -> "39"
        return _eval_plus_minus(short_question)

    if qt == "Roman Numerals":
        # "MCCCXLVIII" -> "1348"
        return str(_roman_to_int(short_question))

    if qt == "Usable IP Addresses of a Subnet":
        # "192.168.1.0/24" -> "254"
        return _usable_ipv4_addresses(short_question)

    if qt == "Network and Broadcast Address of a Subnet":
        # "192.168.1.37/24" -> "192.168.1.0 and 192.168.1.255"
        pair = _network_and_broadcast_pair(short_question)
        if pair is None:
            return None
        net_ip, bcast_ip = pair
        return f"{net_ip} and {bcast_ip}"

    return None

def format_feedback(template: str, answer: str, correct_answer: str) -> str:
    """
    Fill {answer} and {correct_answer} fields in template safely.
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
    """
    Return list of (name, score), sorted by score desc then name asc.
    Uses only *currently connected* PLAYERS.
    """
    items = [(p["name"], p["score"]) for p in PLAYERS.values()]
    items.sort(key=lambda t: (-t[1], t[0]))
    return items

def build_leaderboard_state() -> str:
    """
    Build string:
    "1. Alice: 2 points\n1. Bob: 2 points\n3. Carol: 1 point"
    Tie -> same rank number.
    """
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
    """
    Multiline final standings including winner line.
    """
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
# Question generator adapter (calls questions.py)
# -------------------------------------------------------------------

def get_short_question_for(question_type: str) -> str:
    """
    Use questions.py to generate the short question string.
    We do NOT invent our own question text.
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
    """
    Each TCP client stays in here.
    We:
      - record them on HI
      - store answers on ANSWER
      - keep connection open
    """
    addr = w.get_extra_info("peername")
    pid = f"{addr[0]}:{addr[1]}" if addr else "unknown"

    try:
        while True:
            line = await r.readline()
            if not line:
                break  # disconnect
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                continue

            mtype = str(msg.get("message_type", "")).upper()

            if mtype == "HI":
                username = msg.get("username", pid)
                async with LOCK:
                    # mark this username as "seen", even if they disconnect later
                    SEEN_USERS.add(username)
                    # keep active writer
                    PLAYERS[pid] = {"w": w, "name": username, "score": 0}
                    # once we've SEEN enough users at any time, start game
                    if len(SEEN_USERS) >= REQUIRED_PLAYERS:
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

# broadcast helper for all *currently connected* players
async def broadcast(msg: Dict[str, Any]) -> None:
    async with LOCK:
        targets = list(PLAYERS.values())

    # send to everyone  concurrently
    tasks = []
    for p in targets:
        tasks.append(send_line(p["w"], msg))

    # wait for all writers to flush
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------
# Coordinator (game flow)
# -------------------------------------------------------------------
async def coordinator() -> None:
    # Wait until enough distinct usernames have said HI
    await READY.wait()

    qtypes = CFG.get("question_types", []) or []
    question_word = CFG.get("question_word", "Question")
    qsec = CFG.get("question_seconds")
    qgap = CFG.get("question_interval_seconds", 0)

    ready_info_tpl = CFG.get("ready_info", "Game starts soon!")
    try:
        ready_info = ready_info_tpl.format(question_interval_seconds=qgap)
    except Exception:
        ready_info = ready_info_tpl

    # Tell all connected players we're about to start
    await broadcast({
        "message_type": "READY",
        "info": ready_info
    })

    # Tiny delay so clients can print READY before first QUESTION
    await asyncio.sleep(0.05)

    total_questions = len(qtypes)

    for i, qtype in enumerate(qtypes, start=1):
        # Clear answers for this round
        async with LOCK:
            CURRENT_ANSWERS.clear()

        # Build the question text
        short_q = get_short_question_for(qtype)

        fmt = QUESTION_FORMATS.get(qtype)
        if fmt:
            try:
                question_line = fmt.format(short_q)
            except Exception:
                question_line = short_q
        else:
            question_line = short_q

        trivia = f"{question_word} {i} ({qtype}):\n{question_line}"

        # Broadcast the QUESTION to everyone
        await broadcast({
            "message_type": "QUESTION",
            "question_type": qtype,
            "trivia_question": trivia,
            "short_question": short_q,
            "time_limit": qsec
        })

        # Give players time_limit seconds (+ a small grace) to answer
        try:
            await asyncio.sleep(float(qsec) + 0.3)
        except Exception:
            await asyncio.sleep(0)

        # --- First scoring pass ---
        ok_tpl = CFG.get("correct_answer", "{answer} is correct!")
        bad_tpl = CFG.get(
            "incorrect_answer",
            "The correct answer is {correct_answer}, but your answer {answer} is incorrect :("
        )

        correct_full = compute_correct_answer(qtype, short_q)

        # Snapshot players and answers under the lock
        async with LOCK:
            players_snapshot = list(PLAYERS.values())
            players_by_name = {p["name"]: p for p in players_snapshot}
            answers_snapshot = dict(CURRENT_ANSWERS)

        # We'll track who already got a RESULT this round
        already_resulted = set()

        for username, raw_ans in answers_snapshot.items():
            p = players_by_name.get(username)
            if p is None:
                continue

            ans = raw_ans.strip()
            correct_str = correct_full if correct_full is not None else "N/A"
            ok = (correct_full is not None and ans == correct_full)

            if ok:
                p["score"] += 1

            feedback = format_feedback(
                ok_tpl if ok else bad_tpl,
                ans,
                correct_str
            )

            await send_line(p["w"], {
                "message_type": "RESULT",
                "correct": bool(ok),
                "feedback": feedback
            })

            already_resulted.add(username)

        # Small pause so those RESULT lines flush before next broadcast
        await asyncio.sleep(0.05)

        # Branch by "is this the last question?"
        if i < total_questions:
            # Not the last question:
            # Send LEADERBOARD (every connected player gets it)
            state = build_leaderboard_state()
            await broadcast({
                "message_type": "LEADERBOARD",
                "state": state
            })

            # Optional gap between questions
            if qgap:
                try:
                    await asyncio.sleep(float(qgap))
                except Exception:
                    await asyncio.sleep(0)

        else:
            # Last question:
            # Give a tiny extra grace window to catch any last-millisecond ANSWERs
            try:
                await asyncio.sleep(1)
            except Exception:
                await asyncio.sleep(0)

            # Take one more snapshot of CURRENT_ANSWERS and players
            async with LOCK:
                players_snapshot2 = list(PLAYERS.values())
                players_by_name2 = {p["name"]: p for p in players_snapshot2}
                answers_snapshot2 = dict(CURRENT_ANSWERS)

            # Second scoring pass ONLY for users who did NOT
            # receive a RESULT yet in this round
            for username, raw_ans in answers_snapshot2.items():
                if username in already_resulted:
                    continue  # don't double-send RESULT

                p = players_by_name2.get(username)
                if p is None:
                    continue

                ans = raw_ans.strip()
                correct_str = correct_full if correct_full is not None else "N/A"
                ok = (correct_full is not None and ans == correct_full)

                if ok:
                    p["score"] += 1

                feedback = format_feedback(
                    ok_tpl if ok else bad_tpl,
                    ans,
                    correct_str
                )

                await send_line(p["w"], {
                    "message_type": "RESULT",
                    "correct": bool(ok),
                    "feedback": feedback
                })
            await asyncio.sleep(0.05)

            # Now announce final standings to  everyone
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
    # require: python server.py --config <path>
    if not args or args[0] != "--config" or len(args) < 2:
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"server.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    global CFG, REQUIRED_PLAYERS, QUESTION_FORMATS
    CFG = load_server_config(cfg_path)
    REQUIRED_PLAYERS = int(CFG.get("players"))
    QUESTION_FORMATS = CFG.get("question_formats", {}) or {}

    port = int(CFG.get("port", 5050))
    try:
        srv = await asyncio.start_server(handle_client, "127.0.0.1", port)
    except OSError:
        print(f"server.py: Binding to port {port} was unsuccessful", file=sys.stderr)
        sys.exit(1)

    print(f"[server] i  am listening on 127.0.0.1:{port}")

    # Start the coordinator in the  background
    asyncio.create_task(coordinator())

    # Run the server forever
    async with srv:
        await srv.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass