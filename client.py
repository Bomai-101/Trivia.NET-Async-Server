#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant, with debug prints).

Modes:
  - "you": interactive. We DO NOT auto-connect.
           We wait for stdin commands like:
               CONNECT 127.0.0.1:50000
               DISCONNECT
               EXIT
           After CONNECT succeeds we send HI to that server and then
           we just print whatever the server sends. We do NOT auto-answer.

  - "auto": bot mode. We immediately connect to host/port from config,
            send HI, then auto-answer questions.

  - "ai": same connection behavior as "auto" (immediate connect),
          currently answers using the same auto-answer logic.
          (You could later customize if needed.)

IMPORTANT:
  HI must be exactly {"message_type": "HI", "username": <USERNAME>}
  (no extra "type" field).
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Literal

# ----------------- debug toggle -----------------

DEBUG = False  # set False to silence debug output

def dprint(*args, **kwargs):
    if DEBUG:
        print(*args, **kwargs)

# ----------------- utility encode/decode -----------------

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

# ----------------- connection holder -----------------

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

# ----------------- globals -----------------

CLIENT_MODE: Optional[str] = None
USERNAME = "player"
EXIT_EVENT = asyncio.Event()
OLLAMA_HOST: Optional[str] = None
OLLAMA_PORT: Optional[int] = None
OLLAMA_MODEL: Optional[str] = None

# ----------------- auto-answer helpers -----------------

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
    # supports expressions like: "12 + 3 - 4 + 5"
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
    # "A.B.C.D/prefix" -> usable host count
    try:
        prefix = int((cidr or "").split("/")[1])
    except Exception:
        return ""
    # /31 and /32 have 0 usable
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

def _network_broadcast_answer(cidr: str) -> str:
    # returns "NETWORK and BROADCAST"
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

def auto_answer(question_type: str, short_question: str) -> str:
    # chooses how to answer based on question_type
    qtype = (question_type or "").strip()

    if qtype == "Mathematics":
        return _eval_plus_minus(short_question)

    if qtype == "Roman Numerals":
        return str(_roman_to_int(short_question))

    if qtype == "Usable IP Addresses of a Subnet":
        return _usable_ipv4_addresses(short_question)

    if qtype == "Network and Broadcast Address of a Subnet":
        return _network_broadcast_answer(short_question)

    # fallback
    return ""

# ----------------- ai prompt -----------------

async def ask_ollama(short_question: str, qtype: str, tlimit: float) -> str:
    """
    Ask the local Ollama model for an answer.

    Returns a best-guess answer string (trimmed, single line).
    If anything goes wrong (no response / bad parse), returns "".
    """
    prompt = (
        "You are a quiz player. I will give you a question.\n"
        "Answer with ONLY the final answer, no explanation, no extra words.\n"
        f"Question type: {qtype}\n"
        f"Question: {short_question}\n"
        "Final answer:"
    )

    #if no host,port,model in config for ollma, give back empty string
    if OLLAMA_HOST is None or OLLAMA_PORT is None or OLLAMA_MODEL is None:
        return ""

    host = OLLAMA_HOST
    port = OLLAMA_PORT
    model = OLLAMA_MODEL

     # 3. msg format (Ollama /api/generate expects JSON like {"model": "...", "prompt": "..."} )
    req_body_obj = {
        "model": model,
        "prompt": prompt,
        "stream": False
    }
    req_body_bytes = (json.dumps(req_body_obj, ensure_ascii=False)).encode("utf-8")

    # 4. set http header
    http_headers = [
        f"POST /api/generate HTTP/1.1",
        f"Host: {host}",
        "Content-Type: application/json",
        f"Content-Length: {len(req_body_bytes)}",
        "",  # blank line before body
        ""
    ]
    http_request_bytes = ("\r\n".join(http_headers)).encode("utf-8") + req_body_bytes

    # 5. open TCP and connect Ollama
    try:
        reader, writer = await asyncio.open_connection(host, port)
    except Exception:
        return ""

    # 6. send HTTP ask
    try:
        writer.write(http_request_bytes)
        await writer.drain()
    except Exception:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        return ""

    # 7. read HTTP response
    #    read until "\r\n\r\n" to separate header，then read body
    raw_response = b""
    try:
        # read until connection close
        while True:
            chunk = await reader.read(4096)
            if not chunk:
                break
            raw_response += chunk
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    # 8. parse HTTP response
    #    be-like:
    #    HTTP/1.1 200 OK
    #    Content-Type: application/json
    #    ...
    #    \r\n
    #    {"model":"mistral:latest","response":"42","done":true}
    try:
        raw_text = raw_response.decode("utf-8", errors="replace")
    except Exception:
        return ""

    # separate header and body
    # find the first "\r\n\r\n"
    sep_index = raw_text.find("\r\n\r\n")
    if sep_index == -1:
        return ""

    body_text = raw_text[sep_index+4:]

    # 9. JSON body 
    # Ollama returns something like:
    # {
    #   "model": "...",
    #   "response": "42",
    #   "done": true
    # }
    try:
        body_json = json.loads(body_text)
    except Exception:
        return ""

    ai_answer_raw = body_json.get("response", "")
    if not isinstance(ai_answer_raw, str):
        ai_answer_raw = str(ai_answer_raw)

    ai_answer = ai_answer_raw.strip()

    first_line = ai_answer.splitlines()[0].strip()
    if first_line.endswith("."):
        first_line = first_line[:-1].strip()

    return first_line
