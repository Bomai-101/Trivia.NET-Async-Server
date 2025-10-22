#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A minimal NDJSON client for the companion server.
支持三种使用方式：
1) 交互：直接运行后逐行输入；每行作为 payload 发送（默认 action=echo）
2) 管道：echo hello | python client.py  （从 stdin 读多行发送）
3) 单次：python client.py --send "hello world" --action upper
"""

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any, Dict, IO, Optional


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    default = {
        "host": "127.0.0.1",
        "port": 5050,
        "read_timeout": 300,
        "write_timeout": 300,
    }
    if path is None:
        return default
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        default.update(data or {})
        return default
    except Exception as e:
        print(f"[client] Failed to load config {path}: {e}", file=sys.stderr)
        return default


def send_json_line(fw: IO[bytes], obj: Dict[str, Any]) -> None:
    line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
    fw.write(line)
    fw.flush()


def recv_json_line(fr: IO[bytes]) -> Optional[Dict[str, Any]]:
    line = fr.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"action": "error", "error": "invalid_json", "raw": line.decode("utf-8", "ignore")}


def interactive_loop(fr: IO[bytes], fw: IO[bytes], action: str) -> int:
    hello = recv_json_line(fr)
    if hello:
        print("[server]", hello)

    if sys.stdin.isatty():
        print(f"[client] type lines to send as payload (action={action}); Ctrl+C to quit")

    try:
        for line in sys.stdin:
            line = line.rstrip("\r\n")
            send_json_line(fw, {"action": action, "payload": line})
            resp = recv_json_line(fr)
            if resp is None:
                print("[client] server closed")
                break
            print(resp)
    except KeyboardInterrupt:
        pass
    finally:
        send_json_line(fw, {"action": "bye"})
        bye = recv_json_line(fr)
        if bye:
            print("[server]", bye)
    return 0


def oneshot(fr: IO[bytes], fw: IO[bytes], action: str, payload: str) -> int:
    hello = recv_json_line(fr)
    if hello:
        print("[server]", hello)
    send_json_line(fw, {"action": action, "payload": payload})
    resp = recv_json_line(fr)
    if resp:
        print(resp)
    send_json_line(fw, {"action": "bye"})
    _ = recv_json_line(fr)
    return 0


def main():
    ap = argparse.ArgumentParser(description="NDJSON TCP client")
    ap.add_argument("--config", type=Path, help="Path to JSON config file", default=None)
    ap.add_argument("--action", type=str, default="echo", help="Action name (echo|upper|ping|...)")
    ap.add_argument("--send", type=str, help="Send a single payload then exit")
    args = ap.parse_args()

    cfg = load_config(args.config)
    host = cfg["host"]
    port = int(cfg["port"])

    with socket.create_connection((host, port), timeout=10) as sock:
        fr = sock.makefile("rb")
        fw = sock.makefile("wb")
        if args.send is not None:
            return sys.exit(oneshot(fr, fw, args.action, args.send))
        else:
            return sys.exit(interactive_loop(fr, fw, args.action))


if __name__ == "__main__":
    main()
