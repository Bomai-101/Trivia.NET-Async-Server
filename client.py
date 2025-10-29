#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant).

Modes:
  - "you": human input (autograder-style scripted stdin)
  - "auto": built-in solver
  - "ai": call local Ollama-ish model via /api/chat
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import requests  # allowed per assignment

# ------------- debug toggle -------------
DEBUG = False
def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

# ------------- globals / connection state -------------
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

CLIENT_MODE: Optional[str] = None
USERNAME = "player"

EXIT_EVENT = asyncio.Event()

# stdin pump will push lines here
USER_INPUT_QUEUE: asyncio.Queue[str] = asyncio.Queue()

# ai config
OLLAMA_HOST: Optional[str] = None
OLLAMA_PORT: Optional[int] = None
OLLAMA_MODEL: Optional[str] = None

# ------------- encoding helpers -------------
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

# ------------- auto-answer helpers -------------
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
    return str((1 << host_bits) - 2)

def _ip_to_int(a, b, c, d):
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

def auto_answer(question_type: str, short_question: str) -> str:
    qtype = (question_type or "").strip()
    if qtype == "Mathematics":
        return _eval_plus_minus(short_question)
    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_question))
    if qtype == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_question)
    if qtype == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_answer(short_question)
    return ""

# ------------- ai helper (Ollama-style) -------------
async def ask_ollama(short_question: str, qtype: str, tlimit: float) -> str | None:
    """
    Call a local Ollama-like /api/chat endpoint using requests.post
    inside a thread. Return EXACTLY the model's message.content string
    (no cleanup). On failure, return None.
    """
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
        "messages": [
            {
                "role": "user",
                "content": prompt
            }
        ],
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

    result = await asyncio.to_thread(_do_request)
    return result

# optional warmup (not strictly needed for tests, but harmless no-op if no ai)
async def warmup_ollama():
    if not (OLLAMA_HOST and OLLAMA_PORT and OLLAMA_MODEL):
        return
    try:
        _ = await ask_ollama("2 + 2", "Mathematics", 2.0)
    except Exception:
        pass

# ------------- stdin pump -------------
async def pump_stdin() -> None:
    """
    Single place that ever reads sys.stdin.
    Pushes every line (stripped of trailing \r\n) into USER_INPUT_QUEUE.
    Runs forever in its own task.
    """
    loop = asyncio.get_running_loop()
    def _read():
        for line in sys.stdin:
            loop.call_soon_threadsafe(
                USER_INPUT_QUEUE.put_nowait,
                line.rstrip("\r\n")
            )
    await asyncio.to_thread(_read)

# ------------- command handlers -------------
async def cmd_connect(host: str, port: int) -> None:
    if CONN.is_connected():
        return

    # attempt connect, retry briefly like before
    for _ in range(10):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            break
        except Exception:
            await asyncio.sleep(0.2)
    else:
        print("Connection failed")
        # we don't exit the program here because autograder may continue
        return

    CONN.reader, CONN.writer = reader, writer

    # send HI immediately
    await send_line(writer, {
        "message_type": "HI",
        "username": USERNAME
    })

    # spawn background listener for server messages
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        return
    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore
    except Exception:
        pass
    try:
        CONN.writer.close()
        await CONN.writer.wait_closed()
    except Exception:
        pass
    CONN.clear()

async def handle_command(line: str) -> None:
    """
    Handle user-issued commands like CONNECT, DISCONNECT, EXIT.
    This should only be called with full lines popped from USER_INPUT_QUEUE.
    """
    cmd = (line or "").strip()
    up = cmd.upper()
    if not cmd:
        return

    if up == "EXIT":
        # graceful "player quit"
        await cmd_disconnect()
        EXIT_EVENT.set()
        await asyncio.sleep(0.05)
        sys.exit(0)

    if up == "DISCONNECT":
        await cmd_disconnect()
        return

    if up.startswith("CONNECT"):
        # format: CONNECT host:port
        parts = cmd.split()
        if len(parts) >= 2 and ":" in parts[1]:
            host, port_s = parts[1].split(":", 1)
            try:
                await cmd_connect(host, int(port_s))
            except Exception:
                print("[client] usage: CONNECT <host>:<port>")
        else:
            print("[client] usage: CONNECT <host>:<port>")
        return

    # if it's not a known command, in command context we just ignore.
    # (When an answer is expected, QUESTION branch will handle it instead.)

