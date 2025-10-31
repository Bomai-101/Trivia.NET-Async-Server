import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple
import questions as qmod


PLAYERS: Dict[str, Dict[str, Any]] = {}
CURRENT_ANSWERS: Dict[str, str] = {}
SEEN_USERS: set[str] = set()

LOCK = asyncio.Lock()
READY = asyncio.Event()

CFG: Dict[str, Any] = {}
REQUIRED_PLAYERS: int = 2
QUESTION_FORMATS: Dict[str, str] = {}
CURRENT_QTYPE: str | None = None
CURRENT_SHORT_Q: str | None = None
ROUND_OPEN: bool = False


def _active_player_count() -> int:
    """Return the number of currently active players."""
    return sum(1 for p in PLAYERS.values() if p.get("active", False))


def _eval_plus_minus(expr: str) -> str | None:
    """Evaluate space-delimited + / - expression to a string; None on failure."""
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
    """Convert a Roman numeral string to its integer value."""
    ROMAN_MAP = {
        "M": 1000, "CM": 900, "D": 500, "CD": 400,
        "C": 100, "XC": 90, "L": 50, "XL": 40,
        "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
    }
    i = 0
    n = 0
    s = s.strip().upper()
    while i < len(s):
        if i + 1 < len(s) and s[i:i + 2] in ROMAN_MAP:
            n += ROMAN_MAP[s[i:i + 2]]
            i += 2
        else:
            n += ROMAN_MAP.get(s[i], 0)
            i += 1
    return n


def _usable_ipv4_addresses(cidr: str) -> str | None:
    """Return usable IPv4 host count string for a CIDR; '0' for /31 or /32."""
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
    """Pack 4 octets into one 32-bit integer."""
    return ((a << 24) | (b << 16) | (c << 8) | d)


def _int_to_ip(n: int) -> str:
    """Unpack a 32-bit integer into dotted-quad IPv4 string."""
    a = (n >> 24) & 255
    b = (n >> 16) & 255
    c = (n >> 8) & 255
    d = n & 255
    return f"{a}.{b}.{c}.{d}"


def _network_and_broadcast_pair(cidr: str) -> Tuple[str, str] | None:
    """Return (network_ip, broadcast_ip) for a CIDR or None on parse error."""
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
    """Compute the expected final answer string for a given question type."""
    qt = question_type.strip()
    if not isinstance(short_question, str) or not short_question:
        return None
    if qt == "Mathematics":
        return _eval_plus_minus(short_question)
    if qt == "Roman Numerals":
        return str(_roman_to_int(short_question))
    if qt == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_question)
    if qt == "Network and Broadcast Address of a Subnet":
        pair = _network_and_broadcast_pair(short_question)
        if pair is None:
            return None
        net_ip, bcast_ip = pair
        return f"{net_ip} and {bcast_ip}"
    return None


def format_feedback(template: str, answer: str, correct_answer: str) -> str:
    """Fill a feedback template with answer and correct_answer fields."""
    try:
        return template.format(answer=answer, correct_answer=correct_answer)
    except Exception:
        return template


def pluralize_points(n: int) -> str:
    """Return singular/plural points noun based on n using CFG settings."""
    singular = CFG.get("points_noun_singular", "point")
    plural = CFG.get("points_noun_plural", "points")
    return singular if n == 1 else plural


def sorted_players() -> List[Tuple[str, int]]:
    """Return a score-sorted list of (name, score), tie-broken by name asc."""
    items = [(p["name"], p["score"]) for p in PLAYERS.values()]
    items.sort(key=lambda t: (-t[1], t[0]))
    return items


def build_leaderboard_state() -> str:
    """Build the leaderboard text with stable ranks and ties."""
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
    """Build final standings text including winners summary."""
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


