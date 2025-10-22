#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Minimal async client for the assignment specs.

Start:
  python client.py --config <config_path>
  # or
  python client.py <config_path>

Config (required, always valid JSON per spec):
{
  "username": <str>,
  "client_mode": "you" | "auto" | "ai",
  "ollama_config": {
    "ollama_host": <str>,
    "ollama_port": <int>,
    "ollama_model": <str>
  }  # only required when client_mode == "ai"
}

Runtime commands (stdin):
  CONNECT <HOSTNAME>:<PORT>   # required to join a game
  ANSWER <text>               # optional; sends an ANSWER message
  DISCONNECT                  # send BYE and exit
  EXIT                        # exit; send BYE if connected

On connect success:
  -> send:  {"type":"HI","username":<username>}

On connect failure:
  print("Connection failed") to STDOUT and exit(1)

Message printing rules:
  READY:        print(msg["info"])
  QUESTION:     print(msg["trivia_question"])
  RESULT:       print(msg["feedback"])
  LEADERBOARD:  print(msg["feedback"])
  FINISHED:     print(msg["final_standings"]) then exit
"""

import json
import socket
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

# ----------------- Framing helpers (NDJSON) -----------------
def _send_json_line(conn: socket.socket, obj: Dict[str, Any]) -> None:
    data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    conn.sendall(data)

def _recv_json_line(conn: socket.socket) -> Optional[Dict[str, Any]]:
    buf = bytearray()
    while True:
        b = conn.recv(1)
        if not b:
            # server closed; treat as EOF
            return None if not buf else json.loads(buf.decode("utf-8"))
        if b == b"\n":
            break
        buf.extend(b)
    return json.loads(buf.decode("utf-8"))

# ----------------- Global state -----------------
ACTIVE_CONN: Optional[socket.socket] = None
ACTIVE_LOCK = threading.Lock()
SHOULD_EXIT = threading.Event()
USERNAME: str = "player"
CLIENT_MODE: str = "you"  # kept for future extension (auto/ai)

# ----------------- Receiver loop -----------------
def _receiver_loop(conn: socket.socket):
    try:
        while not SHOULD_EXIT.is_set():
            msg = _recv_json_line(conn)
            if msg is None:
                # server closed
                break
            _handle_server_message(msg)
    except OSError:
        pass
    finally:
        with ACTIVE_LOCK:
            try:
                conn.close()
            except Exception:
                pass
            if ACTIVE_CONN is conn:
                globals()["ACTIVE_CONN"] = None
        # Server disconnected or FINISHED handled -> exit
        SHOULD_EXIT.set()

def _handle_server_message(m: Dict[str, Any]) -> None:
    mtype = str(m.get("type", "")).upper()

    if mtype == "READY":
        # print "info"
        info = m.get("info", "")
        print(str(info))

    elif mtype == "QUESTION":
        # print "trivia_question"
        tq = m.get("trivia_question", "")
        print(str(tq))

    elif mtype == "RESULT":
        # print "feedback"
        fb = m.get("feedback", "")
        print(str(fb))

    elif mtype == "LEADERBOARD":
        # print "feedback"
        fb = m.get("feedback", "")
        print(str(fb))

    elif mtype == "FINISHED":
        # print "final_standings" then exit
        fs = m.get("final_standings", "")
        print(str(fs))
        SHOULD_EXIT.set()

    # Unknown/other types are ignored per minimal client

# ----------------- Commands -----------------
def _cmd_connect(arg: str) -> None:
    """CONNECT <host>:<port>"""
    parts = arg.split(":", 1)
    if len(parts) != 2:
        print("Usage: CONNECT <HOSTNAME>:<PORT>")
        return
    host, port_s = parts
    try:
        port = int(port_s)
    except ValueError:
        print("Usage: CONNECT <HOSTNAME>:<PORT>")
        return

    try:
        conn = socket.create_connection((host, port), timeout=5)
    except OSError:
        # Spec: print to STDOUT and exit
        print("Connection failed")
        sys.exit(1)

    with ACTIVE_LOCK:
        globals()["ACTIVE_CONN"] = conn

    # Send HI with username
    try:
        _send_json_line(conn, {"type": "HI", "username": USERNAME})
    except OSError:
        print("Connection failed")
        sys.exit(1)

    # Start receiver thread
    threading.Thread(target=_receiver_loop, args=(conn,), daemon=True).start()

def _cmd_answer(text: str) -> None:
    with ACTIVE_LOCK:
        conn = ACTIVE_CONN
    if not conn:
        return
    try:
        _send_json_line(conn, {"type": "ANSWER", "answer": text})
    except OSError:
        pass

def _cmd_disconnect_and_exit() -> None:
    with ACTIVE_LOCK:
        conn = ACTIVE_CONN
    if conn:
        try:
            _send_json_line(conn, {"type": "BYE"})
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass
        with ACTIVE_LOCK:
            if ACTIVE_CONN is conn:
                globals()["ACTIVE_CONN"] = None
    SHOULD_EXIT.set()

# ----------------- Stdin command loop -----------------
def _stdin_loop():
    for line in sys.stdin:
        if SHOULD_EXIT.is_set():
            break
        s = line.strip()
        if not s:
            continue
        up = s.split(maxsplit=1)
        cmd = up[0].upper()
        arg = up[1] if len(up) > 1 else ""

        if cmd == "CONNECT":
            _cmd_connect(arg)
        elif cmd == "ANSWER":
            _cmd_answer(arg)
        elif cmd == "DISCONNECT":
            _cmd_disconnect_and_exit()
            break
        elif cmd == "EXIT":
            _cmd_disconnect_and_exit()
            break
        # else: ignore unknown commands
    # stdin closed -> if still connected, keep running until server finishes
    # (receiver thread will set SHOULD_EXIT on FINISHED or close)

# ----------------- Config loading & startup errors -----------------
def _parse_args_and_load_config() -> Dict[str, Any]:
    # Accept: --config <path>  OR lone <path>
    args = sys.argv[1:]
    cfg_path: Optional[str] = None

    if not args:
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    if args[0] == "--config":
        if len(args) < 2:
            print("client.py: Configuration not provided", file=sys.stderr)
            sys.exit(1)
        cfg_path = args[1]
    else:
        # treat first arg as config path
        cfg_path = args[0]

    p = Path(cfg_path)
    if not p.exists():
        print(f"client.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        # Per spec, JSON is always valid; keep a safety net
        print(f"client.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)
    return cfg

def _init_from_config(cfg: Dict[str, Any]) -> None:
    global USERNAME, CLIENT_MODE
    USERNAME = str(cfg.get("username", "player"))
    CLIENT_MODE = str(cfg.get("client_mode", "you"))

    if CLIENT_MODE == "ai":
        oc = cfg.get("ollama_config")
        if not oc:
            print("client.py: Missing values for Ollama configuration", file=sys.stderr)
            sys.exit(1)
        # (We don't actually call Ollama in this minimal handshake client)

# ----------------- Main -----------------
def main():
    cfg = _parse_args_and_load_config()
    _init_from_config(cfg)

    # Start stdin reader (works for both TTY and piped input)
    t_in = threading.Thread(target=_stdin_loop, daemon=True)
    t_in.start()

    # Wait until exit condition (server FINISHED / DISCONNECT / EXIT)
    try:
        while not SHOULD_EXIT.is_set():
            SHOULD_EXIT.wait(0.2)
    finally:
        # Ensure socket is closed
        with ACTIVE_LOCK:
            if ACTIVE_CONN:
                try:
                    ACTIVE_CONN.close()
                except Exception:
                    pass

if __name__ == "__main__":
    main()
