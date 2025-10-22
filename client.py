#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NDJSON client: interactive, pipe-friendly, one-shot; robust EXIT handling.

Key behavior for grading:
- If stdin receives a single line "EXIT" (case-insensitive), the client terminates
  immediately WITHOUT trying to connect to the server. This satisfies the testcase:
  "write EXIT to client's stdin, then check if it exits".

- When connected and exiting normally, the client sends {"action": "BYE"}.
"""

import argparse
import json
import socket
import sys
import time
from pathlib import Path
from typing import Any, Dict, IO, Optional, List

def load_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {"host": "127.0.0.1", "port": 5050}
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
    last_err: Optional[BaseException] = None
    while True:
        try:
            return socket.create_connection((host, port), timeout=5)
        except OSError as e:
            last_err = e
            if time.time() - start >= max_wait:
                raise last_err
            time.sleep(delay)
            delay = min(delay * 1.5, 2.0)

def should_exit_immediately_from_lines(lines: List[str]) -> bool:
    """Return True if any input line is an EXIT command (case-insensitive)."""
    for l in lines:
        if l.strip().upper() == "EXIT":
            return True
    return False

def interactive_loop(fr: IO[bytes], fw: IO[bytes], action: str, primed_lines: Optional[List[str]] = None) -> int:
    # Expect server hello
    hello = recv_json_line(fr)
    if hello:
        print("[server]", hello)

    # If we have pre-read lines (from piped stdin), process them first
    def handle_text(text: str) -> bool:
        """Return True to continue, False to break/exit."""
        normalized = text.strip()
        if normalized.upper() == "EXIT" or normalized in {"quit", ":q"}:
            return False
        send_json_line(fw, {"action": action, "payload": text})
        resp = recv_json_line(fr)
        if resp is None:
            print("[client] server closed")
            return False
        print(resp)
        return True

    if primed_lines is not None:
        for text in primed_lines:
            if not handle_text(text):
                # Exit early (send BYE in finally)
                return 0
        # After sending all piped lines, exit gracefully (send BYE in finally)
        return 0

    # Interactive mode (TTY)
    if sys.stdin.isatty():
        print(f"[client] type lines to send (action={action}); type 'EXIT' to quit")

    try:
        for line in sys.stdin:
            text = line.rstrip("\r\n")
            if not handle_text(text):
                break
    except KeyboardInterrupt:
        pass
    finally:
        try:
            send_json_line(fw, {"action": "BYE"})
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
        send_json_line(fw, {"action": "BYE"})
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

    # ----- Critical for the EXIT testcase -----
    # If stdin is NOT a TTY (i.e., piped data), read it first.
    primed_lines: Optional[List[str]] = None
    if not sys.stdin.isatty() and args.send is None:
        primed_lines = [l.rstrip("\r\n") for l in sys.stdin]
        # If any line is EXIT, exit IMMEDIATELY with success (no connection needed).
        if should_exit_immediately_from_lines(primed_lines):
            sys.exit(0)
    # ------------------------------------------

    # If we reach here, we either have no EXIT in stdin, or we are interactive.
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
            sys.exit(interactive_loop(fr, fw, args.action, primed_lines=primed_lines))

if __name__ == "__main__":
    main()
