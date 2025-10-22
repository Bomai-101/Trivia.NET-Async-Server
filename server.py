#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Minimal quiz server per assignment spec.

Start:
  python server.py --config <config_path>
  # or
  python server.py <config_path>

Config (valid JSON, required):
{
  "port": <int>,
  "players": <int>,
  "question_formats": { "<type>": "..." },
  "question_types": [ "<type>", ... ],
  "question_seconds": <int|float>,
  "question_interval_seconds": <int|float>,
  "ready_info": <str>,                       # "Game starts in {question_interval_seconds} seconds!"
  "question_word": <str>,                    # e.g., "Question"
  "correct_answer": <str>,                   # feedback on correct
  "incorrect_answer": <str>,                 # feedback on incorrect
  "points_noun_singular": <str>,
  "points_noun_plural": <str>,
  "final_standings_heading": <str>,
  "one_winner": <str>,                       # "The winner is: {}"
  "multiple_winners": <str>                  # "The winners are: {}"
}

Protocol (NDJSON):
Client -> Server:  HI {"username": "<str>"} | ANSWER {"answer":"..."} | BYE
Server -> Client:  READY {"info": "..."}
                   QUESTION {"trivia_question":"...", "short_question":"...", "time_limit": <float>}
                   RESULT {"feedback":"..."}
                   LEADERBOARD {"feedback":"..."}
                   FINISHED {"final_standings":"..."}
