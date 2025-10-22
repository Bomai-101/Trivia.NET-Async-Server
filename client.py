#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NDJSON client: interactive, pipe-friendly, one-shot; robust connection retry.
Type 'EXIT', 'exit', 'quit', or ':q' to close (sends 'bye').
"""

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, IO, Optional

def load_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 5050,
    }
    if path is None:
        return defaults
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        defaults.update(data or {})
    except Exception as e:
        print(f"[client] Failed to load config {path}: {e}", file=sys.stderr)
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

def connect_with_retry(host: str, port: int, max_wait: float = 30.0) -> socket.socket:
    start = time.time()
    delay = 0.2
    last_err = None
    while True:
        try:
            return socket.create_connection((host, port), timeout=5)
        except OSError as e:
            last_err = e
            elapsed = time.time() - start
            if elapsed >= max_wait:
                raise last_err
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

def interactive_loop(fr: IO[bytes], fw: IO[bytes], action: str) -> int:
    hello = recv_json_line(fr)
    if hello:
        print("[server]", hello)

    if sys.stdin.isatty():
        print(f"[client] type lines to send (action={action}); type 'EXIT' to quit")

    try:
        for line in sys.stdin:
            text = line.rstrip("\r\n")
            # Accept EXIT in any case; also accept 'quit' and ':q'
            normalized = text.strip()
            if normalized.upper() == "EXIT" or normalized in {"quit", ":q"}:
                break
            send_json_line(fw, {"action": action, "payload": text})
            resp = recv_json_line(fr)
            if resp is None:
                print("[client] server closed")
                break
            print(resp)
    except KeyboardInterrupt:
        pass
    finally:
        try:
            send_json_line(fw, {"action": "bye"})
            _ = recv_json_line(fr)
        except Exception:
            pass
    return 0

def oneshot(fr: IO[bytes], fw: IO[bytes], action: str, payload: str) -> int:
    _ = recv_json_line(fr)  # hello
    send_json_line(fw, {"action": action, "payload": payload})
    resp = recv_json_line(fr)
    if resp:
        print(resp)
    try:
        send_json_line(fw, {"action": "bye"})
        _ = recv_json_line(fr)
    except Exception:
        pass
    return 0

def main():
    ap = argparse.ArgumentParser(description="NDJSON TCP client")
    ap.add_argument("--config", type=Path, default=None, help="Path to JSON config")
    ap.add_argument("--host", type=str, help="Override host")
    ap.add_argument("--port", type=int, help="Override port")
    ap.add_argument("--action", type=str, default="echo", help="Action name (echo|upper|ping|...)")
    ap.add_argument("--send", type=str, help="Send a single payload then exit")
    ap.add_argument("--no-retry", action="store_true", help="Do not retry connection")
    args = ap.parse_args()

    cfg = load_config(args.config)
    host = args.host or cfg["host"]
    port = int(args.port or cfg["port"])

    try:
        if args.no_retry:
            sock = socket.create_connection((host, port), timeout=5)
        else:
            sock = connect_with_retry(host, port, max_wait=30.0)
    except OSError as e:
        print(f"[client] could not connect to {host}:{port} - {e}", file=sys.stderr)
        sys.exit(1)

    with sock:
        fr = sock.makefile("rb")
        fw = sock.makefile("wb")
        if args.send is not None:
            sys.exit(oneshot(fr, fw, args.action, args.send))
        else:
            sys.exit(interactive_loop(fr, fw, args.action))

if __name__ == "__main__":
    main()
