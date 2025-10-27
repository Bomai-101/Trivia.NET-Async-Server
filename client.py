#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Async NDJSON client (spec-compliant, with debug prints).

Modes:
  - "you": interactive
  - "auto": bot
  - "ai": uses local Ollama-like model via /api/chat
"""

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# ----------------- debug toggle -----------------
DEBUG = True
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
    i = 0; n = 0; s = (s or "").strip().upper()
    while i < len(s):
        if i + 1 < len(s) and s[i:i+2] in ROMAN_MAP:
            n += ROMAN_MAP[s[i:i+2]]; i += 2
        else:
            n += ROMAN_MAP.get(s[i], 0); i += 1
    return n

def _eval_plus_minus(expr: str) -> str:
    tokens = (expr or "").split()
    if not tokens: return ""
    try: total = int(tokens[0])
    except Exception: return ""
    i = 1
    while i < len(tokens) - 1:
        op = tokens[i]
        try: val = int(tokens[i+1])
        except Exception: return ""
        if op == "+": total += val
        elif op == "-": total -= val
        else: return ""
        i += 2
    return str(total)

def _usable_ipv4_addresses(cidr: str) -> str:
    try: prefix = int((cidr or "").split("/")[1])
    except Exception: return ""
    if prefix >= 31: return "0"
    host_bits = 32 - prefix
    return str((1 << host_bits) - 2)

def _ip_to_int(a,b,c,d): return ((a<<24)|(b<<16)|(c<<8)|d)
def _int_to_ip(n:int)->str:
    return f"{(n>>24)&255}.{(n>>16)&255}.{(n>>8)&255}.{n&255}"

def _network_broadcast_answer(cidr:str)->str:
    try:
        addr_str,prefix_str=(cidr or "").split("/")
        prefix=int(prefix_str)
        a,b,c,d=[int(x) for x in addr_str.split(".")]
    except Exception: return ""
    if prefix<0 or prefix>32: return ""
    ip_int=_ip_to_int(a,b,c,d)
    mask=((0xFFFFFFFF<<(32-prefix))&0xFFFFFFFF) if prefix>0 else 0
    network_int=ip_int&mask
    broadcast_int=network_int|(~mask&0xFFFFFFFF)
    return f"{_int_to_ip(network_int)} and {_int_to_ip(broadcast_int)}"

def auto_answer(question_type:str, short_question:str)->str:
    qtype=(question_type or "").strip()
    if qtype=="Mathematics": return _eval_plus_minus(short_question)
    if qtype=="Roman Numerals": return str(_roman_to_int(short_question))
    if qtype=="Usable IP Addresses of a Subnet": return _usable_ipv4_addresses(short_question)
    if qtype=="Network and Broadcast Address of a Subnet": return _network_broadcast_answer(short_question)
    return ""

# ----------------- ai prompt (Ollama chat-style) -----------------
async def ask_ollama(short_question:str, qtype:str, tlimit:float)->str:
    prompt=(f"You are a quiz player. I will give you a question.\n"
            f"Answer with ONLY the final answer, no explanation, no extra words.\n"
            f"Question type: {qtype}\n"
            f"Question: {short_question}\n"
            f"Final answer:")

    if OLLAMA_HOST is None or OLLAMA_PORT is None or OLLAMA_MODEL is None:
        return ""

    req_body_obj={"model":OLLAMA_MODEL,"messages":[{"role":"user","content":prompt}],"stream":False}
    req_body_bytes=json.dumps(req_body_obj,ensure_ascii=False).encode("utf-8")
    headers=["POST /api/chat HTTP/1.1",f"Host: {OLLAMA_HOST}","Content-Type: application/json",f"Content-Length: {len(req_body_bytes)}","",""]
    raw_request=("\r\n".join(headers)).encode("utf-8")+req_body_bytes

    try:
        reader,writer=await asyncio.open_connection(OLLAMA_HOST,OLLAMA_PORT)
    except Exception:
        dprint("[ollama] connect failed"); return ""

    try:
        writer.write(raw_request); await writer.drain()
    except Exception:
        try: writer.close(); await writer.wait_closed()
        except Exception: pass
        dprint("[ollama] send failed"); return ""

    def _extract_from_partial_minimal(http_text:str)->Optional[str]:
        sep_idx=http_text.find("\r\n\r\n")
        if sep_idx==-1: return None
        body=http_text[sep_idx+4:]
        msg_pos=body.rfind("\"message\"")
        if msg_pos==-1: return None
        c_key="\"content\""
        c_pos=body.find(c_key+":",msg_pos)
        if c_pos==-1: return None
        q_start=body.find("\"",c_pos+len(c_key)+1)
        if q_start==-1: return None
        q_end=body.find("\"",q_start+1)
        if q_end==-1: return None
        ans=body[q_start+1:q_end].strip()
        if "\n" in ans: ans=ans.splitlines()[0].strip()
        if ans.endswith("."): ans=ans[:-1].strip()
        return ans or None

    accum=b""; early_answer=None
    t_start=asyncio.get_running_loop().time()
    while True:
        now=asyncio.get_running_loop().time()
        remaining=tlimit-(now-t_start)
        if remaining<=0: break
        try:
            chunk=await asyncio.wait_for(reader.read(1024),timeout=min(0.25,remaining))
        except asyncio.TimeoutError:
            continue
        if not chunk: break
        accum+=chunk
        try: text_so_far=accum.decode("utf-8",errors="replace")
        except Exception: text_so_far=""
        maybe=_extract_from_partial_minimal(text_so_far)
        if maybe:
            early_answer=maybe
            dprint(f"[ollama] EARLY HIT(min)={early_answer!r}")
            break

    try: writer.close(); await writer.wait_closed()
    except Exception: pass

    if early_answer:
        ans=early_answer.strip()
        if "\n" in ans: ans=ans.splitlines()[0].strip()
        if ans.endswith("."): ans=ans[:-1].strip()
        dprint(f"[ollama] final(min-early)={ans!r}")
        return ans

    try: raw_text=accum.decode("utf-8",errors="replace")
    except Exception: return ""
    sep_idx=raw_text.find("\r\n\r\n")
    if sep_idx==-1: return ""
    body_text=raw_text[sep_idx+4:]
    candidate=""
    for line in body_text.strip().splitlines():
        l=line.strip()
        if l.startswith("{") and l.endswith("}"): candidate=l
    if candidate=="": candidate=body_text.strip()
    try: body_json=json.loads(candidate)
    except Exception:
        dprint("[ollama] json parse failed (min-final)")
        return ""
    msg_obj=body_json.get("message")
    ai_answer_raw=msg_obj.get("content","") if isinstance(msg_obj,dict) else ""
    ans=str(ai_answer_raw).strip()
    if "\n" in ans: ans=ans.splitlines()[0].strip()
    if ans.endswith("."): ans=ans[:-1].strip()
    dprint(f"[ollama] final(min-fallback)={ans!r}")
    return ans

# ----------------- warmup (NEW) -----------------
async def warmup_ollama():
    """Preload the Ollama model before the first real question."""
    if not (OLLAMA_HOST and OLLAMA_PORT and OLLAMA_MODEL):
        return
    dprint("[warmup] starting Ollama warmup...")
    try:
        _ = await asyncio.wait_for(
            ask_ollama("2 + 2", "Mathematics", tlimit=1.0),
            timeout=1.0
        )
        dprint("[warmup] completed successfully.")
    except Exception:
        dprint("[warmup] failed or timed out (ignored).")

# ----------------- server message loop -----------------
async def handle_server_messages()->None:
    assert CONN.reader and CONN.writer
    reader,writer=CONN.reader,CONN.writer
    try:
        while True:
            try: msg=await read_line_json(reader)
            except ConnectionResetError: break
            if msg is None:
                dprint("[debug] server closed connection"); break
            dprint(f"[debug rx] {msg}")
            mtype=str(msg.get("message_type","")).upper()
            if mtype=="READY":
                print(msg.get("info",""))
            elif mtype=="QUESTION":
                trivia=msg.get("trivia_question","")
                qtype=msg.get("question_type","")
                short_q=msg.get("short_question","")
                tlimit=msg.get("time_limit",0)
                print(trivia)
                if CLIENT_MODE=="ai":
                    try:
                        ai_ans=await asyncio.wait_for(
                            ask_ollama(short_q,qtype,tlimit),
                            timeout=float(tlimit))
                    except asyncio.TimeoutError:
                        ai_ans=""; dprint(f"[debug ai timeout] after {tlimit}s")
                    dprint(f"[debug ai_ans before send] {ai_ans!r}")
                    if ai_ans:
                        await send_line(writer,{"message_type":"ANSWER","answer":ai_ans})
                        dprint(f"[debug sent ANSWER {ai_ans!r}]")
                    else:
                        dprint("Error 404: Answer not found")
                        dprint("[debug no ANSWER sent for this question]")
                elif CLIENT_MODE=="auto":
                    ans=auto_answer(qtype,short_q)
                    if ans:
                        await send_line(writer,{"message_type":"ANSWER","answer":ans})
                else:
                    try:
                        raw=await asyncio.wait_for(asyncio.to_thread(sys.stdin.readline),timeout=float(tlimit))
                        ans=(raw or "").strip()
                    except asyncio.TimeoutError:
                        ans=""
                    if ans:
                        await send_line(writer,{"message_type":"ANSWER","answer":ans})
            elif mtype=="RESULT":
                fb=msg.get("feedback",""); 
                if fb: print(fb)
                dprint(f"[debug RESULT] {msg}")
            elif mtype=="LEADERBOARD":
                fb=msg.get("feedback",msg.get("state","")); 
                if fb: print(fb)
                dprint(f"[debug LEADERBOARD] {msg}")
            elif mtype=="FINISHED":
                print(msg.get("final_standings","")); break
            elif mtype=="ERROR":
                print(f"[server] ERROR {msg.get('message')}")
    finally:
        try:
            if CONN.writer:
                CONN.writer.close(); await CONN.writer.wait_closed()
        except Exception: pass
        CONN.clear(); EXIT_EVENT.set()

# ----------------- commands -----------------
async def cmd_connect(host:str,port:int)->None:
    if CONN.is_connected(): return
    for _ in range(10):
        try:
            reader,writer=await asyncio.open_connection(host,port); break
        except Exception: await asyncio.sleep(0.2)
    else:
        print("Connection failed"); sys.exit(0)
    CONN.reader,CONN.writer=reader,writer
    await send_line(writer,{"message_type":"HI","username":USERNAME})
    asyncio.create_task(handle_server_messages())

async def cmd_disconnect()->None:
    if not CONN.is_connected(): return
    try: await send_line(CONN.writer,{"message_type":"BYE"})  # type: ignore
    except Exception: pass
    try:
        CONN.writer.close(); await CONN.writer.wait_closed()
    except Exception: pass
    CONN.clear(); EXIT_EVENT.set()

async def handle_command(line:str)->None:
    cmd=line.strip(); up=cmd.upper()
    if not cmd: return
    if up=="EXIT": await cmd_disconnect(); sys.exit(0)
    if up.startswith("CONNECT"):
        try:
            host,port_s=cmd.split()[1].split(":")
            await cmd_connect(host,int(port_s))
        except Exception: print("[client] usage: CONNECT <host>:<port>")
        return
    if up=="DISCONNECT": await cmd_disconnect(); return

# ----------------- config and main -----------------
def load_client_config(path:Path)->Dict[str,Any]:
    try:
        cfg=json.loads(path.read_text(encoding="utf-8"))
        if "client_mode" not in cfg:
            print("client.py: Missing client_mode",file=sys.stderr); sys.exit(1)
        return cfg
    except Exception:
        print("client.py: failed to load config",file=sys.stderr); sys.exit(1)

async def main_async():
    dprint(f"[debug] startup mode={CLIENT_MODE} host={OLLAMA_HOST} port={(OLLAMA_PORT,OLLAMA_MODEL)} username={USERNAME}")
    # ---- NEW: warmup Ollama before connecting ----
    if CLIENT_MODE == "ai":
        # warmup round 1
        try:
            await asyncio.wait_for(warmup_ollama(), timeout=1.5)
        except Exception:
            dprint("[warmup] warmup #1 skipped due to exception.")

        # warmup round 2
        try:
            await asyncio.wait_for(warmup_ollama(), timeout=1.5)
        except Exception:
            dprint("[warmup] warmup #2 skipped due to exception.")
        # warmup round 3
        try:
            await asyncio.wait_for(warmup_ollama(), timeout=1.5)
        except Exception:
            dprint("[warmup] warmup #2 skipped due to exception.")
    # stdin command handling (grader)
    if not sys.stdin.isatty():
        line=await asyncio.to_thread(sys.stdin.readline)
        line=(line or "").strip()
        if not line: sys.exit(0)
        await handle_command(line)
        await EXIT_EVENT.wait(); sys.exit(0)
    await interactive_loop(); sys.exit(0)

async def interactive_loop()->None:
    q:asyncio.Queue[str]=asyncio.Queue()
    async def stdin_reader():
        loop=asyncio.get_running_loop()
        def _read():
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait,line.rstrip("\r\n"))
        await asyncio.to_thread(_read)
    asyncio.create_task(stdin_reader())
    while True:
        done,_=await asyncio.wait(
            {asyncio.create_task(q.get()),asyncio.create_task(EXIT_EVENT.wait())},
            return_when=asyncio.FIRST_COMPLETED)
        if EXIT_EVENT.is_set(): break
        for t in done: await handle_command(t.result())

def main():
    args=sys.argv[1:]
    if not args or args[0]!="--config" or len(args)<2:
        print("client.py: Configuration not provided",file=sys.stderr); sys.exit(1)
    cfg_path=Path(args[1])
    if not cfg_path.exists():
        print("client.py: Configuration not provided",file=sys.stderr); sys.exit(1)
    cfg=load_client_config(cfg_path)
    global CLIENT_MODE,USERNAME,OLLAMA_HOST,OLLAMA_PORT,OLLAMA_MODEL
    CLIENT_MODE=cfg.get("client_mode")
    USERNAME=cfg.get("username","player")
    if CLIENT_MODE=="ai":
        ollama_cfg=cfg.get("ollama_config",{}) or {}
        OLLAMA_HOST=ollama_cfg.get("ollama_host","localhost")
        OLLAMA_PORT=int(ollama_cfg.get("ollama_port",11434))
        OLLAMA_MODEL=ollama_cfg.get("ollama_model","mistral:latest")
    else:
        OLLAMA_HOST=OLLAMA_PORT=OLLAMA_MODEL=None
    try:
        asyncio.run(main_async())
    except (SystemExit,KeyboardInterrupt):
        pass

if __name__=="__main__":
    main()
