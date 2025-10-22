#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A minimal, extensible JSON-over-TCP server using NDJSON framing.
每行一条 JSON 消息，便于调试与扩展；支持多个客户端，线程处理。
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

# ---------- Global stop flag for graceful shutdown ----------
STOP_EVENT = threading.Event()


# ---------- Utilities ----------
def load_config(path: Optional[Path]) -> Dict[str, Any]:
    """
    Load JSON config or return defaults.
    配置示例:
    {
      "host": "127.0.0.1",
      "port": 5050,
      "backlog": 16,
      "read_timeout": 300,
      "write_timeout": 300
    }
    """
    default = {
        "host": "127.0.0.1",
        "port": 5050,
        "backlog": 16,
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
        print(f"[server] Failed to load config {path}: {e}", file=sys.stderr)
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


# ---------- Handlers (you can extend here) ----------
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


# ---------- Per-connection thread ----------
def client_thread(sock: socket.socket, addr: Tuple[str, int], cfg: Dict[str, Any]) -> None:
    sock.settimeout(None)  # we wrap with file object; timeouts not needed here
    with sock:
        fr = sock.makefile("rb")
        fw = sock.makefile("wb")
        send_json_line(fw, {"ok": True, "action": "hello", "msg": f"connected {addr}"})

        while not STOP_EVENT.is_set():
            msg = recv_json_line(fr)
            if msg is None:  # client closed
                break
            if msg.get("action") == "bye":
                send_json_line(fw, {"ok": True, "action": "bye"})
                break

            resp = dispatch(msg)
            send_json_line(fw, resp)


# ---------- Main server loop ----------
def run_server(cfg: Dict[str, Any]) -> None:
    host = cfg["host"]
    port = int(cfg["port"])
    backlog = int(cfg["backlog"])

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, port))
        s.listen(backlog)
        print(f"[server] listening on {host}:{port}")

        # Graceful shutdown on Ctrl+C (SIGINT) / SIGTERM
        def _stop(signum, frame):
            print(f"[server] received signal {signum}, shutting down...")
            STOP_EVENT.set()
            try:
                # Kick the accept() by connecting to self (best-effort)
                with socket.create_connection((host, port), timeout=1):
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
            t = threading.Thread(target=client_thread, args=(conn, addr, cfg), daemon=True)
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=1.0)
        print("[server] bye.")


def main():
    ap = argparse.ArgumentParser(description="NDJSON TCP server")
    ap.add_argument("--config", type=Path, help="Path to JSON config file", default=None)
    args = ap.parse_args()
    cfg = load_config(args.config)
    run_server(cfg)


if __name__ == "__main__":
    main()
