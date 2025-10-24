#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant with debug prints).

- Start with: python client.py --config <config_path>
- Auto mode:
  * immediately CONNECT to server from config
  * send HI (with both "message_type" and "type")
  * sit and answer automatically
- Interactive mode ("you"):
  * behaves like a manual client with CONNECT / DISCONNECT / EXIT

This version prints extra [debug] lines so we can see where it times out.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Literal

def _enc(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    writer.write(_enc(obj))
    await writer.drain()

async def read_line_json(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    line = await reader.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"message_type": "ERROR", "message": "invalid_json"}

class Conn:
    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
    def is_connected(self) -> bool:
        return self.reader is not None and self.writer is not None
    def clear(self) -> None:
        self.reader = None
        self.writer = None

CONN = Conn()
CLIENT_MODE: Literal["you", "auto", "ai"] = "auto"
USERNAME = "player"
EXIT_EVENT = asyncio.Event()

def answer_question_auto(question_type: str, short_question: str, mode: str) -> str:
    if mode != "auto":
        return ""

    qtype = (question_type or "").strip()

    if qtype == "Mathematics":
        return _eval_plus_minus(short_question)

    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_question))

    if qtype == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_question)

    if qtype == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_pair(short_question)

    return ""

# ---- helpers for auto mode ----

def _roman_to_int(s: str) -> int:
    ROMAN_MAP = {
        "M": 1000, "CM": 900, "D": 500, "CD": 400,
        "C": 100, "XC": 90, "L": 50, "XL": 40,
        "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
    }
    i = 0
    n = 0
    s = (s or "").strip().upper()
    while i < len(s):
        if i + 1 < len(s) and s[i:i+2] in ROMAN_MAP:
            n += ROMAN_MAP[s[i:i+2]]
            i += 2
        else:
            n += ROMAN_MAP.get(s[i], 0)
            i += 1
    return n

def _eval_plus_minus(expr: str) -> str:
    tokens = (expr or "").split()
    if not tokens:
        return ""
    try:
        total = int(tokens[0])
    except Exception:
        return ""
    i = 1
    while i < len(tokens) - 1:
        op = tokens[i]
        try:
            val = int(tokens[i+1])
        except Exception:
            return ""
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        else:
            return ""
        i += 2
    return str(total)

def _usable_ipv4_addresses(cidr: str) -> str:
    try:
        prefix = int((cidr or "").split("/")[1])
    except Exception:
        return ""
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    usable = (1 << host_bits) - 2
    return str(usable)

def _ip_to_int(a, b, c, d):
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

def _network_broadcast_pair(cidr: str) -> str:
    try:
        addr_str, prefix_str = (cidr or "").split("/")
        prefix = int(prefix_str)
        octets = addr_str.split(".")
        if len(octets) != 4:
            return ""
        a, b, c, d = [int(x) for x in octets]
    except Exception:
        return ""
    if prefix < 0 or prefix > 32:
        return ""

    ip_int = _ip_to_int(a, b, c, d)

    if prefix == 0:
        mask = 0
    else:
        mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)

    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return f"{net_ip} and {bcast_ip}"

# --------------------------------------------------
async def handle_server_messages() -> None:
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer
    try:
        while True:
            msg = await read_line_json(reader)
            if msg is None:
                print("[debug] server closed connection")
                break

            # log raw message we got
            print(f"[debug] received: {msg}")

            raw_type = msg.get("message_type")
            mtype = str(raw_type or "").upper()

            if mtype == "READY":
                info = msg.get("info", "")
                print(info)

            elif mtype == "QUESTION":
                trivia = msg.get("trivia_question", "")
                short_q = msg.get("short_question", "")
                qtype = msg.get("question_type", "")

                # print the actual question text (spec requires this)
                print(trivia)

                ans = answer_question_auto(qtype, short_q, CLIENT_MODE)
                print(f"[debug] answering with: {ans}")
                await send_line(writer, {
                    "message_type": "ANSWER",
                    "answer": ans
                })

            elif mtype == "RESULT":
                fb = msg.get("feedback", "")
                print(fb)

            elif mtype == "LEADERBOARD":
                state = msg.get("state", "")
                print(state)

            elif mtype == "FINISHED":
                final_standings = msg.get("final_standings", "")
                print(final_standings)
                break

            elif mtype == "ERROR":
                print(f"[server] ERROR {msg.get('message')}")

            else:
                # unknown message from server
                print(f"[debug] unknown message_type {mtype} / full={msg}")
    finally:
        try:
            if CONN.writer:
                CONN.writer.close()
                await CONN.writer.wait_closed()
        except Exception:
            pass
        CONN.clear()
        EXIT_EVENT.set()

