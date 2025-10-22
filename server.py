#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Async NDJSON quiz server.

Protocol (NDJSON, one JSON per line):
Client -> Server:
  HI { "name": "<optional>" }
  ANSWER { "answer": "<string>" }
  BYE
Server -> Client:
  ACK { "player_id": "<str>" }
  READY { "round": <int>, "total_rounds": <int>, "question_seconds": <int> }
  QUESTION { "question_type": "<str>", "question": "<str>", "short_question": "<str>" }
  RESULT { "correct": <bool>, "feedback": "<str>", "score_delta": <int> }
  LEADERBOARD { "state": "<str>", "scores": {"<player_name>": <int>, ...} }
  FINISHED { "scores": {...} }
  ERROR { "message": "<str>" }

Config (optional JSON):
{
  "host": "127.0.0.1",
  "port": 5050,
  "players": 2,
  "question_seconds": 10,
  "question_types": ["math:add", "upper"]
}
"""

import asyncio
import json
import signal
import sys
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

# ---------------- Runtime state ----------------
SERVER_CFG: Dict[str, Any] = {}
PLAYERS_REQUIRED = 2
QUESTION_SECONDS = 10
QUESTION_TYPES: List[str] = ["math:add", "upper"]

# player_id -> {"writer": StreamWriter, "name": str, "score": int, "alive": bool}
PLAYERS: Dict[str, Dict[str, Any]] = {}
PLAYERS_LOCK = asyncio.Lock()

# current round answers: player_id -> {"answer": str, "ts": float}
CURRENT_ANSWERS: Dict[str, Dict[str, Any]] = {}
ANSWERS_LOCK = asyncio.Lock()

STOP_EVENT = asyncio.Event()
ROUND_READY = asyncio.Event()

# ---------------- Utilities ----------------
def _encode(obj: Dict[str, Any]) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

async def send_line(writer: asyncio.StreamWriter, obj: Dict[str, Any]) -> None:
    writer.write(_encode(obj))
    await writer.drain()

async def load_config(path: Optional[Path]) -> Dict[str, Any]:
    defaults = {
        "host": "127.0.0.1",
        "port": 5050,
        "players": 2,
        "question_seconds": 10,
        "question_types": ["math:add", "upper"],
    }
    if not path:
        return defaults
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        defaults.update(data or {})
    except Exception as e:
        print(f"[server] Failed to load config: {e}", file=sys.stderr)
    return defaults

# ---------------- Scaffolded helpers ----------------
async def add_player(writer: asyncio.StreamWriter, addr: Tuple[str, int], name: Optional[str]) -> str:
    pid = f"{addr[0]}:{addr[1]}"
    async with PLAYERS_LOCK:
        if pid not in PLAYERS:
            PLAYERS[pid] = {
                "writer": writer,
                "name": name or pid,
                "score": 0,
                "alive": True,
            }
    return pid

async def remove_player(player_id: str) -> None:
    async with PLAYERS_LOCK:
        info = PLAYERS.get(player_id)
        if info:
            info["alive"] = False
            try:
                info["writer"].close()
            except Exception:
                pass

async def handle_player_answer(player_id: str, answer: str) -> None:
    async with ANSWERS_LOCK:
        CURRENT_ANSWERS[player_id] = {"answer": answer, "ts": asyncio.get_event_loop().time()}

async def send_to_all_players(msg: Dict[str, Any]) -> None:
    async with PLAYERS_LOCK:
        dead = []
        for pid, info in PLAYERS.items():
            if not info["alive"]:
                continue
            try:
                await send_line(info["writer"], msg)
            except Exception:
                dead.append(pid)
        for pid in dead:
            PLAYERS[pid]["alive"] = False

async def receive_answers(timeout_seconds: int) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_seconds
    while loop.time() < deadline:
        async with PLAYERS_LOCK:
            alive = [pid for pid, p in PLAYERS.items() if p["alive"]]
        async with ANSWERS_LOCK:
            got = set(CURRENT_ANSWERS.keys())
        if alive and got.issuperset(alive):
            return
        await asyncio.sleep(0.05)

def generate_leaderboard_state() -> str:
    items = sorted(((p["name"], p["score"]) for p in PLAYERS.values() if p["alive"]),
                   key=lambda x: (-x[1], x[0]))
    return ", ".join(f"{name}:{score}" for name, score in items)

def generate_question(question_type: str) -> Dict[str, Any]:
    if question_type == "upper":
        return {"question_type": "upper",
                "question": "Convert to UPPERCASE: hello world",
                "short_question": "hello world"}
    if question_type.startswith("math:add"):
        a, b = 3, 4
        return {"question_type": "math:add",
                "question": f"What is {a}+{b}?",
                "short_question": f"{a}+{b}"}
    return {"question_type": question_type, "question": "noop?", "short_question": "noop"}

def generate_question_answer(question_type: str, short_question: str) -> str:
    if question_type == "upper":
        return short_question.upper()
    if question_type.startswith("math:add"):
        try:
            x, y = short_question.split("+")
            return str(int(x) + int(y))
        except Exception:
            return ""
    return ""

async def start_game(total_rounds: int, question_seconds: int) -> None:
    await send_to_all_players({"type": "READY", "round": 0,
                               "total_rounds": total_rounds,
                               "question_seconds": question_seconds})

async def start_round(round_idx: int, question_type: str) -> Dict[str, Any]:
    q = generate_question(question_type)
    await send_to_all_players({"type": "QUESTION", **q})
    return q

async def end_round(round_idx: int, total_rounds: int) -> None:
    if round_idx + 1 < total_rounds:
        state = generate_leaderboard_state()
        scores = {p["name"]: p["score"] for p in PLAYERS.values() if p["alive"]}
        await send_to_all_players({"type": "LEADERBOARD", "state": state, "scores": scores})
    else:
        scores = {p["name"]: p["score"] for p in PLAYERS.values() if p["alive"]}
        await send_to_all_players({"type": "FINISHED", "scores": scores})

# ---------------- Per-connection handler ----------------
async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    addr = writer.get_extra_info("peername")
    pid = None
    try:
        while not STOP_EVENT.is_set():
            line = await reader.readline()
            if not line:
                break  # client socket received empty message -> disconnect
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                await send_line(writer, {"type": "ERROR", "message": "invalid_json"})
                continue

            mtype = str(msg.get("type", "")).upper()

            if mtype == "HI":
                name = msg.get("name")
                pid = await add_player(writer, addr, name)
                await send_line(writer, {"type": "ACK", "player_id": pid})
                async with PLAYERS_LOCK:
                    alive = [p for p in PLAYERS.values() if p["alive"]]
                if len(alive) >= PLAYERS_REQUIRED:
                    ROUND_READY.set()

            elif mtype == "ANSWER":
                if pid is None:
                    await send_line(writer, {"type": "ERROR", "message": "not_registered"})
                    continue
                ans = str(msg.get("answer", ""))
                await handle_player_answer(pid, ans)

            elif mtype == "BYE":
                break

            else:
                await send_line(writer, {"type": "ERROR", "message": f"unknown_type:{msg.get('type')}"})
    except Exception as e:
        print(f"[server] client error {addr}: {e}", file=sys.stderr)
    finally:
        if pid:
            await remove_player(pid)

# ---------------- Game coordinator ----------------
async def coordinator():
    # Wait for enough players
    while not STOP_EVENT.is_set():
        async with PLAYERS_LOCK:
            alive = [p for p in PLAYERS.values() if p["alive"]]
        if len(alive) >= PLAYERS_REQUIRED:
            break
        try:
            await asyncio.wait_for(ROUND_READY.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

    if STOP_EVENT.is_set():
        return

    total_rounds = len(QUESTION_TYPES)
    await start_game(total_rounds, QUESTION_SECONDS)

    for r_idx, qtype in enumerate(QUESTION_TYPES):
        async with ANSWERS_LOCK:
            CURRENT_ANSWERS.clear()

        q = await start_round(r_idx, qtype)

        await receive_answers(QUESTION_SECONDS)

        correct = generate_question_answer(q["question_type"], q["short_question"])

        # score updates + per-player RESULT
        async with PLAYERS_LOCK:
            for pid, info in list(PLAYERS.items()):
                if not info["alive"]:
                    continue
                ans_obj = CURRENT_ANSWERS.get(pid)
                ans = ans_obj["answer"] if ans_obj else ""
                ok = (ans == correct)
                delta = 1 if ok else 0
                info["score"] += delta
                feedback = "correct" if ok else f"incorrect; expected '{correct}'"
                try:
                    await send_line(info["writer"],
                                    {"type": "RESULT", "correct": ok,
                                     "feedback": feedback, "score_delta": delta})
                except Exception:
                    info["alive"] = False

        await end_round(r_idx, total_rounds)
        if STOP_EVENT.is_set():
            break

# ---------------- Main ----------------
async def main_async():
    global SERVER_CFG, PLAYERS_REQUIRED, QUESTION_SECONDS, QUESTION_TYPES

    # Parse --config
    cfg_path = None
    if "--config" in sys.argv:
        i = sys.argv.index("--config")
        cfg_path = Path(sys.argv[i + 1]) if i + 1 < len(sys.argv) else None
    SERVER_CFG = await load_config(cfg_path)

    host = SERVER_CFG.get("host", "127.0.0.1")
    port = int(SERVER_CFG.get("port", 5050))
    PLAYERS_REQUIRED = int(SERVER_CFG.get("players", 2))
    QUESTION_SECONDS = int(SERVER_CFG.get("question_seconds", 10))
    QUESTION_TYPES = list(SERVER_CFG.get("question_types", ["math:add", "upper"]))

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, STOP_EVENT.set)
        except NotImplementedError:
            pass

    server = await asyncio.start_server(handle_client, host, port)
    print(f"[server] listening on {host}:{port} (players={PLAYERS_REQUIRED}, qsec={QUESTION_SECONDS})")

    coord_task = asyncio.create_task(coordinator())

    async with server:
        try:
            await server.serve_forever()
        except asyncio.CancelledError:
            pass

    await coord_task

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass
    finally:
        print("[server] bye.")

if __name__ == "__main__":
    main()
