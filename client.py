import asyncio
import requests
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from contextlib import suppress


DEBUG = False


def dprint(*args, **kwargs):
    """Optional debug printer to stderr when DEBUG is True."""
    if DEBUG:
        print(*args, **kwargs, file=sys.stderr, flush=True)


def _enc(obj: Dict[str, Any]) -> bytes:
    """Encode a JSON-serializable object as UTF-8 NDJSON line."""
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


async def send_line(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    """Send one NDJSON line to the server and drain the writer."""
    writer.write(_enc(obj))
    await writer.drain()


async def read_line_json(reader: asyncio.StreamReader) -> Optional[Dict[str, Any]]:
    """Read one NDJSON line and parse as JSON; return None on EOF."""
    line = await reader.readline()
    if not line:
        return None
    try:
        return json.loads(line.decode("utf-8"))
    except json.JSONDecodeError:
        return {"message_type": "ERROR", "message": "invalid_json"}


class Conn:
    """Holds the current socket connection state."""

    def __init__(self) -> None:
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None

    def is_connected(self) -> bool:
        """Return True if both reader and writer are present."""
        return self.reader is not None and self.writer is not None

    def clear(self) -> None:
        """Clear reader and writer to represent a disconnected state."""
        self.reader = None
        self.writer = None


CONN = Conn()

CLIENT_MODE: Optional[str] = None
USERNAME = "player"

EXIT_EVENT = asyncio.Event()
QUIT_EVENT = asyncio.Event()

CMD_QUEUE: asyncio.Queue[str] = asyncio.Queue()
ANS_QUEUE: asyncio.Queue[str] = asyncio.Queue()
INCOMING_QUEUE: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()

OLLAMA_HOST: Optional[str] = None
OLLAMA_PORT: Optional[int] = None
OLLAMA_MODEL: Optional[str] = None

CURRENT_ANSWER_TASK: Optional[asyncio.Task] = None


def _roman_to_int(s: str) -> int:
    """Convert a Roman numeral string to its integer value."""
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
    """Evaluate a simple space-delimited + / - expression; return result as string."""
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
    """Compute usable host addresses for a CIDR (return '0' for /31 or /32)."""
    try:
        prefix = int((cidr or "").split("/")[1])
    except Exception:
        return ""
    if prefix >= 31:
        return "0"
    host_bits = 32 - prefix
    return str((1 << host_bits) - 2)


def _ip_to_int(a, b, c, d):
    """Pack 4 octets into a 32-bit integer."""
    return ((a << 24) | (b << 16) | (c << 8) | d)


def _int_to_ip(n: int) -> str:
    """Unpack a 32-bit integer into dotted-quad IPv4 string."""
    return f"{(n>>24)&255}.{(n>>16)&255}.{(n>>8)&255}.{n&255}"


def _network_broadcast_answer(cidr: str) -> str:
    """Compute 'network and broadcast' IPv4 for a given CIDR string."""
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
    """Deterministic solver for known question types; may return empty string."""
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
    """Query Ollama using /api/chat and return the model's raw content string or None."""
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
            if resp.status_code != 200:
                return None
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
    """Read messages from the socket and push them into INCOMING_QUEUE."""
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


def _drain_ans_queue() -> None:
    """Empty any pending user answers from ANS_QUEUE."""
    try:
        while True:
            ANS_QUEUE.get_nowait()
            ANS_QUEUE.task_done()
    except asyncio.QueueEmpty:
        pass


async def message_dispatcher(writer: asyncio.StreamWriter) -> None:
    """Consume messages from INCOMING_QUEUE and react according to message_type."""
    global CURRENT_ANSWER_TASK

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

            if CURRENT_ANSWER_TASK and not CURRENT_ANSWER_TASK.done():
                CURRENT_ANSWER_TASK.cancel()
            CURRENT_ANSWER_TASK = None

            async def _submit(ans: Optional[str]) -> None:
                """Send ANSWER if ans is not None, connection is active, and quitting is not set."""
                if ans is None:
                    return
                if not CONN.is_connected() or QUIT_EVENT.is_set():
                    return
                await send_line(writer, {"message_type": "ANSWER", "answer": ans})

            def _drain_local() -> None:
                _drain_ans_queue()

            if CLIENT_MODE == "ai":
                async def _ai_send():
                    try:
                        ai_ans = await asyncio.wait_for(
                            ask_ollama(short_q, qtype, tlimit),
                            timeout=tlimit
                        )
                    except asyncio.TimeoutError:
                        ai_ans = None
                    await _submit(ai_ans)
                CURRENT_ANSWER_TASK = asyncio.create_task(_ai_send())

            elif CLIENT_MODE == "auto":
                async def _auto_send():
                    try:
                        ans = auto_answer(qtype, short_q)
                    except Exception:
                        ans = ""
                    await _submit(ans)
                CURRENT_ANSWER_TASK = asyncio.create_task(_auto_send())

            else:
                _drain_local()

                async def _you_send():
                    try:
                        ans = await asyncio.wait_for(ANS_QUEUE.get(), timeout=tlimit)
                        await _submit(ans)
                    except asyncio.TimeoutError:
                        return
                CURRENT_ANSWER_TASK = asyncio.create_task(_you_send())

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

        elif mtype == "ERROR":
            errm = msg.get("message", "")
            if errm:
                print(f"[server] ERROR {errm}", flush=True)


async def handle_server_messages() -> None:
    """Start socket reader and message dispatcher; exit when either completes."""
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


async def cmd_connect(host: str, port: int) -> None:
    """Open a TCP connection, send HI, and start message handling."""
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
    """Politely disconnect from the server and cancel any answer task."""
    global CURRENT_ANSWER_TASK

    if CURRENT_ANSWER_TASK and not CURRENT_ANSWER_TASK.done():
        CURRENT_ANSWER_TASK.cancel()

    if not CONN.is_connected():
        return

    try:
        await send_line(CONN.writer, {"message_type": "BYE"})  
        await CONN.writer.drain()                              
    except Exception:
        pass

    try:
        CONN.writer.close()                                    
        await CONN.writer.wait_closed()                        
    except Exception:
        pass

    CONN.clear()


async def handle_command(line: str) -> None:
    """Dispatch a single stdin command like CONNECT, DISCONNECT, or EXIT."""
    global CURRENT_ANSWER_TASK

    cmd = (line or "").strip()
    if not cmd:
        return

    up = cmd.upper()

    if up == "EXIT":
        if CURRENT_ANSWER_TASK and not CURRENT_ANSWER_TASK.done():
            CURRENT_ANSWER_TASK.cancel()
        if CONN.is_connected():
            try:
                await send_line(CONN.writer, {"message_type": "BYE"})  
                await CONN.writer.drain()                              
                CONN.writer.close()                                    
                await CONN.writer.wait_closed()                        
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
        else:
            print("[client] usage: CONNECT <host>:<port>", flush=True)
        return

    if up == "DISCONNECT":
        await cmd_disconnect()
        return


def _is_command(text: str) -> bool:
    """Return True if a stdin line is a control command rather than an answer."""
    t = (text or "").strip().upper()
    if not t:
        return False
    if t == "EXIT" or t == "DISCONNECT":
        return True
    if t.startswith("CONNECT "):
        return True
    return False


async def stdin_reader():
    """Register a readable callback for stdin that routes commands/answers into queues."""
    loop = asyncio.get_running_loop()
    done_fut: asyncio.Future[None] = loop.create_future()

    def on_readable():
        line = sys.stdin.readline()
        if line == "":
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

    loop.add_reader(sys.stdin.fileno(), on_readable)

    try:
        await done_fut
    except asyncio.CancelledError:
        try:
            loop.remove_reader(sys.stdin.fileno())
        except Exception:
            pass
        raise


async def router_worker():
    """Consume CMD_QUEUE and execute commands sequentially."""
    while True:
        line = await CMD_QUEUE.get()
        if line is None:
            continue
        if line.strip().upper() == "EXIT":
            await handle_command("EXIT")
            return
        await handle_command(line)


async def interactive_loop(first_line: Optional[str] = None) -> None:
    """Launch stdin reader, command router, and wait until QUIT_EVENT is set."""
    t_stdin = asyncio.create_task(stdin_reader())
    t_router = asyncio.create_task(router_worker())
    t_quit = asyncio.create_task(QUIT_EVENT.wait())

    try:
        await asyncio.wait({t_quit}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (t_quit, t_stdin, t_router):
            if not t.done():
                t.cancel()
        with suppress(asyncio.CancelledError):
            if not t_quit.done():
                await t_quit


async def main_async() -> None:
    """Entrypoint for the async client runtime."""
    await interactive_loop(None)


def load_client_config(path: Path) -> Dict[str, Any]:
    """Load client configuration JSON from file or exit with error."""
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
    """Parse args, load config, set globals, and run the async client."""
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