async def cmd_connect(host: str, port: int) -> None:
    if CONN.is_connected():
        print("[debug] already connected (cmd_connect ignored)")
        return
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        print("Connection failed")
        raise SystemExit(1)

    CONN.reader, CONN.writer = reader, writer
    print(f"[client] connected to {host}:{port}")
    hi_msg = {
        "message_type": "HI",
        "username": USERNAME
    }
    print(f"[debug] sending HI: {hi_msg}")
    await send_line(writer, hi_msg)
    print("[debug] HI sent")

    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        print("[debug] not connected (cmd_disconnect ignored)")
        return
    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore
    except Exception:
        pass
    try:
        CONN.writer.close()  # type: ignore
        await CONN.writer.wait_closed()  # type: ignore
    except Exception:
        pass
    CONN.clear()
    print("[client] disconnected")
    EXIT_EVENT.set()

async def handle_command(line: str, default_host: str, default_port: int) -> None:
    cmd = line.strip()
    if not cmd:
        return
    up = cmd.upper()

    if up == "EXIT":
        await cmd_disconnect()
        print("[client] exiting...")
        sys.exit(0)

    if up == "CONNECT":
        await cmd_connect(default_host, default_port)
        return

    if up.startswith("CONNECT "):
        try:
            _, target = cmd.split(maxsplit=1)
            host, port_s = target.split(":", 1)
            await cmd_connect(host, int(port_s))
        except Exception:
            print("[client] usage: CONNECT <host>:<port>")
        return

    if up == "DISCONNECT":
        await cmd_disconnect()
        return

    print(f"[debug] unknown command from stdin: {cmd}")

def load_client_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 5050,
        "client_mode": "auto",
        "username": "player"
    }
    if not path:
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults.update(data or {})
    except Exception as e:
        print(f"[client] failed to load config: {e}", file=sys.stderr)
    return defaults

async def main_async():
    global CLIENT_MODE, USERNAME
    args = sys.argv[1:]
    if not args or args[0] != "--config":
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)
    if len(args) < 2:
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"client.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    cfg = load_client_config(cfg_path)
    CLIENT_MODE = cfg.get("client_mode", "auto")
    USERNAME = cfg.get("username", "player")
    default_host = cfg.get("host", "127.0.0.1")
    default_port = int(cfg.get("port", 5050))

    print(f"[debug] startup mode={CLIENT_MODE} host={default_host} port={default_port} username={USERNAME}")

    # auto mode: immediately connect and then  just wait for server msgs
    if CLIENT_MODE == "auto":
        await cmd_connect(default_host, default_port)
        print("[debug] waiting for server messages in auto mode")
        await EXIT_EVENT.wait()
        sys.exit(0)

    # non-auto:
    if not sys.stdin.isatty():
        line = await asyncio.to_thread(sys.stdin.readline)
        line = (line or "").strip()
        if not line:
            sys.exit(0)
        if line.upper() == "EXIT":
            sys.exit(0)
        await handle_command(line, default_host, default_port)
        sys.exit(0)

    print(f"[client] default target: {default_host}:{default_port} (mode={CLIENT_MODE})")
    print("[client] commands: CONNECT <host>:<port> | DISCONNECT | EXIT")

    q: asyncio.Queue[str] = asyncio.Queue()

    async def stdin_reader():
        loop = asyncio.get_running_loop()
        def _read():
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line.rstrip("\r\n"))
        await asyncio.to_thread(_read)

    asyncio.create_task(stdin_reader())

    while True:
        done, _ = await asyncio.wait(
            {asyncio.create_task(q.get()), asyncio.create_task(EXIT_EVENT.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if EXIT_EVENT.is_set():
            break
        for t in done:
            line = t.result()
            await handle_command(line, default_host, default_port)

    sys.exit(0)

def main():
    try:
        asyncio.run(main_async())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
