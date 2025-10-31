#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import requests
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import suppress

DEBUG = False
def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs, file=sys.stderr, flush=True)

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

CLIENT_MODE: Optional[str] = None
USERNAME = "player"

EXIT_EVENT = asyncio.Event()
QUIT_EVENT = asyncio.Event()

# NEW: split queues — commands vs answers
CMD_QUEUE: asyncio.Queue[str] = asyncio.Queue()
ANS_QUEUE: asyncio.Queue[str] = asyncio.Queue()
INCOMING_QUEUE: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
OLLAMA_HOST: Optional[str] = None
OLLAMA_PORT: Optional[int] = None
OLLAMA_MODEL: Optional[str] = None

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
    return ((a << 24) | (b << 16) | (c << 8) | d)

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
            resp = requests.post(url, json=req_body_obj, timeout=max(1.5, float(tlimit) + 0.5))
            print(f"resp.status_code: {resp.status_code}")
            if resp.status_code != 200:
                return None
            print(f"resp:{resp}")
            body = resp.json()

            if isinstance(body.get("message"), dict):
                return body["message"].get("content", "")
            msgs = body.get("messages")
            if isinstance(msgs, list) and msgs and isinstance(msgs[-1], dict):
                return msgs[-1].get("content", "")
            return None
        except Exception:
            return None

    return await asyncio.to_thread(_do_request)



async def socket_reader_task(reader: asyncio.StreamReader) -> None:
    """Keep reading from socket and put each decoded JSON into INCOMING_QUEUE."""
    try:
        while True:
            msg = await read_line_json(reader)
            if msg is None:
                break
            await INCOMING_QUEUE.put(msg)
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        await INCOMING_QUEUE.put({"message_type": "__CLOSED__"})


async def message_dispatcher(writer: asyncio.StreamWriter) -> None:
    """Consume messages from INCOMING_QUEUE and handle them in order."""
    while True:
        msg = await INCOMING_QUEUE.get()
        mtype = str(msg.get("message_type", "")).upper()
        if mtype == "__CLOSED__":
            break

        if mtype == "READY":
            print(msg.get("info", ""), flush=True)

        elif mtype == "QUESTION":
            trivia = msg.get("trivia_question", "")
            qtype = msg.get("question_type", "")
            short_q = msg.get("short_question", "")
            tlimit = float(msg.get("time_limit", 0) or 0)
            print(trivia, flush=True)

            # auto/ai 
            if CLIENT_MODE in {"auto", "ai"}:
                async def _auto_send():
                    try:
                        if CLIENT_MODE == "ai":
                            try:
                                ai_ans = await asyncio.wait_for(
                                    ask_ollama(short_q, qtype, tlimit),
                                    timeout=tlimit
                                )
                                ans = ai_ans
                            except asyncio.TimeoutError:
                                ans = None
                                    
                            if ai_ans is not None:
                                await send_line(writer, {"message_type": "ANSWER", "answer": ai_ans})
                            return
                        
                        ans = auto_answer(qtype, short_q)# or "Not generated"
                        if ans:
                            await send_line(writer, {"message_type": "ANSWER", "answer": ans})
                    except Exception:
                        pass
                asyncio.create_task(_auto_send())

            # you 
            else:
                try:
                    ans = await asyncio.wait_for(ANS_QUEUE.get(), timeout=tlimit)
                    ans = (ans or "").strip()
                except asyncio.TimeoutError:
                    ans = ""
                if ans:
                    await send_line(writer, {"message_type": "ANSWER", "answer": ans})

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
            #QUIT_EVENT.set()
            #break

        elif mtype == "ERROR":
            errm = msg.get("message", "")
            if errm:
                print(f"[server] ERROR {errm}", flush=True)


