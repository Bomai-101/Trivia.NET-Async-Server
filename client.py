#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant).

- Start with: python client.py --config <config_path>
- HI uses {"message_type":"HI","username":...}
- BYE uses {"message_type":"BYE"}
- Reads only "message_type" from server messages
- Prints:
  READY -> info
  QUESTION -> trivia_question
  RESULT -> feedback
  LEADERBOARD -> state
  FINISHED -> final_standings
- Piped stdin fast path: one line; EXIT exits immediately
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

    qtype = question_type.strip()

    if qtype == "Mathematics":
        return _eval_plus_minus(short_question)

    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_question))

    if qtype == "Usable IP Addresses of a Subnet":
        # should be just the number of usable hosts
        return _usable_ipv4_addresses(short_question)

    if qtype == "Network and Broadcast Address of a Subnet":
        # must answer "NETWORK and BROADCAST"
        return _network_broadcast_pair(short_question)

    # fallback
    return ""


# --- helpers for auto mode ---

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
            n += ROMAN_MAP[s[i]]
            i += 1
    return n

def _eval_plus_minus(expr: str) -> str:
    # expr like "12 + 3 - 4 + 5"
    tokens = expr.split()
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
    # "A.B.C.D/prefix"
    try:
        prefix = int(cidr.split("/")[1])
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
    # return "NETWORK and BROADCAST"
    # so we can send exactly that as the ANSWER string
    try:
        addr_str, prefix_str = cidr.split("/")
        prefix = int(prefix_str)
        octets = addr_str.split(".")
        if len(octets) != 4:
            return ""
        a, b, c, d = [int(x) for x in octets]
    except Exception:
        return ""
    if prefix < 0 or prefix > 32:
        return ""

    # convert IP to 32-bit int
    ip_int = _ip_to_int(a, b, c, d)

    # build mask: first prefix bits are 1s, rest 0s
    if prefix == 0:
        mask = 0
    else:
        mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF)

    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(network_int)
    bcast_ip = _int_to_ip(broadcast_int)
    return f"{net_ip} and {bcast_ip}"

#------------------------------------------------------------------
async def handle_server_messages() -> None:
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer
    try:
        while True:
            msg = await read_line_json(reader)
            if msg is None:
                print("[client] server closed")
                break

            raw_type = msg.get("message_type")
            mtype = str(raw_type or "").upper()

            if mtype == "READY":
                info = msg.get("info", "")
                print(info)

            elif mtype == "QUESTION":
                trivia = msg.get("trivia_question", "")
                short_q = msg.get("short_question", "")
                qtype = msg.get("question_type", "")
                print(trivia)
                ans = answer_question_auto(qtype, short_q, CLIENT_MODE)
                await send_line(writer, {"message_type": "ANSWER", "answer": ans})

            elif mtype == "RESULT":
                print(msg.get("feedback", ""))

            elif mtype == "LEADERBOARD":
                print(msg.get("state", ""))

            elif mtype == "FINISHED":
                print(msg.get("final_standings", ""))
                break

            elif mtype == "ERROR":
                print(f"[server] ERROR {msg.get('message')}")

            else:
                # debug:  server sent something we didn't recognise
                print(f"[server] <unknown> {msg}")
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
        print("[client] already connected")
        return
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        print("Connection failed")
        raise SystemExit(1)
    CONN.reader, CONN.writer = reader, writer
    await send_line(writer, {"message_type": "HI", "username": USERNAME})
    print(f"[client] connected to {host}:{port}")
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        print("[client] not connected")
        return
    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore
    except Exception:
        pass
    try:
        CONN.writer.close()  # type: ignore
        await CONN.writer.wait_closed()  # type:  ignore 
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
    print("[client] unknown command")

def load_client_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {"host": "127.0.0.1", "port": 5050, "client_mode": "auto", "username": "player"}
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

    if CLIENT_MODE == "auto":
        await cmd_connect(default_host, default_port)
        await EXIT_EVENT.wait()  
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