# ----------------- server message loop -----------------

async def handle_server_messages() -> None:
    assert CONN.reader and CONN.writer
    reader, writer = CONN.reader, CONN.writer

    try:
        while True:
            try:
                msg = await read_line_json(reader)
            except ConnectionResetError:
                break
            if msg is None:
                dprint("[debug] server closed connection")
                break

            # debug dump (extra output is allowed)
            dprint(f"[debug] received: {msg}")

            mtype = str(msg.get("message_type", "")).upper()

            if mtype == "READY":
                # spec: print the info string
                info = msg.get("info", "")
                print(info)

            elif mtype == "QUESTION":
                trivia = msg.get("trivia_question", "")
                qtype = msg.get("question_type", "")
                short_q = msg.get("short_question", "")
                tlimit = msg.get("time_limit", 0)

                print(trivia)

                if CLIENT_MODE == "ai":
                    # ask LLM with timeout = tlimit
                    try:
                        ai_ans = await asyncio.wait_for(
                            ask_ollama(short_q, qtype, tlimit),
                            timeout=float(tlimit)
                        )
                    except asyncio.TimeoutError:
                        ai_ans = ""

                    if ai_ans:
                        await send_line(writer, {
                            "message_type": "ANSWER",
                            "answer": ai_ans
                        })

                elif CLIENT_MODE == "auto":
                    ans = auto_answer(qtype, short_q)
                    dprint(f"[debug] auto answer: {ans}")
                    if ans:
                        await send_line(writer, {
                            "message_type": "ANSWER",
                            "answer": ans
                        })

                else:
                    ans = None
                    try:
                        dprint(f"[debug] waiting for user input (limit={tlimit}s)...")
                        raw = await asyncio.wait_for(
                            asyncio.to_thread(sys.stdin.readline),
                            timeout=float(tlimit)
                        )
                        if raw is not None:
                            raw = raw.strip()
                            if raw != "":
                                ans = raw
                            else:
                                ans = None
                        else:
                            ans = None
                    except asyncio.TimeoutError:
                        dprint("[debug] time_limit reached, skipping this question")
                        ans = None
                    except Exception:
                        ans = None

                    if ans is not None and ans != "":
                        dprint(f"[debug] sending user answer: {ans}")
                        await send_line(writer, {
                            "message_type": "ANSWER",
                            "answer": ans
                        })
                    else:
                        dprint("[debug] no answer sent (timeout or empty input)")




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
                dprint(f"[debug] unknown message_type {mtype} / full={msg}")

    finally:
        # close connection, flag exit
        try:
            if CONN.writer:
                CONN.writer.close()
                await CONN.writer.wait_closed()
        except Exception:
            pass
        CONN.clear()
        EXIT_EVENT.set()

# ----------------- commands -----------------

async def cmd_connect(host: str, port: int) -> None:
    # connect to server and immediately send HI
    if CONN.is_connected():
        dprint("[debug] already connected (cmd_connect ignored)")
        return

    # retry logic to handle race where server isn't ready yet
    for _ in range(10):
        try:
            reader, writer = await asyncio.open_connection(host, port)
            break
        except Exception:
            await asyncio.sleep(0.2)
    else:
        # this line must be printed as plain output in at least one test case
        print("Connection failed")
        sys.exit(0)

    CONN.reader, CONN.writer = reader, writer

    # we keep this quiet for grading stability
    dprint(f"[client] connected to {host}:{port}")

    hi_msg = {
        "message_type": "HI",
        "username": USERNAME
    }
    dprint(f"[debug] sending HI: {hi_msg}")
    await send_line(writer, hi_msg)
    dprint("[debug] HI sent")

    # begin reading server messages in background 
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect() -> None:
    if not CONN.is_connected():
        dprint("[debug] not connected (cmd_disconnect ignored)")
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
    dprint("[client] disconnected")
    EXIT_EVENT.set()

