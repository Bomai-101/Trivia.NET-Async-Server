#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal quiz server following the provided scaffold and function names.

Protocol (NDJSON, one JSON per line):
- Client → Server:
  HI { "name": "<optional>" }
  ANSWER { "answer": "<string>" }
  BYE
- Server → Client:
  ACK { "player_id": "<str>" }
  READY { "round": <int>, "total_rounds": <int>, "question_seconds": <int> }
  QUESTION { "question_type": "<str>", "question": "<str>", "short_question": "<str>" }
  RESULT { "correct": <bool>, "feedback": "<str>", "score_delta": <int> }
  LEADERBOARD { "state": "<str>", "scores": {"<player_id>": <int>, ...} }
  FINISHED { "scores": {...} }
  ERROR { "message": "<str>" }

Config file (JSON), optional:
{
  "host": "127.0.0.1",
  "port": 5050,
  "players": 2,
  "question_seconds": 10,
  "question_types": ["math:add", "upper"]
}
"""

import json
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

# ---------------- Global runtime state ----------------
SERVER_CFG: Dict[str, Any] = {}
PLAYERS_REQUIRED = 2
QUESTION_SECONDS = 10
QUESTION_TYPES: List[str] = ["math:add", "upper"]

# player_id -> {"conn": socket.socket, "name": str, "score": int, "alive": bool, "lock": threading.Lock()}
PLAYERS: Dict[str, Dict[str, Any]] = {}
PLAYERS_LOCK = threading.Lock()

# round-scoped answers: player_id -> {"answer": str, "ts": float}
CURRENT_ANSWERS: Dict[str, Dict[str, Any]] = {}
CURRENT_ANSWERS_LOCK = threading.Lock()

STOP_EVENT = threading.Event()
ROUND_EVENT = threading.Event()

# ---------------- Utility I/O ----------------
def send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(line)

def recv_json_line(conn: socket.socket) -> Optional[Dict[str, Any]]:
    # Read a single line (blocking)
    buf = b""
    while True:
        ch = conn.recv(1)
        if not ch:
            if not buf:
                return None
            break
        if ch == b"\n":
            break
        buf += ch
    try:
        return json.loads(buf.decode("utf-8"))
    except json.JSONDecodeError:
        return {"type": "ERROR", "message": "invalid_json", "raw": buf.decode("utf-8", "ignore")}

def load_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 5050,
        "players": 2,
        "question_seconds": 10,
        "question_types": ["math:add", "upper"]
    }
    if path is None:
        return defaults
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        defaults.update(data or {})
    except Exception as e:
        print(f"[server] Failed to load config: {e}", file=sys.stderr)
    return defaults

# ---------------- Scaffolded functions ----------------
def add_player(conn: socket.socket, addr: Tuple[str, int], name: Optional[str]) -> str:
    """Register a player and return a player_id."""
    pid = f"{addr[0]}:{addr[1]}"
    with PLAYERS_LOCK:
        if pid not in PLAYERS:
            PLAYERS[pid] = {
                "conn": conn,
                "name": name or pid,
                "score": 0,
                "alive": True,
                "lock": threading.Lock(),
            }
    return pid

def remove_player(player_id: str) -> None:
    with PLAYERS_LOCK:
        info = PLAYERS.get(player_id)
        if info:
            info["alive"] = False
            try:
                info["conn"].close()
            except Exception:
                pass

def handle_player_answer(player_id: str, answer: str) -> None:
    with CURRENT_ANSWERS_LOCK:
        CURRENT_ANSWERS[player_id] = {"answer": answer, "ts": time.time()}

def send_to_all_players(msg: Dict[str, Any]) -> None:
    with PLAYERS_LOCK:
        for pid, info in list(PLAYERS.items()):
            if not info["alive"]:
                continue
            try:
                send_json_line(info["conn"], msg)
            except Exception:
                info["alive"] = False

def receive_answers(timeout_seconds: int) -> None:
    """Block until all active players answered, or timeout expires."""
    end = time.time() + timeout_seconds
    while time.time() < end:
        with PLAYERS_LOCK:
            alive_players = [pid for pid, p in PLAYERS.items() if p["alive"]]
        with CURRENT_ANSWERS_LOCK:
            got = set(CURRENT_ANSWERS.keys())
        if got.issuperset(alive_players) and len(alive_players) > 0:
            return
        time.sleep(0.05)

def generate_leaderboard_state() -> str:
    with PLAYERS_LOCK:
        items = sorted(((p["name"], p["score"]) for p in PLAYERS.values()), key=lambda x: (-x[1], x[0]))
    return ", ".join(f"{name}:{score}" for name, score in items)

def generate_question(question_type: str) -> Dict[str, Any]:
    """Return a dict with 'question', 'short_question', 'question_type'."""
    if question_type == "upper":
        q = "Convert to UPPERCASE: hello world"
        sq = "hello world"
        return {"question_type": "upper", "question": q, "short_question": sq}
    # default math:add
    if question_type.startswith("math:add"):
        a, b = 3, 4
        q = f"What is {a}+{b}?"
        sq = f"{a}+{b}"
        return {"question_type": "math:add", "question": q, "short_question": sq}
    # fallback
    return {"question_type": question_type, "question": "noop?", "short_question": "noop"}

def generate_question_answer(question_type: str, short_question: str) -> str:
    if question_type == "upper":
        return short_question.upper()
    if question_type.startswith("math:add"):
        # naive parser "X+Y"
        try:
            parts = short_question.split("+")
            return str(int(parts[0]) + int(parts[1]))
        except Exception:
            return ""
    return ""

def start_game(total_rounds: int, question_seconds: int) -> None:
    send_to_all_players({"type": "READY", "round": 0, "total_rounds": total_rounds, "question_seconds": question_seconds})

def start_round(round_idx: int, question_type: str) -> Dict[str, Any]:
    q = generate_question(question_type)
    msg = {"type": "QUESTION", **q}
    send_to_all_players(msg)
    return q

def end_round(round_idx: int, total_rounds: int, award: Dict[str, int]) -> None:
    # Send per-player RESULT (already sent inline in this implementation), then either LEADERBOARD or FINISHED
    if round_idx + 1 < total_rounds:
        state = generate_leaderboard_state()
        with PLAYERS_LOCK:
            scores = {p["name"]: p["score"] for p in PLAYERS.values()}
        send_to_all_players({"type": "LEADERBOARD", "state": state, "scores": scores})
    else:
        with PLAYERS_LOCK:
            scores = {p["name"]: p["score"] for p in PLAYERS.values()}
        send_to_all_players({"type": "FINISHED", "scores": scores})

# ---------------- Per-client thread ----------------
def client_thread(conn: socket.socket, addr: Tuple[str, int]) -> None:
    pid = None
    try:
        while not STOP_EVENT.is_set():
            msg = recv_json_line(conn)
            if msg is None:
                break
            mtype = str(msg.get("type", "")).upper()

            if mtype == "HI":
                name = msg.get("name")
                pid = add_player(conn, addr, name)
                send_json_line(conn, {"type": "ACK", "player_id": pid})
                # If enough players joined, signal the coordinator thread
                with PLAYERS_LOCK:
                    active = [p for p in PLAYERS.values() if p["alive"]]
                if len(active) >= PLAYERS_REQUIRED:
                    ROUND_EVENT.set()

            elif mtype == "ANSWER":
                if pid is None:
                    send_json_line(conn, {"type": "ERROR", "message": "not_registered"})
                    continue
                ans = str(msg.get("answer", ""))
                handle_player_answer(pid, ans)

            elif mtype == "BYE":
                break

            else:
                send_json_line(conn, {"type": "ERROR", "message": f"unknown_type:{msg.get('type')}"})
    except Exception as e:
        # Log and drop
        print(f"[server] client error {addr}: {e}", file=sys.stderr)
    finally:
        if pid:
            remove_player(pid)

# ---------------- Game coordinator ----------------
def coordinator() -> None:
    # Wait for enough players
    while not STOP_EVENT.is_set():
        with PLAYERS_LOCK:
            active = [p for p in PLAYERS.values() if p["alive"]]
        if len(active) >= PLAYERS_REQUIRED:
            break
        ROUND_EVENT.wait(timeout=0.5)

    if STOP_EVENT.is_set():
        return

    total_rounds = len(QUESTION_TYPES)
    start_game(total_rounds, QUESTION_SECONDS)

    for r_idx, qtype in enumerate(QUESTION_TYPES):
        with CURRENT_ANSWERS_LOCK:
            CURRENT_ANSWERS.clear()

        # Announce round and send question
        q = start_round(r_idx, qtype)

        # Wait for answers or timeout
        receive_answers(QUESTION_SECONDS)

        # Evaluate answers and update scores
        correct = generate_question_answer(q["question_type"], q["short_question"])
        awards: Dict[str, int] = {}

        with PLAYERS_LOCK:
            for pid, pinfo in list(PLAYERS.items()):
                if not pinfo["alive"]:
                    continue
                ans_obj = CURRENT_ANSWERS.get(pid)
                ans = ans_obj["answer"] if ans_obj else ""
                is_correct = (ans == correct)
                delta = 1 if is_correct else 0
                pinfo["score"] += delta
                awards[pid] = delta
                feedback = "correct" if is_correct else f"incorrect; expected '{correct}'"
                try:
                    send_json_line(pinfo["conn"], {"type": "RESULT", "correct": is_correct, "feedback": feedback, "score_delta": delta})
                except Exception:
                    pinfo["alive"] = False

        end_round(r_idx, total_rounds, awards)
        if STOP_EVENT.is_set():
            break

# ---------------- Main server loop ----------------
def main():
    global SERVER_CFG, PLAYERS_REQUIRED, QUESTION_SECONDS, QUESTION_TYPES

    # 0. Load config and start listening
    cfg_path = None
    if "--config" in sys.argv:
        idx = sys.argv.index("--config")
        cfg_path = Path(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else None
    SERVER_CFG = load_config(cfg_path)

    host = SERVER_CFG.get("host", "127.0.0.1")
    port = int(SERVER_CFG.get("port", 5050))
    PLAYERS_REQUIRED = int(SERVER_CFG.get("players", 2))
    QUESTION_SECONDS = int(SERVER_CFG.get("question_seconds", 10))
    QUESTION_TYPES = list(SERVER_CFG.get("question_types", ["math:add", "upper"]))

    def _stop(signum, _frame):
        print(f"[server] signal {signum}, shutting down...")
        STOP_EVENT.set()
        try:
            # Nudge any blocking accept by connecting to ourselves
            with socket.create_connection((host, port), timeout=0.2):
                pass
        except Exception:
            pass

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(16)
        print(f"[server] listening on {host}:{port} (players={PLAYERS_REQUIRED}, qsec={QUESTION_SECONDS})")

        # Coordinator thread to run the game flow
        coord = threading.Thread(target=coordinator, daemon=True)
        coord.start()

        threads: List[threading.Thread] = []
        while not STOP_EVENT.is_set():
            try:
                conn, addr = s.accept()
            except OSError:
                break
            if STOP_EVENT.is_set():
                conn.close()
                break
            t = threading.Thread(target=client_thread, args=(conn, addr), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=0.5)
        coord.join(timeout=0.5)
        print("[server] bye.")

if __name__ == "__main__":
    main()
