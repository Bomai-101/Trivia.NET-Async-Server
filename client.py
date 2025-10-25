#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Literal

# ===================== DEBUG SWITCH =====================
DEBUG = True  # <-- set to False before final submission
# ========================================================

def dprint(*args: Any, **kwargs: Any) -> None:
    """debug print: only prints when DEBUG == True."""
    if DEBUG:
        print("[debug]", *args, **kwargs)


# -------------------- util io --------------------

def _enc(obj: Dict[str, Any]) -> bytes:
    # spec: each message = one line JSON
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    dprint("send_line ->", obj)
    w.write(_enc(obj))
    await w.drain()

async def read_line_json(r: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    line = await r.readline()
    if not line:
        dprint("read_line_json <- EOF")
        return None
    raw = line.decode("utf-8")
    dprint("read_line_json <- raw:", raw.rstrip("\n"))
    return json.loads(raw)

# -------------------- connection holder --------------------

class Conn:
    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional(asyncio.StreamWriter) = None

    def is_connected(self) -> bool:
        return self.reader is not None and self.writer is not None

    def attach(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.reader = r
        self.writer = w
        dprint("Conn.attach(): connection established")

    def clear(self) -> None:
        dprint("Conn.clear(): dropping connection refs")
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
            dprint("roman pair", s[i:i+2], "=>", n)
            i += 2
        else:
            n += ROMAN[s[i]]
            dprint("roman char", s[i], "=>", n)
            i += 1
    return n

def _eval_plus_minus(expr: str) -> str:
    toks = expr.split()
    dprint("_eval_plus_minus toks:", toks)
    if not toks:
        return ""
    total = int(toks[0])
    i = 1
    while i < len(toks) - 1:
        op = toks[i]
        val = int(toks[i+1])
        dprint("math step:", total, op, val)
        if op == "+":
            total += val
        elif op == "-":
            total -= val
        i += 2
    dprint("math result:", total)
    return str(total)

def _usable_ipv4_addresses(cidr: str) -> str:
    # "A.B.C.D/prefix"
    ip_str, pref_str = cidr.split("/")
    prefix = int(pref_str)
    dprint("usable_ipv4", cidr, "prefix=", prefix)
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    usable = (1 << host_bits) - 2
    dprint("host_bits=", host_bits, "usable=", usable)
    return str(usable)

def _ip_to_int(a: int, b: int, c: int, d: int) -> int:
    return (a << 24) | (b << 16) | (c << 8) | d

def _int_to_ip(x: int) -> str:
    return f"{(x>>24)&255}.{(x>>16)&255}.{(x>>8)&255}.{x&255}"

def _network_broadcast_pair(cidr: str) -> str:
    # returns "NETWORK and BROADCAST" which we then send as the ANSWER string
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

    dprint("network/broadcast for", cidr, "=>", net_ip, "and", bcast_ip)
    return f"{net_ip} and {bcast_ip}"

def auto_answer(question_type: str, short_q: str) -> str:
    qt = question_type.strip()
    dprint("auto_answer:", qt, "| short_q:", short_q)
    if qt == "Mathematics":
        return _eval_plus_minus(short_q)
    if qt == "Roman Numerals":
        return str(_roman_to_int(short_q))
    if qt == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_q)
    if qt == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_pair(short_q)
    return ""

# -------------------- core receive loops --------------------

async def play_game_auto(username: str) -> None:
    """Mode auto/ai: answer automatically."""
    assert CONN.reader and CONN.writer
    r, w = CONN.reader, CONN.writer
    dprint("play_game_auto(): start listening")

    while True:
        msg = await read_line_json(r)
        if msg is None:
            dprint("play_game_auto(): server closed")
            break

        dprint("play_game_auto(): got msg:", msg)
        mtype = str(msg.get("message_type", "")).upper()

        if mtype == "READY":
            info = msg.get("info", "")
            print(info)

        elif mtype == "QUESTION":
            qtype = msg.get("question_type", "")
            trivia = msg.get("trivia_question", "")
            short_q = msg.get("short_question", "")

            # print actual question text  (expected output)
            print(trivia)

            # compute + send answer
            ans = auto_answer(qtype, short_q)
            dprint("auto sending ANSWER:", ans)
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
            dprint("play_game_auto(): FINISHED received, end loop")
            break

        elif mtype == "ERROR":
            print(f"[server] ERROR {msg.get('message','')}")
        else:
            dprint("play_game_auto(): unknown message_type", mtype)

async def play_game_you(username: str) -> None:
    """
    Mode you:
    - print READY/QUESTION/RESULT/etc
    - after each QUESTION, wait for user's line (stdin) to send that as ANSWER
    - we do not auto-answer
    """
    assert CONN.reader and CONN.writer
    r, w = CONN.reader, CONN.writer
    dprint("play_game_you(): start listening")

    ans_queue: asyncio.Queue[str] = asyncio.Queue()

    async def stdin_task():
        # continuously read stdin lines into queue
        for line in sys.stdin:
            text = line.rstrip("\r\n")
            dprint("stdin_task captured:", text)
            await ans_queue.put(text)

    asyncio.create_task(stdin_task())

    while True:
        msg = await read_line_json(r)
        if msg is None:
            dprint("play_game_you(): server closed")
            break

        dprint("play_game_you(): got msg:", msg)
        mtype = str(msg.get("message_type", "")).upper()

        if mtype == "READY":
            info = msg.get("info", "")
            print(info)

        elif mtype == "QUESTION":
            trivia = msg.get("trivia_question", "")
            print(trivia)

            # wait for user's manual answer up to the given time_limit
            limit = msg.get("time_limit", 1)
            dprint("waiting user answer up to", limit, "seconds")
            try:
                user_ans = await asyncio.wait_for(ans_queue.get(), timeout=limit)
            except asyncio.TimeoutError:
                dprint("timeout: no manual answer provided")
                user_ans = ""

            dprint("sending manual ANSWER:", user_ans)
            await send_line(w, {
                "message_type": "ANSWER",
                "answer": user_ans
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
            dprint("play_game_you(): FINISHED -> end loop")
            break

        elif mtype == "ERROR":
            print(f"[server] ERROR {msg.get('message','')}")
        else:
            dprint("play_game_you(): unknown message_type", mtype)

# -------------------- connection helpers --------------------

async def connect_and_hi(host: str, port: int, username: str) -> None:
    dprint("connect_and_hi(): connecting to", host, port)
    r, w = await asyncio.open_connection(host, port)
    CONN.attach(r, w)

    hi_msg = {
        "message_type": "HI",
        "username": username
    }
    dprint("connect_and_hi(): sending HI ->", hi_msg)
    await send_line(w, hi_msg)
    dprint("connect_and_hi(): HI sent")

async def disconnect() -> None:
    if CONN.is_connected() and CONN.writer:
        dprint("disconnect(): sending BYE")
        try:
            await send_line(CONN.writer, {"message_type": "BYE"})
        except Exception as e:
            dprint("disconnect(): error sending BYE", e)

        try:
            dprint("disconnect(): closing writer")
            CONN.writer.close()
            await CONN.writer.wait_closed()
        except Exception as e:
            dprint("disconnect(): error closing writer", e)

    CONN.clear()
    dprint("disconnect(): done")

# -------------------- config + main --------------------

def load_config(path: Path) -> Dict[str, Any]:
    # no defaults, trust spec that config exists and is valid
    raw = path.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    dprint("load_config():", cfg)
    return cfg

async def main_async():
    # We follow spec strictly:
    # must be run as: python client.py --config <path>
    args = sys.argv[1:]
    if len(args) != 2 or args[0] != "--config":
        dprint("main_async(): bad args", args)
        sys.exit(1)

    cfg_path = Path(args[1])
    cfg = load_config(cfg_path)

    mode: Literal["you","auto","ai"] = cfg.get("client_mode")
    username: str = cfg.get("username")
    host: str = cfg.get("host")
    port: int = cfg.get("port")

    dprint("startup mode=", mode, "host=", host, "port=", port, "username=", username)

    if mode in ("auto", "ai"):
        # connect immediately to host/port in config
        await connect_and_hi(host, port, username)
        # then just auto-play until FINISHED and disconnect
        await play_game_auto(username)
        await disconnect()
        return

    # mode == "you"
    # we must NOT auto-connect. We wait for stdin like:
    #   CONNECT 127.0.0.1:12345
    dprint("mode=you waiting for CONNECT line on stdin")
    first_line = await asyncio.to_thread(sys.stdin.readline)
    if not first_line:
        dprint("main_async(): stdin EOF before CONNECT")
        await disconnect()
        return

    first_line = first_line.strip()
    dprint("main_async(): got first_line:", first_line)

    # expected "CONNECT host:port"
    if first_line.upper().startswith("CONNECT "):
        _, target = first_line.split(maxsplit=1)
        host2, port_s = target.split(":", 1)
        dprint("main_async(): parsed CONNECT", host2, port_s)

        await connect_and_hi(host2, int(port_s), username)
        await play_game_you(username)
        await disconnect()
        return

    dprint("main_async(): no valid CONNECT, exiting")
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
