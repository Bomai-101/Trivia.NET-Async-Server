#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Async NDJSON quiz server (fixed for local testing).
"""

import asyncio, json, sys
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

PLAYERS: Dict[str, Dict[str, Any]] = {}
CURRENT_ANSWERS: Dict[str, str] = {}
LOCK = asyncio.Lock()
READY = asyncio.Event()

async def send_line(w, obj): 
    w.write((json.dumps(obj, ensure_ascii=False)+"\n").encode("utf-8")); await w.drain()

async def handle_client(r, w):
    addr = w.get_extra_info("peername")
    pid = f"{addr[0]}:{addr[1]}"
    try:
        while True:
            line = await r.readline()
            if not line: break
            msg = json.loads(line.decode("utf-8"))
            t = msg.get("type","").upper()
            if t=="HI":
                async with LOCK: PLAYERS[pid]={"w":w,"name":msg.get("username",pid),"score":0}
                await send_line(w,{"type":"ACK","player_id":pid})
                if len(PLAYERS)>=2: READY.set()
            elif t=="ANSWER":
                async with LOCK: CURRENT_ANSWERS[PLAYERS[pid]["name"]]=msg.get("answer","")
            elif t=="BYE": break
    except: pass
    finally:
        async with LOCK:
            if pid in PLAYERS: del PLAYERS[pid]
        try:w.close();await w.wait_closed()
        except: pass

async def coordinator():
    await READY.wait()
    await asyncio.sleep(0.5)
    await broadcast({"type":"READY","info":"Game starts!"})
    await asyncio.sleep(0.5)
    q={"trivia_question":"What is 3+4?","short_question":"3+4","time_limit":5}
    await broadcast({"type":"QUESTION",**q})
    await asyncio.sleep(5)
    correct="7"
    async with LOCK:
        for p in list(PLAYERS.values()):
            ans=CURRENT_ANSWERS.get(p["name"],"")
            ok=ans==correct
            fb="Woohoo! Great job!" if ok else "Maybe next time :("
            if ok:p["score"]+=1
            await send_line(p["w"],{"type":"RESULT","feedback":fb})
    lb=", ".join(f"{p['name']}:{p['score']}" for p in PLAYERS.values())
    await broadcast({"type":"LEADERBOARD","feedback":lb})
    fs="\n".join(f"{p['name']}:{p['score']}" for p in PLAYERS.values())
    await broadcast({"type":"FINISHED","final_standings":fs})

async def broadcast(msg):
    async with LOCK:
        for p in PLAYERS.values():
            try: await send_line(p["w"],msg)
            except: pass

async def main():
    cfg={"port":5050}
    srv=await asyncio.start_server(handle_client,"127.0.0.1",cfg["port"])
    print(f"[server] listening on 127.0.0.1:{cfg['port']}")
    asyncio.create_task(coordinator())
    async with srv: await srv.serve_forever()

#main
if __name__=="__main__":
    asyncio.run(main())