"""

import json
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List, Set

# ----------------- I/O helpers (NDJSON) -----------------
def send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)

def recv_json_line(conn: socket.socket) -> Optional[Dict[str, Any]]:
    buf = bytearray()
    while True:
        b = conn.recv(1)
        if not b:
            # EOF
            if not buf:
                return None
            break
        if b == b"\n":
            break
        buf.extend(b)
    return json.loads(buf.decode("utf-8"))

# ----------------- Global runtime -----------------
CFG: Dict[str, Any] = {}
STOP = threading.Event()

# player_id -> info
# info: {"conn": socket, "username": str, "score": int, "alive": bool, "lock": threading.Lock()}
PLAYERS: Dict[str, Dict[str, Any]] = {}
PLAYERS_LOCK = threading.Lock()

# round answers: player_id -> {"answer": str, "ts": float}
ROUND_ANSWERS: Dict[str, Dict[str, Any]] = {}
ROUND_LOCK = threading.Lock()

JOINED_EVENT = threading.Event()  # set when enough players joined

def player_key(addr: Tuple[str, int]) -> str:
    return f"{addr[0]}:{addr[1]}"

# ----------------- Config & errors -----------------
def load_config_from_argv() -> Dict[str, Any]:
    args = sys.argv[1:]
    if not args:
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    if args[0] == "--config":
        if len(args) < 2:
            print("server.py: Configuration not provided", file=sys.stderr)
            sys.exit(1)
        cfg_path = args[1]
    else:
        cfg_path = args[0]

    p = Path(cfg_path)
    if not p.exists():
        print(f"server.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    # Per spec, JSON is always valid; still guard
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        print(f"server.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

# ----------------- Messaging helpers -----------------
def broadcast(msg: Dict[str, Any], only_alive=True):
    with PLAYERS_LOCK:
        for info in list(PLAYERS.values()):
            if only_alive and not info["alive"]:
                continue
            try:
                send_json_line(info["conn"], msg)
            except Exception:
                info["alive"] = False

def alive_player_ids() -> List[str]:
    with PLAYERS_LOCK:
        return [pid for pid, p in PLAYERS.items() if p["alive"]]

def alive_players_info() -> List[Dict[str, Any]]:
    with PLAYERS_LOCK:
        return [p for p in PLAYERS.values() if p["alive"]]

# ----------------- Question generation (minimal demos) -----------------
def make_question_text(q_index: int, q_type: str, short_q: str) -> str:
    """Compose the 'trivia_question' using question_formats and question_word."""
    qword = CFG.get("question_word", "Question")
    fmt_map: Dict[str, str] = CFG.get("question_formats", {})
    # Prefer matching type format; else fallback to "{}"
    body_fmt = fmt_map.get(q_type, "{}")
    body = body_fmt.format(short_q)
    return f"{qword} {q_index}: {body}"

def pick_question_material(q_type: str) -> Tuple[str, str]:
    """
    Return (short_question, correct_answer) for a given type.
    This is intentionally simple, just for local testing with the client.
    """
    t = q_type.lower()

    if "usable ip addresses" in t:
        sq = "192.168.1.0/24"
        correct = "254"
        return sq, correct

    if "network and broadcast address" in t:
        sq = "192.168.1.0/24"
        correct = "192.168.1.0, 192.168.1.255"
        return sq, correct

    if "roman numerals" in t:
        sq = "XIV"
        correct = "14"
        return sq, correct

    if "mathematics" in t or "math" in t or "evaluate" in t:
        sq = "2+3"
        correct = "5"
        return sq, correct

    # Fallback generic
    sq = "HELLO"
    correct = "HELLO"
    return sq, correct

# ----------------- Result/standings text -----------------
def leaderboard_feedback() -> str:
    with PLAYERS_LOCK:
        alive = [p for p in PLAYERS.values() if p["alive"]]
        # sort by score desc then name
        alive.sort(key=lambda x: (-x["score"], x["username"]))
        parts = [f'{p["username"]}: {p["score"]}' for p in alive]
    return " | ".join(parts) if parts else "No active players."

def final_standings_text() -> str:
    heading = CFG.get("final_standings_heading", "Final standings:")
    with PLAYERS_LOCK:
        alive = [p for p in PLAYERS.values() if p["alive"]]
        alive.sort(key=lambda x: (-x["score"], x["username"]))
        lines = [f'{p["username"]}: {p["score"]}' for p in alive]

        if not alive:
            winners_line = CFG.get("multiple_winners", "The winners are: {}").format("")
            return f"{heading}\n{winners_line}"

        top_score = alive[0]["score"]
        winners = [p["username"] for p in alive if p["score"] == top_score]
        if len(winners) == 1:
            winners_line = CFG.get("one_winner", "The winner is: {}").format(winners[0])
        else:
            winners_line = CFG.get("multiple_winners", "The winners are: {}").format(", ".join(winners))

    return f"{heading}\n" + "\n".join(lines) + "\n" + winners_line

# ----------------- Per-client thread -----------------
def client_thread(conn: socket.socket, addr: Tuple[str, int]):
    pid = player_key(addr)
    try:
        while not STOP.is_set():
            msg = recv_json_line(conn)
            if msg is None:
                break
            mtype = str(msg.get("type", "")).upper()

            if mtype == "HI":
                username = str(msg.get("username", pid))
                with PLAYERS_LOCK:
                    if pid not in PLAYERS:
                        PLAYERS[pid] = {
                            "conn": conn,
                            "username": username,
                            "score": 0,
                            "alive": True,
                            "lock": threading.Lock(),
                        }
                # Check join count
                with PLAYERS_LOCK:
                    active = [p for p in PLAYERS.values() if p["alive"]]
                if len(active) >= int(CFG["players"]):
                    JOINED_EVENT.set()

            elif mtype == "ANSWER":
                with PLAYERS_LOCK:
                    pinfo = PLAYERS.get(pid)
                    if not pinfo or not pinfo["alive"]:
                        continue
                ans = str(msg.get("answer", ""))
                with ROUND_LOCK:
                    # record first answer only
                    if pid not in ROUND_ANSWERS:
                        ROUND_ANSWERS[pid] = {"answer": ans, "ts": time.time()}

            elif mtype == "BYE":
                with PLAYERS_LOCK:
                    if pid in PLAYERS:
                        PLAYERS[pid]["alive"] = False
                break

            # unknown types ignored (per minimal server)
    except Exception:
        pass
    finally:
        # Mark dead and close socket
        with PLAYERS_LOCK:
            if pid in PLAYERS:
                PLAYERS[pid]["alive"] = False
        try:
            conn.close()
        except Exception:
            pass

# ----------------- Game coordinator -----------------
def coordinator():
    # Wait for enough players
    while not STOP.is_set():
        with PLAYERS_LOCK:
            active = [p for p in PLAYERS.values() if p["alive"]]
        if len(active) >= int(CFG["players"]):
            break
        JOINED_EVENT.wait(timeout=0.2)

    if STOP.is_set():
        return

    # READY
    info = CFG.get("ready_info", "Game starts in {question_interval_seconds} seconds!")
    info = info.format(question_interval_seconds=CFG.get("question_interval_seconds", 1))
    broadcast({"type": "READY", "info": info})

    # interval before Q1
    time.sleep(float(CFG.get("question_interval_seconds", 1)))

    qsecs = float(CFG.get("question_seconds", 10))
    qtypes: List[str] = list(CFG.get("question_types", []))

    for i, qtype in enumerate(qtypes, start=1):
        with ROUND_LOCK:
            ROUND_ANSWERS.clear()

        short_q, correct = pick_question_material(qtype)
        trivia = make_question_text(i, qtype, short_q)

        # Send QUESTION
        broadcast({
            "type": "QUESTION",
            "trivia_question": trivia,
            "short_question": short_q,
            "time_limit": qsecs
        })

        # Wait up to qsecs, or until all alive answered
        end = time.time() + qsecs
        while time.time() < end and not STOP.is_set():
            with PLAYERS_LOCK:
                alive_ids = [pid for pid, p in PLAYERS.items() if p["alive"]]
            with ROUND_LOCK:
                if all(pid in ROUND_ANSWERS for pid in alive_ids) and alive_ids:
                    break
            time.sleep(0.05)

        # Mark and send RESULT to each alive player
        with PLAYERS_LOCK:
            items = list(PLAYERS.items())

        for pid, pinfo in items:
            if not pinfo["alive"]:
                continue
            with ROUND_LOCK:
                ans = ROUND_ANSWERS.get(pid, {}).get("answer", None)

            is_correct = (ans is not None and ans == correct)
            if is_correct:
                pinfo["score"] += 1
                feedback = CFG.get("correct_answer", "Correct!")
            else:
                feedback = CFG.get("incorrect_answer", "Incorrect!")

            try:
                send_json_line(pinfo["conn"], {"type": "RESULT", "feedback": feedback})
            except Exception:
                pinfo["alive"] = False

        # After each question: either LEADERBOARD or FINISHED
        is_last = (i == len(qtypes))
        if not is_last:
            lb_text = leaderboard_feedback()
            broadcast({"type": "LEADERBOARD", "feedback": lb_text})
            time.sleep(float(CFG.get("question_interval_seconds", 1)))
        else:
            fs_text = final_standings_text()
            broadcast({"type": "FINISHED", "final_standings": fs_text})
            # close all and stop
            with PLAYERS_LOCK:
                for p in PLAYERS.values():
                    try:
                        p["conn"].close()
                    except Exception:
                        pass
                    p["alive"] = False
            STOP.set()
            break

# ----------------- Main -----------------
def main():
    global CFG
    CFG = load_config_from_argv()

    host = "0.0.0.0"
    port = int(CFG["port"])

    # graceful stop
    def _stop(sig, _f):
        STOP.set()
        # poke accept()
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    # listen
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind((host, port))
    except Exception:
        print(f"server.py: Binding to port {port} was unsuccessful", file=sys.stderr)
        sys.exit(1)

    srv.listen(16)
    print(f"[server] listening on {host}:{port}")

    # coordinator thread
    t_coord = threading.Thread(target=coordinator, daemon=True)
    t_coord.start()

    threads: List[threading.Thread] = []
    try:
        while not STOP.is_set():
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            t = threading.Thread(target=client_thread, args=(conn, addr), daemon=True)
            t.start()
            threads.append(t)
    finally:
        try:
            srv.close()
        except Exception:
            pass
        for t in threads:
            t.join(timeout=0.5)
        t_coord.join(timeout=0.5)
        print("[server] bye.")

if __name__ == "__main__":
    main()
