#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import requests
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

DEBUG = False
def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs, file=sys.stderr, flush=True)

def _enc_line(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(w: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    w.write(_enc_line(obj))
    await w.drain()

async def read_line_json(r: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    line = await r.readline()
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
    def set(self, r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        self.reader = r
        self.writer = w
    async def close(self) -> None:
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
        self.reader = None
        self.writer = None

CONN = Conn()

CLIENT_MODE: Optional[str] = None
USERNAME = "player"

OLLAMA_HOST: Optional[str] = None
OLLAMA_PORT: Optional[int] = None
OLLAMA_MODEL: Optional[str] = None

EXIT_EVENT = asyncio.Event()  # server finished/disconnected
QUIT_EVENT = asyncio.Event()  # client must terminate now

# Two separate queues: one for commands, one for answers.
COMMAND_QUEUE: asyncio.Queue[str] = asyncio.Queue()
ANSWER_QUEUE: asyncio.Queue[str] = asyncio.Queue()

# ---------- auto helpers ----------
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
    return ((a << 24) |
            (b << 16) |
            (c << 8)  |
            d)

def _int_to_ip(n: int) -> str:
    return f"{(n>>24)&255}.{(n>>16)&255}.{(n>>8)&255}.{n&255}"

def _network_broadcast_answer(cidr: str) -> str:
    try:
        addr_str, prefix_str = (cidr or "").split("/")
        prefix = int(prefix_str)
        a, b, c, d = [int(x) for x in addr_str.split(".")]
    except Exception:
        return ""
    if prefix < 0 or prefix > 32:
        return ""
    ip_int = _ip_to_int(a, b, c, d)
    mask = ((0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF) if prefix > 0 else 0
    network_int = ip_int & mask
    broadcast_int = network_int | (~mask & 0xFFFFFFFF)
    return f"{_int_to_ip(network_int)} and {_int_to_ip(broadcast_int)}"

def auto_answer(qtype: str, short_q: str) -> str:
    qtype = (qtype or "").strip()
    if qtype == "Mathematics":
        return _eval_plus_minus(short_q)
    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_q))
    if qtype == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_q)
    if qtype == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_answer(short_q)
    return ""

# ---------- ai helper ----------
async def ask_ollama(short_question: str, qtype: str, tlimit: float) -> Optional[str]:
    if OLLAMA_HOST is None or OLLAMA_PORT is None or OLLAMA_MODEL is None:
        return None

    prompt = (
        "You are a quiz player. I will give you a question.\n"
        "Answer with ONLY the final answer, no explanation, no extra words.\n"
        "Do NOT say anything except the direct answer.\n"
        f"Question type: {qtype}\n"
        f"Question: {short_question}\n"
        "Final answer:"
    )

    req_body_obj = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False
    }

    url = f"http://{OLLAMA_HOST}:{OLLAMA_PORT}/api/chat"

    def _do_request():
        try:
            resp = requests.post(url, json=req_body_obj, timeout=float(tlimit))
            if resp.status_code != 200:
                return None
            body_json = resp.json()
            msg_obj = body_json.get("message")
            if isinstance(msg_obj, dict):
                return str(msg_obj.get("content", ""))
            return None
        except Exception:
            return None

    return await asyncio.to_thread(_do_request)

# ---------- server loop ----------
async def handle_server_messages() -> None:
    assert CONN.reader and CONN.writer
    reader = CONN.reader
    writer = CONN.writer

    try:
        while True:
            try:
                msg = await read_line_json(reader)
            except ConnectionResetError:
                break
            if msg is None:
                break

            mtype = str(msg.get("message_type", "")).upper()
            dprint("[server rx]", msg)

            if mtype == "READY":
                print(msg.get("info", ""), flush=True)

            elif mtype == "QUESTION":
                trivia  = msg.get("trivia_question", "")
                qtype   = msg.get("question_type", "")
                short_q = msg.get("short_question", "")
                tlimit  = msg.get("time_limit", 0)

                print(trivia, flush=True)

                answer_to_send = ""

                if CLIENT_MODE == "ai":
                    try:
                        ai_ans = await asyncio.wait_for(
                            ask_ollama(short_q, qtype, tlimit),
                            timeout=float(tlimit)
                        )
                    except asyncio.TimeoutError:
                        ai_ans = None
                    if ai_ans:
                        answer_to_send = ai_ans

                elif CLIENT_MODE == "auto":
                    answer_to_send = auto_answer(qtype, short_q)

                else:
                    # you-mode: read from ANSWER_QUEUE, not stdin directly
                    try:
                        raw_player = await asyncio.wait_for(
                            ANSWER_QUEUE.get(),
                            timeout=float(tlimit)
                        )
                        ANSWER_QUEUE.task_done()
                        answer_to_send = (raw_player or "").strip()
                    except asyncio.TimeoutError:
                        answer_to_send = ""

                if answer_to_send:
                    await send_line(writer, {"message_type": "ANSWER", "answer": answer_to_send})

            elif mtype == "RESULT":
                fb = msg.get("feedback", "")
                if fb:
                    print(fb, flush=True)

            elif mtype == "LEADERBOARD":
                fb = msg.get("feedback", msg.get("state", ""))
                if fb:
                    print(fb, flush=True)

            elif mtype == "FINISHED":
                print(msg.get("final_standings", ""), flush=True)
                break

            elif mtype == "ERROR":
                em = msg.get("message", "")
                if em:
                    print(f"[server] ERROR {em}", flush=True)

    finally:
        await asyncio.sleep(0.05)
        await CONN.close()
        EXIT_EVENT.set()

# ---------- commands ----------
async def cmd_connect(host: str, port: int) -> None:
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        print("Connection failed", flush=True)
        QUIT_EVENT.set()
        return
    CONN.set(reader, writer)
    await send_line(writer, {"message_type": "HI", "username": USERNAME})
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        QUIT_EVENT.set()
        return
    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore[arg-type]
        await CONN.writer.drain()                              # type: ignore[union-attr]
    except Exception:
        pass
    await CONN.close()
    QUIT_EVENT.set()

async def handle_command(line: str) -> None:
    cmd = (line or "").strip()
    if not cmd:
        return
    up = cmd.upper()

    if up == "EXIT":
        if CONN.is_connected():
            try:
                await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore[arg-type]
                await CONN.writer.drain()                              # type: ignore[union-attr]
            except Exception:
                pass
            await CONN.close()
        QUIT_EVENT.set()
        return

    if up == "DISCONNECT":
        await cmd_disconnect()
        return

    if up.startswith("CONNECT"):
        parts = cmd.split()
        if len(parts) >= 2 and ":" in parts[1]:
            host, port_s = parts[1].split(":", 1)
            try:
                port_i = int(port_s)
            except Exception:
                QUIT_EVENT.set()
                return
            await cmd_connect(host, port_i)
        else:
            QUIT_EVENT.set()
        return

    # non-command lines are answers; they were already routed to ANSWER_QUEUE by the reader
    return

# ---------- stdin routing ----------
def _is_command_line(s: str) -> bool:
    up = (s or "").strip().upper()
    return (up == "EXIT") or (up == "DISCONNECT") or up.startswith("CONNECT ")

async def route_stdin_lines(lines: list[str]) -> None:
    # push lines to the proper queue in order
    for ln in lines:
        if _is_command_line(ln):
            await COMMAND_QUEUE.put(ln)
        else:
            await ANSWER_QUEUE.put(ln)

async def command_worker() -> None:
    while True:
        if QUIT_EVENT.is_set():
            break
        try:
            cmd_line = await asyncio.wait_for(COMMAND_QUEUE.get(), timeout=0.05)
        except asyncio.TimeoutError:
            continue
        await handle_command(cmd_line)
        COMMAND_QUEUE.task_done()
        if QUIT_EVENT.is_set():
            break

async def waiter() -> None:
    await asyncio.wait(
        [QUIT_EVENT.wait(), EXIT_EVENT.wait()],
        return_when=asyncio.FIRST_COMPLETED
    )

# ---------- main ----------
async def main_async() -> None:
    try:
        raw_all = sys.stdin.read()
    except Exception:
        raw_all = ""
    lines = raw_all.splitlines()

    # fast path: single EXIT line exits immediately
    nonempty = [x for x in lines if x.strip() != ""]
    if len(nonempty) == 1 and nonempty[0].strip().upper() == "EXIT":
        return

    if not lines:
        return

    await route_stdin_lines(lines)
    await asyncio.gather(command_worker(), waiter())

def load_client_config(path: Path) -> Dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("client.py: failed to load config", file=sys.stderr, flush=True)
        sys.exit(1)
    if "client_mode" not in cfg:
        print("client.py: Missing client_mode", file=sys.stderr, flush=True)
        sys.exit(1)
    return cfg

def main():
    args = sys.argv[1:]
    if not args or args[0] != "--config" or len(args) < 2:
        print("client.py: Configuration not provided", file=sys.stderr, flush=True)
        sys.exit(1)
    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print("client.py: Configuration not provided", file=sys.stderr, flush=True)
        sys.exit(1)

    cfg = load_client_config(cfg_path)

    global CLIENT_MODE, USERNAME, OLLAMA_HOST, OLLAMA_PORT, OLLAMA_MODEL
    CLIENT_MODE = cfg.get("client_mode")
    USERNAME = cfg.get("username", "player")

    if CLIENT_MODE == "ai":
        ollama_cfg = cfg.get("ollama_config", {}) or {}
        OLLAMA_HOST = ollama_cfg.get("ollama_host", "localhost")
        OLLAMA_PORT = int(ollama_cfg.get("ollama_port", 11434))
        OLLAMA_MODEL = ollama_cfg.get("ollama_model", "mistral:latest")
    else:
        OLLAMA_HOST = None
        OLLAMA_PORT = None
        OLLAMA_MODEL = None

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
