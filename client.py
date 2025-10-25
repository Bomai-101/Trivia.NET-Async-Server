#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Literal

# -------------------- util io --------------------

def _enc(obj: Dict[str, Any]) -> bytes:
    # spec: each message = one line JSON
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    w.write(_enc(obj))
    await w.drain()

async def read_line_json(r: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    line = await r.readline()
    if not line:
        return None
    return json.loads(line.decode("utf-8"))

# -------------------- connection holder --------------------

class Conn:
    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    def is_connected(self) -> bool:
        return self.reader is not None and self.writer is not None

    def attach(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.reader = r
        self.writer = w

    def clear(self) -> None:
        self.reader = None
        self.writer = None

CONN = Conn()

# -------------------- auto-answer helpers --------------------

def _roman_to_int(s: str) -> int:
    ROMAN = {
        "M": 1000, "CM": 900, "D": 500, "CD": 400,
        "C": 100, "XC": 90, "L": 50, "XL": 40,
        "X": 10, "IX": 9, "V": 5, "IV": 4, "I": 1
    }
    s = s.strip().upper()
    i = 0
    n = 0
    while i < len(s):
        if i+1 < len(s) and s[i:i+2] in ROMAN:
            n += ROMAN[s[i:i+2]]
            i += 2
        else:
            n += ROMAN[s[i]]
            i += 1
    return n

def _eval_plus_minus(expr: str) -> str:
    # "12 + 3 - 4 + 5"
    toks = expr.split()
    if not toks:
        return ""
    total = int(toks[0])
    i = 1
    while i < len(toks) - 1:
        op = toks[i]
        val = int(toks[i+1])
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        i += 2
    return str(total)

def _usable_ipv4_addresses(cidr: str) -> str:
    # A.B.C.D/prefix
    prefix = int(cidr.split("/")[1])
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    usable = (1 << host_bits) - 2
    return str(usable)

def _ip_to_int(a: int, b: int, c: int, d: int) -> int:
    return (a << 24) | (b << 16) | (c << 8) | d

def _int_to_ip(x: int) -> str:
    return f"{(x>>24)&255}.{(x>>16)&255}.{(x>>8)&255}.{x&255}"

def _network_broadcast_pair(cidr: str) -> str:
    # return "NET and BROADCAST" exactly like server expects client ANSWER
    ip_str, pref_str = cidr.split("/")
    prefix = int(pref_str)
    a, b, c, d = map(int, ip_str.split("."))

    ip_int = _ip_to_int(a,b,c,d)
    if prefix == 0:
        mask = 0
    else:
        mask = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF

    net_int = ip_int & mask
    bcast_int = net_int | (~mask & 0xFFFFFFFF)

    net_ip = _int_to_ip(net_int)
    bcast_ip = _int_to_ip(bcast_int)
    return f"{net_ip} and {bcast_ip}"

def auto_answer(question_type: str, short_q: str) -> str:
    qtype = question_type.strip()
    if qtype == "Mathematics":
        return _eval_plus_minus(short_q)
    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_q))
    if qtype == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_q)
    if qtype == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_pair(short_q)
    return ""

# -------------------- core receive loop --------------------

