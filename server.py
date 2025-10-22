#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NDJSON-over-TCP server: multi-threaded, extensible handlers, graceful shutdown.
"""

import argparse
import json
import signal
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, IO, Optional, Tuple

STOP_EVENT = threading.Event()

def load_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 5050,
        "backlog": 16,
    }
    if path is None:
        return defaults
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        defaults.update(data or {})
    except Exception as e:
        print(f"[server] Failed to load config {path}: {e}", file=sys.stderr)
    return defaults

def send_json_line(fw: IO[bytes], obj: Dict[str, Any]) -> None:
    fw.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    fw.flush()

def recv_json_line(fr: IO[bytes]) -> Optional[Dict[str, Any]]:
    line = fr.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"action": "error", "error": "invalid_json"}

# --- Example handlers (add your own here) ---
def handle_echo(payload: Any) -> Dict[str, Any]:
    return {"ok": True, "action": "echo", "payload": payload}

def handle_ping(_: Any) -> Dict[str, Any]:
    return {"ok": True, "action": "pong", "ts": time.time()}

def handle_upper(payload: Any) -> Dict[str, Any]:
    s = "" if payload is None else str(payload)
    return {"ok": True, "action": "upper", "payload": s.upper()}

HANDLERS = {
    "echo": handle_echo,
    "ping": handle_ping,
    "upper": handle_upper,
}

def dispatch(msg: Dict[str, Any]) -> Dict[str, Any]:
    action = msg.get("action")
    payload = msg.get("payload")
    if action == "bye":
        return {"ok": True, "action": "bye"}
    fn = HANDLERS.get(action)
    if not fn:
        return {"ok": False, "error": f"unknown_action:{action}"}
    try:
        return fn(payload)
    except Exception as e:
        return {"ok": False, "error": f"handler_error:{type(e).__name__}:{e}"}

def handle_client(sock: socket.socket, addr: Tuple[str, int]) -> None:
    with sock:
        fr = sock.makefile("rb")
        fw = sock.makefile("wb")
        send_json_line(fw, {"ok": True, "action": "hello", "msg": f"connected {addr}"})
        while not STOP_EVENT.is_set():
            msg = recv_json_line(fr)
            if msg is None:
                break
            if msg.get("action") == "BYE":
                send_json_line(fw, {"ok": True, "action": "bye"})
                break
            resp = dispatch(msg)
            send_json_line(fw, resp)

def run_server(cfg: Dict[str, Any]) -> None:
    host = cfg["host"]
    port = int(cfg["port"])
    backlog = int(cfg["backlog"])

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(backlog)
        print(f"[server] listening on {host}:{port}")

        def _stop(signum, _frame):
            print(f"[server] signal {signum}, shutting down...")
            STOP_EVENT.set()
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    pass
            except Exception:
                pass

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

        threads = []
        while not STOP_EVENT.is_set():
            try:
                conn, addr = s.accept()
            except OSError:
                break
            if STOP_EVENT.is_set():
                conn.close()
                break
            print(f"[server] accepted {addr}")
            t = threading.Thread(target=handle_client, args=(conn, addr), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=1.0)
        print("[server] bye.")

def main():
    ap = argparse.ArgumentParser(description="NDJSON TCP server")
    ap.add_argument("--config", type=Path, default=None, help="Path to JSON config")
    ap.add_argument("--host", type=str, help="Override host")
    ap.add_argument("--port", type=int, help="Override port")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg["host"] = args.host
    if args.port:
        cfg["port"] = args.port

    run_server(cfg)

if __name__ == "__main__":
    main()