async def handle_server_messages() -> None:
    """Spawn reader + dispatcher so reading and processing never block each other."""
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer
    try:
        t_reader = asyncio.create_task(socket_reader_task(reader))
        t_dispatcher = asyncio.create_task(message_dispatcher(writer))
        await asyncio.wait({t_reader, t_dispatcher}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        try:
            if CONN.writer:
                CONN.writer.close()
                await CONN.writer.wait_closed()
        except Exception:
            pass
        CONN.clear()
        #EXIT_EVENT.set()

async def cmd_connect(host: str, port: int) -> None:
    if CONN.is_connected():
        return
    global INCOMING_QUEUE
    INCOMING_QUEUE = asyncio.Queue()
    for _ in range(10):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            break
        except Exception:
            await asyncio.sleep(0.2)
    else:
        print("Connection failed", flush=True)
        QUIT_EVENT.set()
        return

    CONN.reader, CONN.writer = reader, writer
    await send_line(writer, {"message_type": "HI", "username": USERNAME})
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        #QUIT_EVENT.set()
        return
    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore
        await CONN.writer.drain()                              # type: ignore
    except Exception:
        pass
    try:
        CONN.writer.close()                                    # type: ignore
        await CONN.writer.wait_closed()                        # type: ignore
    except Exception:
        pass
    CONN.clear()
    #QUIT_EVENT.set()

async def handle_command(line: str) -> None:
    cmd = (line or "").strip()
    if not cmd:
        return
    up = cmd.upper()

    if up == "EXIT":
        if CONN.is_connected():
            try:
                await send_line(CONN.writer, {"message_type": "BYE"})  # type: ignore
                await CONN.writer.drain()                              # type: ignore
                CONN.writer.close()                                    # type: ignore
                await CONN.writer.wait_closed()                        # type: ignore
            except Exception:
                pass
            CONN.clear()
        QUIT_EVENT.set()
        return

    if up.startswith("CONNECT"):
        parts = cmd.split()
        if len(parts) >= 2 and ":" in parts[1]:
            host, port_s = parts[1].split(":", 1)
            try:
                await cmd_connect(host, int(port_s))
            except Exception:
                print("Connection failed", flush=True)
                #QUIT_EVENT.set()
        else:
            print("[client] usage: CONNECT <host>:<port>", flush=True)
            #QUIT_EVENT.set()
        return

    if up == "DISCONNECT":
        await cmd_disconnect()
        return

def _is_command(text: str) -> bool:
    t = (text or "").strip().upper()
    if not t:
        return False
    if t == "EXIT" or t == "DISCONNECT":
        return True
    if t.startswith("CONNECT "):
        return True
    return False

async def stdin_reader():
    loop = asyncio.get_running_loop()
    done_fut: asyncio.Future[None] = loop.create_future()

    def on_readable():
        line = sys.stdin.readline()
        if line == "":
            # EOF: stop watching
            try:
                loop.remove_reader(sys.stdin.fileno())
            except Exception:
                pass
            if not done_fut.done():
                done_fut.set_result(None)
            return

        line = line.rstrip("\r\n")
        if _is_command(line):
            CMD_QUEUE.put_nowait(line)
        else:
            ANS_QUEUE.put_nowait(line)

    # register OS-level readable callback (Unix)
    loop.add_reader(sys.stdin.fileno(), on_readable)

    try:
        await done_fut  # wait until EOF or we cancel this task
    except asyncio.CancelledError:
        # clean up the reader on cancel (e.g., after EXIT)
        try:
            loop.remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        raise

async def router_worker():
    while True:
        line = await CMD_QUEUE.get()
        if line is None:
            continue
        if line.strip().upper() == "EXIT":
            await handle_command("EXIT")
            return
        await handle_command(line)

async def interactive_loop(first_line: Optional[str] = None) -> None:
    # no priming; stdin_reader  will consume all incoming lines
    t_stdin = asyncio.create_task(stdin_reader())
    t_router = asyncio.create_task(router_worker())
    t_quit = asyncio.create_task(QUIT_EVENT.wait())
    t_exit = asyncio.create_task(EXIT_EVENT.wait())

    try:
        await asyncio.wait({t_quit, t_exit}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t_quit, t_exit, t_stdin, t_router):
            if not t.done():
                t.cancel()
        # best-effort waits; stdin_reader cancel will remove_reader and return quickly
        with suppress(asyncio.CancelledError):
            if not t_quit.done():
                await t_quit
        with suppress(asyncio.CancelledError):
            if not t_exit.done():
                await t_exit

async def main_async() -> None:
    await interactive_loop(None)

def load_client_config(path: Path) -> Dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        print("client.py: failed to load config", file=sys.stderr)
        sys.exit(1)
    if "client_mode" not in cfg:
        print("client.py: Missing client_mode", file=sys.stderr)
        sys.exit(1)
    return cfg

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
    USERNAME = cfg.get("username", "player")

    if CLIENT_MODE == "ai":
        ocfg = cfg.get("ollama_config", {}) or {}
        OLLAMA_HOST = ocfg.get("ollama_host", "localhost")
        OLLAMA_PORT = int(ocfg.get("ollama_port", 11434))
        OLLAMA_MODEL = ocfg.get("ollama_model", "mistral:latest")
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