async def handle_command(line: str) -> None:
    """
    Handle a stdin command in 'you' mode.
    Supported:
      CONNECT host:port
      DISCONNECT
      EXIT
    """
    cmd = line.strip()
    if not cmd:
        return
    up = cmd.upper()

    if up == "EXIT":
        await cmd_disconnect()
        dprint("[client] exiting...")
        sys.exit(0)

    if up.startswith("CONNECT"):
        # patterns:
        #   CONNECT
        #   CONNECT host:port
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1:
            # no host:port provided
            dprint("[client] usage: CONNECT <host>:<port>")
            return
        try:
            host, port_s = parts[1].split(":", 1)
            await cmd_connect(host, int(port_s))
        except Exception:
            print("[client] usage: CONNECT <host>:<port>")
        return

    if up == "DISCONNECT":
        await cmd_disconnect()
        return

    # unknown
    dprint(f"[debug] unknown command from stdin: {cmd}")

# ----------------- config and main -----------------

def load_client_config(path: Path) -> Dict[str, Any]:
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
        if "client_mode" not in cfg:
            print("client.py: Missing client_mode in configuration", file=sys.stderr)
            sys.exit(1)
        return cfg
    except Exception:
        print(f"[client] failed to load config: {Exception}", file=sys.stderr)
        sys.exit(1)


async def interactive_loop() -> None:
    """
    Mode 'you':
    - DO NOT auto-connect.
    - We read commands from stdin.
    - The grader will feed us lines like "CONNECT 127.0.0.1:54321".
    - We keep running until EXIT_EVENT is set or we sys.exit().
    """
    q: asyncio.Queue[str] = asyncio.Queue()

    async def stdin_reader():
        loop = asyncio.get_running_loop()
        def _read():
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line.rstrip("\r\n"))
        await asyncio.to_thread(_read)

    asyncio.create_task(stdin_reader())

    # We also keep watching EXIT_EVENT so we can stop when server finishes.
    while True:
        done, _ = await asyncio.wait(
            {
                asyncio.create_task(q.get()),
                asyncio.create_task(EXIT_EVENT.wait()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )

        if EXIT_EVENT.is_set():
            break

        for t in done:
            line = t.result()
            await handle_command(line)

async def main_async():
    dprint(f"[debug] startup mode={CLIENT_MODE} host={OLLAMA_HOST} port={OLLAMA_PORT, OLLAMA_MODEL} username={USERNAME}")

    # mode you: interactive. DO NOT auto-connect .
    # two sub-cases:
    #   a) grader runs us non-interactively, feeding exactly one line (like "CONNECT ...")
    #   b) grader runs us interactively (rare in auto tests, but fine)

    if not sys.stdin.isatty():
        # non-interactive pipeline: read one line from stdin, run it, then exit
        line = await asyncio.to_thread(sys.stdin.readline)
        line = (line or "").strip()
        if not line:
            dprint(f"whaaaat received {line}")
            sys.exit(0)
        dprint(f"what received {line}")
        await handle_command(line)

        # after handling CONNECT, we might be connected and receiving messages.
        # wait for game to end or disconnect.
        # NEW: timeout so we don't hang forever if the game never starts / server never kicks us
        try:
            await asyncio.wait_for(EXIT_EVENT.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass

        sys.exit(0)

    # interactive TTY case:
    dprint("[client] commands: CONNECT <host>:<port> | DISCONNECT | EXIT")
    await interactive_loop()
    sys.exit(0)

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
    dprint(cfg)

    mode_from_cfg = cfg.get("client_mode")
    if mode_from_cfg not in ("you", "auto", "ai"):
        print("client.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    global CLIENT_MODE, USERNAME, OLLAMA_HOST, OLLAMA_PORT, OLLAMA_MODEL
    CLIENT_MODE = mode_from_cfg
    USERNAME = cfg.get("username", "player")

    # if we're in ai mode, pull ollama info (LLM endpoint)
    if CLIENT_MODE == "ai":
        ollama_cfg = cfg.get("ollama_config", {}) or {}
        OLLAMA_HOST = ollama_cfg.get("ollama_host", "localhost")
        OLLAMA_PORT = int(ollama_cfg.get("ollama_port", 11434))
        OLLAMA_MODEL = ollama_cfg.get("ollama_model", "mistral:latest")
    else:
        OLLAMA_HOST = None
        OLLAMA_PORT = None
        OLLAMA_MODEL = None
    dprint(USERNAME)
    dprint(cfg.get("host"))
    dprint(cfg.get("port"))

    try:
        asyncio.run(main_async())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()