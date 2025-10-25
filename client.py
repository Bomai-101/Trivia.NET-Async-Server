#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant, auto mode).

Start:
  python client.py --config <config_path>

The config file should include:
{
  "host": "127.0.0.1",
  "port": 5050,
  "username": "Human",
  "mode": "auto"
}

Protocol:
- On connect: send {"message_type": "HI", "username": <username>}
- Receive READY, QUESTION, RESULT, LEADERBOARD, FINISHED
- When QUESTION arrives:
    if mode == "auto", compute answer and send ANSWER.
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# =========================
# Debug toggle / helper
# =========================
DEBUG = False

def dprint(*args, **kwargs):
    if DEBUG:
        print("[debug]", *args, **kwargs)

# ----------------- helpers shared with server logic -----------------

def _eval_plus_minus(expr: str) -> Optional[str]:
    tokens = expr.split()
    if not tokens:
        return None
    try:
        total = int(tokens[0])
    except Exception:
        return None
    i = 1
    while i < len(tokens) - 1:
        op = tokens[i]
        try:
            val = int(tokens[i + 1])
        except Exception:
            return None
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        else:
            return None
        i += 2
    return str(total)

def _roman_to_int(s: str) -> int:
    ROMAN_MAP = {
        "M": 1000, "CM": 900, "D": 500, "CD": 400,
        "C": 100, "XC": 90, "L": 50, "XL": 40,
        "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
    }
    i = 0
    n = 0
    s = s.strip().upper()
    while i < len(s):
        if i + 1 < len(s) and s[i:i+2] in ROMAN_MAP:
            n += ROMAN_MAP[s[i:i+2]]
            i += 2
        else:
            n += ROMAN_MAP.get(s[i], 0)
            i += 1
    return n

def _usable_ipv4_addresses(cidr: str) -> Optional[str]:
    try:
        prefix = int(cidr.split("/")[1])
    except Exception:
        return None
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    usable = (1 << host_bits) - 2
    return str(usable)

def _ip_to_int(a: int, b: int, c: int, d: int) -> int:
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

def _network_and_broadcast_pair(cidr: str):
    try:
        addr_str, prefix_str = cidr.split("/")
        prefix = int(prefix_str)
        octets = addr_str.split(".")
        if len(octets) != 4:
            return None
        a, b, c, d = [int(x) for x in octets]
    except Exception:
        return None

    if prefix < 0 or prefix > 32:
        return None

    ip_int = _ip_to_int(a, b, c, d)

    if prefix == 0:
        mask = 0
    else:
        mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)

    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return (net_ip, bcast_ip)

def compute_answer(question_type: str, short_question: str) -> Optional[str]:
    qt = question_type.strip()

    if qt == "Mathematics":
        return _eval_plus_minus(short_question)

    if qt == "Roman Numerals":
        return str(_roman_to_int(short_question))

    if qt == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_question)

    if qt == "Network and Broadcast Address of a Subnet":
        pair = _network_and_broadcast_pair(short_question)
        if pair is None:
            return None
        net_ip, bcast_ip = pair
        return f"{net_ip} and {bcast_ip}"

    return None

# ----------------- IO helpers -----------------

def _enc(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    data = _enc(obj)
    dprint("sending", obj)
    writer.write(data)
    try:
        await writer.drain()
    except ConnectionResetError:
        # server closed first; ignore for graceful shutdown
        pass

async def read_line_json(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    line = await reader.readline()
    if not line:
        return None
    try:
        msg = json.loads(line.decode("utf-8"))
        dprint("received", msg)
        return msg
    except json.JSONDecodeError:
        dprint("bad json line", line)
        return None

# ----------------- main client task -----------------

async def client_main(cfg: Dict[str, Any]) -> None:
    host = cfg.get("host", "127.0.0.1")
    port = int(cfg.get("port", 5050))
    username = cfg.get("username", "Human")
    mode = cfg.get("mode", "auto").lower()

    # connect
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        print("Connection failed")
        return

    dprint("connected to", host, port, "as", username)

    # send HI
    await send_line(writer, {
        "message_type": "HI",
        "username": username,
    })

    finished = False

    while not finished:
        msg = await read_line_json(reader)
        if msg is None:
            dprint("server closed connection")
            break

        mtype = str(msg.get("message_type", "")).upper()

        if mtype == "READY":
            info = msg.get("info", "")
            print(info)

        elif mtype == "QUESTION":
            # show question
            trivia = msg.get("trivia_question", "")
            print(trivia)

            # auto-answer if allowed
            if mode == "auto":
                qtype = msg.get("question_type", "")
                short_q = msg.get("short_question", "")
                ans = compute_answer(qtype, short_q)
                if ans is None:
                    ans = ""
                await send_line(writer, {
                    "message_type": "ANSWER",
                    "answer": ans
                })
                dprint("answered with", ans)

        elif mtype == "RESULT":
            correct = msg.get("correct", False)
            feedback = msg.get("feedback", "")
            print(feedback)

        elif mtype == "LEADERBOARD":
            state = msg.get("state", "")
            print(state)

        elif mtype == "FINISHED":
            final_text = msg.get("final_standings", "")
            print(final_text)
            finished = True

        else:
            # ignore unknown types
            dprint("unknown message_type", mtype, msg)

    # be polite and say BYE
    await asyncio.sleep(0.2)
    try:
        await send_line(writer, {
            "message_type": "BYE"
        })
    except Exception:
        pass

    # graceful close delay so server doesn't get reset mid-read
    await asyncio.sleep(0.2)

    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass

# ----------------- config loader / entry -----------------

def load_client_config(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

async def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] != "--config" or len(args) < 2:
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"client.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    cfg = load_client_config(cfg_path)

    await client_main(cfg)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
