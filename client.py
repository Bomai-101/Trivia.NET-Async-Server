#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant, with debug prints).

Protocol summary:
- Communication is line-delimited JSON (NDJSON) over UTF-8.
- We send:
    HI        -> {"message_type":"HI","type":"HI","username":...}
    ANSWER    -> {"message_type":"ANSWER","answer":...}
    BYE       -> {"message_type":"BYE"}
- We print (as required by spec/testcases):
    READY.info
    QUESTION.trivia_question
    RESULT.feedback
    LEADERBOARD.state
    FINISHED.final_standings

Modes:
- 'auto' : connects automatically, answers automatically
- 'you'  : connects and behaves like a human client (used in most tests)
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Literal

# ---------------- Helpers ----------------

def _enc(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    print(f"[debug] send_line -> {obj}")
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

# ---------------- Connection ----------------

class Conn:
    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    def is_connected(self) -> bool:
        return self.reader is not None and self.writer is not None

    def attach(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer
        print("[debug] Conn.attach(): connection established")

    def clear(self) -> None:
        self.reader = None
        self.writer = None

CONN = Conn()
CLIENT_MODE: Literal["you", "auto", "ai"] = "you"
USERNAME = "player"
EXIT_EVENT = asyncio.Event()

# ---------------- Auto-answer helpers ----------------

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

def _ip_to_int(a: int, b: int, c: int, d: int) -> int:
    return ((a << 24) | (b << 16) | (c << 8) | d)

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
    mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF) if prefix != 0 else 0
    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)
    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return f"{net_ip} and {bcast_ip}"

def auto_answer(question_type: str, short_question: str) -> str:
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

# ---------------- Message handler ----------------

async def game_loop() -> None:
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer

    try:
        while True:
            msg = await read_line_json(reader)
            if msg is None:
                print("[debug] server closed connection")
                break

            print(f"[debug] received: {msg}")
            mtype = str(msg.get("message_type", "")).upper()

            if mtype == "READY":
                print(msg.get("info", ""))

            elif mtype == "QUESTION":
                trivia = msg.get("trivia_question", "")
                print(trivia)
                if CLIENT_MODE == "auto":
                    qtype = msg.get("question_type", "")
                    short_q = msg.get("short_question", "")
                    ans = auto_answer(qtype, short_q)
                    print(f"[debug] auto answering -> {ans}")
                    await send_line(writer, {
                        "message_type": "ANSWER",
                        "answer": ans
                    })

            elif mtype == "RESULT":
                print(msg.get("feedback", ""))

            elif mtype == "LEADERBOARD":
                print(msg.get("state", ""))

            elif mtype == "FINISHED":
                final_text = msg.get("final_standings", "")
                if final_text:
                    print(final_text)
                break

            elif mtype == "ERROR":
                print(f"[debug] server ERROR: {msg.get('message')}")

            else:
                print(f"[debug] unknown message_type={mtype} full={msg}")

    finally:
        try:
            if CONN.writer:
                CONN.writer.close()
                await CONN.writer.wait_closed()
        except Exception:
            pass
        CONN.clear()
        EXIT_EVENT.set()

# ---------------- Connection helpers ----------------

async def connect_and_hi(host: str, port: int, username: str) -> None:
    print(f"[debug] connect_and_hi(): connecting to {host} {port}")
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        print("Connection failed")
        raise SystemExit(1)

    CONN.attach(reader, writer)
    hi_msg = {
        "message_type": "HI",
        "type": "HI",
        "username": username
    }
    print(f"[debug] connect_and_hi(): sending HI -> {hi_msg}")
    await send_line(writer, hi_msg)
    print("[debug] connect_and_hi(): HI sent")

async def graceful_bye() -> None:
    if not CONN.is_connected():
        EXIT_EVENT.set()
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
    EXIT_EVENT.set()

# ---------------- Config ----------------

def load_client_config(path: Optional[Path]) -> Dict[str, Any]:
    if not path:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    print(f"[debug] load_config(): {data}")
    return data

# ---------------- Modes ----------------

async def play_game_auto(host: str, port: int, username: str) -> None:
    await connect_and_hi(host, port, username)
    await game_loop()

async def play_game_you(host: str, port: int, username: str) -> None:
    await connect_and_hi(host, port, username)
    await game_loop()

# ---------------- Main ----------------

async def main_async() -> None:
    global CLIENT_MODE, USERNAME

    args = sys.argv[1:]
    if not args or args[0] != "--config" or len(args) < 2:
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"client.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    cfg = load_client_config(cfg_path)

    host = cfg.get("host")
    port = cfg.get("port")
    USERNAME = cfg.get("username", "player")
    CLIENT_MODE = cfg.get("client_mode", "you")

    print(f"[debug] startup mode={CLIENT_MODE} host={host} port={port} username={USERNAME}")

    if CLIENT_MODE == "auto":
        await play_game_auto(host, port, USERNAME)
    else:
        await play_game_you(host, port, USERNAME)

    await EXIT_EVENT.wait()

def main() -> None:
    try:
        asyncio.run(main_async())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
    except ConnectionResetError:
        pass

if __name__ == "__main__":
    main()
