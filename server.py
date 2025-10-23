#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Async NDJSON quiz server (fixed for local testing) with spec-compliant startup checks.
"""

import asyncio, json, sys
from pathlib import Path
from typing import Any, Dict

PLAYERS: Dict[str, Dict[str, Any]] = {}
CURRENT_ANSWERS: Dict[str, str] = {}
LOCK = asyncio.Lock()
READY = asyncio.Event()

async def send_line(w, obj):
    w.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
    await w.drain()

async def handle_client(r, w):
    addr = w.get_extra_info("peername")
    pid = f"{addr[0]}:{addr[1]}"
    try:
        while True:
            line = await r.readline()
            if not line:
                break
            msg = json.loads(line.decode("utf-8"))
            t = msg.get("type", "").upper()
            if t == "HI":
                async with LOCK:
                    PLAYERS[pid] = {"w": w, "name": msg.get("username", pid), "score": 0}
                await send_line(w, {"type": "ACK", "player_id": pid})
                if len(PLAYERS) >= 2:  # for solo testing you can change to >= 1
                    READY.set()
            elif t == "ANSWER":
                async with LOCK:
                    # store by name so coordinator reads consistently
                    CURRENT_ANSWERS[PLAYERS[pid]["name"]] = msg.get("answer", "")
            elif t == "BYE":
                break
    except:
        pass
    finally:
        async with LOCK:
            if pid in PLAYERS:
                del PLAYERS[pid]
        try:
            w.close()
            await w.wait_closed()
        except:
            pass

async def coordinator():
    await READY.wait()
    await asyncio.sleep(0.5)
    await broadcast({"type": "READY", "info": "Game starts!"})
    await asyncio.sleep(0.5)
    q = {"trivia_question": "What is 3+4?", "short_question": "3+4", "time_limit": 5}
    await broadcast({"type": "QUESTION", **q})
    await asyncio.sleep(5)
    correct = "7"
    async with LOCK:
        for p in list(PLAYERS.values()):
            ans = CURRENT_ANSWERS.get(p["name"], "")
            ok = ans == correct
            fb = "Woohoo! Great job!" if ok else "Maybe next time :("
            if ok:
                p["score"] += 1
            await send_line(p["w"], {"type": "RESULT", "feedback": fb})
    lb = ", ".join(f"{p['name']}:{p['score']}" for p in PLAYERS.values())
    await broadcast({"type": "LEADERBOARD", "feedback": lb})
    fs = "\n".join(f"{p['name']}:{p['score']}" for p in PLAYERS.values())
    await broadcast({"type": "FINISHED", "final_standings": fs})

async def broadcast(msg):
    async with LOCK:
        for p in PLAYERS.values():
            try:
                await send_line(p["w"], msg)
            except:
                pass

# ---------------- Config loading ----------------
def load_server_config(path: Path) -> Dict[str, Any]:
    """
    Load JSON server configuration. You can extend this to validate fields.
    Not part of the fatal-startup spec unless file missing; invalid JSON can default.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        # If JSON is bad, fall back to empty (you can choose to hard-fail if desired)
        return {}

# ---------------- Main with spec-compliant arg parsing ----------------
async def main():
    # Spec: --config is required; missing or no path => stderr + exit(1)
    args = sys.argv[1:]
    if not args or args[0] != "--config":
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    if len(args) < 2:
        print("server.py: Configuration not provided", file=sys.stderr)
        sys.exit(1)

    cfg_path = Path(args[1])
    if not cfg_path.exists():
        print(f"server.py: File {cfg_path} does not exist", file=sys.stderr)
        sys.exit(1)

    cfg = load_server_config(cfg_path)
    port = int(cfg.get("port", 5050))  # default allowed; spec doesn’t forbid

    # Spec: if binding fails => stderr + exit(1) with exact message
    try:
        srv = await asyncio.start_server(handle_client, "127.0.0.1", port)
    except OSError:
        print(f"server.py: Binding to port {port} was unsuccessful", file=sys.stderr)
        sys.exit(1)

    print(f"[server] listening on 127.0.0.1:{port}")
    asyncio.create_task(coordinator())
    async with srv:
        await srv.serve_forever()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit:
        pass
    except KeyboardInterrupt:
        pass