# ------------- server message loop -------------
async def handle_server_messages() -> None:
    """
    Reads messages from the trivia server and reacts.
    Sends ANSWER when needed (depending on mode).
    Prints READY/RESULT/LEADERBOARD/FINISHED to stdout.
    When FINISHED arrives, we set EXIT_EVENT but DO NOT force-close
    the client process immediately; main_async waits on EXIT_EVENT.
    """
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer

    try:
        while True:
            try:
                msg = await read_line_json(reader)
            except ConnectionResetError:
                break
            if msg is None:
                # server closed socket
                break

            dprint(f"[debug rx] {msg}")
            mtype = str(msg.get("message_type", "")).upper()

            if mtype == "READY":
                # server "game is about to begin!"
                info = msg.get("info", "")
                if info:
                    print(info)

            elif mtype == "QUESTION":
                trivia   = msg.get("trivia_question", "")
                qtype    = msg.get("question_type", "")
                short_q  = msg.get("short_question", "")
                tlimit   = msg.get("time_limit", 0)

                if trivia:
                    print(trivia)

                if CLIENT_MODE == "ai":
                    # ask ollama within tlimit
                    try:
                        ai_ans = await asyncio.wait_for(
                            ask_ollama(short_q, qtype, float(tlimit)),
                            timeout=float(tlimit)
                        )
                    except asyncio.TimeoutError:
                        ai_ans = None

                    if ai_ans:
                        await send_line(writer, {
                            "message_type": "ANSWER",
                            "answer": ai_ans
                        })

                elif CLIENT_MODE == "auto":
                    ans = auto_answer(qtype, short_q)
                    if ans:
                        await send_line(writer, {
                            "message_type": "ANSWER",
                            "answer": ans
                        })

                else:  # CLIENT_MODE == "you"
                    # We have at most tlimit seconds to grab ONE line
                    try:
                        user_line = await asyncio.wait_for(
                            USER_INPUT_QUEUE.get(),
                            timeout=float(tlimit)
                        )
                    except asyncio.TimeoutError:
                        user_line = ""

                    upper_line = user_line.strip().upper()

                    # If user actually typed a command here (like EXIT / DISCONNECT),
                    # treat it as a command instead of an ANSWER.
                    if (upper_line == "EXIT" or
                        upper_line == "DISCONNECT" or
                        upper_line.startswith("CONNECT")):
                        await handle_command(user_line)
                    else:
                        ans = user_line.strip()
                        if ans:
                            await send_line(writer, {
                                "message_type": "ANSWER",
                                "answer": ans
                            })

            elif mtype == "RESULT":
                fb = msg.get("feedback", "")
                if fb:
                    print(fb)

            elif mtype == "LEADERBOARD":
                # server may send "state" (string with standings)
                fb = msg.get("feedback", msg.get("state", ""))
                if fb:
                    print(fb)

            elif mtype == "FINISHED":
                # final standings string
                final_txt = msg.get("final_standings", "")
                if final_txt:
                    print(final_txt)
                # spec: after FINISHED the server disconnects clients.
                # we'll exit after this.
                break

            elif mtype == "ERROR":
                print(f"[server] ERROR {msg.get('message')}")

    finally:
        # server is done or connection dropped
        try:
            if CONN.writer:
                CONN.writer.close()
                await CONN.writer.wait_closed()
        except Exception:
            pass
        CONN.clear()
        # signal main_async that we're done
        EXIT_EVENT.set()

# ------------- main_async orchestration -------------
async def main_async():
    dprint(f"[debug] startup mode={CLIENT_MODE} host={OLLAMA_HOST} "
           f"port={(OLLAMA_PORT, OLLAMA_MODEL)} username={USERNAME}")

    # optional warmup (safe no-op if not ai)
    # if CLIENT_MODE == "ai":
    #     await warmup_ollama()
    if not sys.stdin.isatty():

        asyncio.create_task(pump_stdin())
        # read exactly one line from stdin (synchronously but off main loop)
        try:
            first_cmd = await asyncio.wait_for(USER_INPUT_QUEUE.get(), timeout=3.0)
        except asyncio.TimeoutError:
            first_cmd = ""

        if first_cmd:
            await handle_command(first_cmd)
        else:
            # nothing at all -> just exit
            sys.exit(0)

        # wait for game over or EXIT
        await EXIT_EVENT.wait()
        sys.exit(0)

    # start pumping stdin into USER_INPUT_QUEUE
    asyncio.create_task(pump_stdin())

    # First line from stdin should be something like "CONNECT host:port".
    # The autograder feeds it right away.
    # We MUST consume it *once* here (not in a loop), run it as a command,
    # then just wait for EXIT_EVENT (which is set after FINISHED).
    try:
        first_cmd = await asyncio.wait_for(USER_INPUT_QUEUE.get(), timeout=3.0)
    except asyncio.TimeoutError:
        first_cmd = ""

    if first_cmd:
        await handle_command(first_cmd)

    # Now just idle until the game naturally ends or user EXITs.
    await EXIT_EVENT.wait()
    # graceful exit
    sys.exit(0)

# ------------- config loader / main() entrypoint -------------
def load_client_config(path: Path) -> Dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if "client_mode" not in cfg:
            print("client.py: Missing client_mode", file=sys.stderr)
            sys.exit(1)
        return cfg
    except Exception:
        print("client.py: failed to load config", file=sys.stderr)
        sys.exit(1)

def main():
    args = sys.argv[1:]
    if not args or args[0] != "--config" or len(args) < 2:
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg = load_client_config(cfg_path)

    global CLIENT_MODE, USERNAME, OLLAMA_HOST, OLLAMA_PORT, OLLAMA_MODEL
    CLIENT_MODE = cfg.get("client_mode")
    USERNAME    = cfg.get("username", "player")

    if CLIENT_MODE == "ai":
        ollama_cfg   = cfg.get("ollama_config", {}) or {}
        OLLAMA_HOST  = ollama_cfg.get("ollama_host", "localhost")
        OLLAMA_PORT  = int(ollama_cfg.get("ollama_port", 11434))
        OLLAMA_MODEL = ollama_cfg.get("ollama_model", "mistral:latest")
    else:
        OLLAMA_HOST = None
        OLLAMA_PORT = None
        OLLAMA_MODEL = None

    try:
        asyncio.run(main_async())
    except (SystemExit, KeyboardInterrupt):
        pass

if __name__ == "__main__":
    main()