async def play_game_auto(username: str) -> None:
    # auto/ai mode: automatically answer once per QUESTION
    assert CONN.reader and CONN.writer
    r, w = CONN.reader, CONN.writer
    while True:
        msg = await read_line_json(r)
        if msg is None:
            break

        mtype = str(msg.get("message_type", "")).upper()

        if mtype == "READY":
            info = msg.get("info", "")
            print(info)

        elif mtype == "QUESTION":
            qtype = msg.get("question_type", "")
            trivia = msg.get("trivia_question", "")
            short_q = msg.get("short_question", "")

            # print the trivia question text (spec: client prints it)
            print(trivia)

            # send ANSWER automatically
            ans = auto_answer(qtype, short_q)
            await send_line(w, {
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
            final_msg = msg.get("final_standings", "")
            print(final_msg)
            break

        elif mtype == "ERROR":
            err = msg.get("message", "")
            print(f"[server] ERROR {err}")

        else:
            # ignore unknown types
            pass

async def play_game_you(username: str) -> None:
    # "you" mode: no auto-answer.
    # we still print READY/QUESTION/RESULT etc exactly like in auto mode,
    # but we DO NOT calculate answer automatically. We wait for user input
    # after each QUESTION and send that as ANSWER (at most once).
    assert CONN.reader and CONN.writer
    r, w = CONN.reader, CONN.writer

    # We'll create a small queue to read stdin lines asynchronously
    ans_queue: asyncio.Queue[str] = asyncio.Queue()

    async def stdin_task():
        # keep reading lines from stdin and push to queue
        for line in sys.stdin:
            await ans_queue.put(line.rstrip("\r\n"))

    asyncio.create_task(stdin_task())

    while True:
        msg = await read_line_json(r)
        if msg is None:
            break

        mtype = str(msg.get("message_type", "")).upper()

        if mtype == "READY":
            print(msg.get("info", ""))

        elif mtype == "QUESTION":
            print(msg.get("trivia_question", ""))

            # wait for one line from user as their answer
            try:
                user_ans = await asyncio.wait_for(ans_queue.get(), timeout=msg.get("time_limit", 1))
            except asyncio.TimeoutError:
                user_ans = ""  # didn't answer in time

            await send_line(w, {
                "message_type": "ANSWER",
                "answer": user_ans
            })

        elif mtype == "RESULT":
            print(msg.get("feedback", ""))

        elif mtype == "LEADERBOARD":
            print(msg.get("state", ""))

        elif mtype == "FINISHED":
            print(msg.get("final_standings", ""))
            break

        elif mtype == "ERROR":
            print(f"[server] ERROR {msg.get('message','')}")

        else:
            pass

# -------------------- connection helpers --------------------

async def connect_and_hi(host: str, port: int, username: str) -> None:
    r, w = await asyncio.open_connection(host, port)
    CONN.attach(r, w)
    # HI exactly as spec: ONLY message_type and username
    await send_line(w, {
        "message_type": "HI",
        "username": username
    })

async def disconnect() -> None:
    if CONN.is_connected() and CONN.writer:
        try:
            await send_line(CONN.writer, {"message_type": "BYE"})
        except Exception:
            pass
        try:
            CONN.writer.close()
            await CONN.writer.wait_closed()
        except Exception:
            pass
    CONN.clear()

# -------------------- config + main --------------------

def load_config(path: Path) -> Dict[str, Any]:
    # 根据你的要求：不做任何默认值/兜底
    text = path.read_text(encoding="utf-8")
    return json.loads(text)

async def main_async():
    # 要求：必须用 --config <path>
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "--config":
        # 不给额外 fallback，直接退出
        sys.exit(1)

    cfg_path = Path(args[1])
    cfg = load_config(cfg_path)

    mode: Literal["you","auto","ai"] = cfg.get("client_mode")
    username: str = cfg.get("username")
    host: str = cfg.get("host")
    port: int = cfg.get("port")

    # 三种模式处理

    if mode in ("auto","ai"):
        # 立即连
        await connect_and_hi(host, port, username)
        # 自动游戏流程
        await play_game_auto(username)
        await disconnect()
        return

    # mode == "you"
    # 这里不抢跑。我们先读取一行 stdin，格式应该是:
    # CONNECT <host>:<port>
    # 然后连那个 host/port，而不是 config 里的 host/port
    first_line = await asyncio.to_thread(sys.stdin.readline)
    first_line = first_line.strip()

    if first_line.upper().startswith("CONNECT "):
        _, target = first_line.split(maxsplit=1)
        host2, port_s = target.split(":", 1)
        await connect_and_hi(host2, int(port_s), username)
        await play_game_you(username)
        await disconnect()
        return

    # 如果没有 CONNECT，我就直接退出（因为规范里不会给乱的输入）
    await disconnect()
    return

def main():
    try:
        asyncio.run(main_async())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