def enc_line(obj: Dict[str, Any]) -> bytes:
    """Encode a JSON object as a newline-terminated UTF-8 NDJSON line."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    """Send a NDJSON line to a client and drain the writer."""
    w.write(enc_line(obj))
    await w.drain()


def get_short_question_for(question_type: str) -> str:
    """Generate one short_question string for the given question_type."""
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


async def handle_client(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
    """Per-client coroutine: process HI, ANSWER, and BYE; update shared state."""
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
                    SEEN_USERS.add(username)
                    PLAYERS[pid] = {"w": w, "name": username, "score": 0, "active": True}
                    if _active_player_count() >= REQUIRED_PLAYERS and not READY.is_set():
                        READY.set()

            elif mtype == "ANSWER":
                ans_raw = str(msg.get("answer", ""))

                async with LOCK:
                    qtype_now = CURRENT_QTYPE
                    short_q_now = CURRENT_SHORT_Q
                    round_open = ROUND_OPEN
                    p = PLAYERS.get(pid)

                    correct_full = compute_correct_answer(qtype_now or "", short_q_now or "")
                    correct_str_fallback = "N/A" if correct_full is None else correct_full
                    is_correct_now = (correct_full is not None and ans_raw == correct_full)
                    is_first_answer = (pid not in CURRENT_ANSWERS)

                    if is_first_answer:
                        CURRENT_ANSWERS[pid] = ans_raw

                    if round_open and is_first_answer and p is not None:
                        if is_correct_now:
                            p["score"] = p.get("score", 0) + 1

                        ok_tpl = CFG.get("correct_answer", "{answer} is correct!")
                        bad_tpl = CFG.get(
                            "incorrect_answer",
                            "The correct answer is {correct_answer}, but your answer {answer} is incorrect :("
                        )
                        feedback = format_feedback(
                            ok_tpl if is_correct_now else bad_tpl,
                            ans_raw,
                            correct_str_fallback
                        )
                        w_target = p.get("w")
                    else:
                        w_target = None

                if w_target is not None:
                    await send_line(w_target, {
                        "message_type": "RESULT",
                        "correct": bool(is_correct_now),
                        "feedback": feedback
                    })

            elif mtype == "BYE":
                async with LOCK:
                    if pid in PLAYERS:
                        PLAYERS[pid]["active"] = False
                        PLAYERS[pid]["w"] = None
                break

    finally:
        async with LOCK:
            if pid in PLAYERS:
                PLAYERS[pid]["active"] = False
                PLAYERS[pid]["w"] = None
        try:
            w.close()
            await w.wait_closed()
        except Exception:
            pass


async def broadcast(msg: Dict[str, Any]) -> None:
    """Send a message to all active players."""
    async with LOCK:
        targets = [p for p in PLAYERS.values() if p.get("w") is not None and p.get("active", False)]
    tasks = []
    for p in targets:
        tasks.append(send_line(p["w"], msg))
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def score_current_round(qtype: str, short_q: str, i: int, total_questions: int, qgap: float) -> None:
    """After a round closes, show leaderboard or final standings, with a gap."""
    await asyncio.sleep(0.05)
    if i < total_questions:
        state = build_leaderboard_state()
        await broadcast({"message_type": "LEADERBOARD", "state": state})
        if qgap:
            try:
                await asyncio.sleep(float(qgap))
            except Exception:
                await asyncio.sleep(0)
    else:
        final_text = build_final_standings()
        await broadcast({
            "message_type": "FINISHED",
            "final_standings": final_text
        })


async def coordinator() -> None:
    """Main quiz loop: wait for ready, then iterate questions and timing."""
    await READY.wait()
    qtypes = CFG.get("question_types", []) or []
    question_word = CFG.get("question_word", "Question")
    qsec = CFG.get("question_seconds")
    qgap = CFG.get("question_interval_seconds", 0)
    ready_info_tpl = CFG.get("ready_info", "Game starts soon!")
    try:
        ready_info = ready_info_tpl.format(
            players=REQUIRED_PLAYERS,
            question_seconds=qsec,
            question_interval_seconds=qgap
        )
    except Exception:
        ready_info = ready_info_tpl
    await broadcast({"message_type": "READY", "info": ready_info})
    await asyncio.sleep(0.5)
    total_questions = len(qtypes)
    for i, qtype in enumerate(qtypes, start=1):
        async with LOCK:
            CURRENT_ANSWERS.clear()
            global CURRENT_QTYPE, CURRENT_SHORT_Q, ROUND_OPEN
            CURRENT_QTYPE = qtype
            CURRENT_SHORT_Q = get_short_question_for(qtype)
            ROUND_OPEN = True
        short_q = CURRENT_SHORT_Q
        fmt = QUESTION_FORMATS.get(qtype)
        if fmt:
            try:
                question_line = fmt.format(short_q)
            except Exception:
                question_line = short_q
        else:
            question_line = short_q
        trivia = f"{question_word} {i} ({qtype}):\n{question_line}"
        await broadcast({
            "message_type": "QUESTION",
            "question_type": qtype,
            "trivia_question": trivia,
            "short_question": short_q,
            "time_limit": qsec
        })
        await asyncio.sleep(float(qsec))
        async with LOCK:
            ROUND_OPEN = False
        await score_current_round(qtype, short_q, i, total_questions, qgap)


def load_server_config(path: Path) -> Dict[str, Any]:
    """Load server configuration from JSON file; return empty dict on error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def main() -> None:
    """Parse args, load config, bind server, and run until stopped."""
    args = sys.argv[1:]
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
    def format_cfg_value(k, v):
        if not isinstance(v, str):
            return v
        if k in (
            "question_word",
            "points_noun_singular",
            "points_noun_plural"
        ):
            return v
        if k == "question_formats":
            return v
        try:
            return v.format(**CFG)
        except Exception:
            return v

    for key, val in list(CFG.items()):
        CFG[key] = format_cfg_value(key, val)

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
