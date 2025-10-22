# client.py
import json
import socket
import sys
from typing import Any, Dict

# ---------- 基础工具 / Utilities ----------

def send_json_from_client(sock: socket.socket, message: Dict[str, Any]) -> None:
    """
    客户端发送：JSON（不加换行）
    Client messages must NOT end with newline.
    """
    data = json.dumps(message)
    sock.sendall(data.encode("utf-8"))

def recv_json_line_from_server(sock: socket.socket) -> Dict[str, Any] | None:
    """
    服务器消息以 '\n' 结尾，这里读到换行为止，再 json.loads。
    Server messages end with newline; read until '\n'.
    """
    buffer = b""
    sock.settimeout(120)
    while True:
        chunk = sock.recv(1)
        if not chunk:
            return None
        buffer += chunk
        if buffer.endswith(b"\n"):
            try:
                text = buffer.decode("utf-8").rstrip("\n")
                return json.loads(text)
            except json.JSONDecodeError:
                # 出现解析错误继续累积（理论上不应发生）
                buffer += b""

# ---------- 配置加载 / Config loader ----------

def load_config() -> Dict[str, Any]:
    if len(sys.argv) != 3 or sys.argv[1] != "--config":
        print("Usage: python client.py --config <config_path>", file=sys.stderr)
        sys.exit(1)
    config_path = sys.argv[2]
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Config file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON in '{config_path}': {e}", file=sys.stderr)
        sys.exit(1)

# ---------- 主流程 / Main flow ----------

def main():
    config = load_config()
    host = config.get("host", "127.0.0.1")
    port = int(config.get("port", 5000))
    username = config.get("username", "Alice")
    mode = config.get("client_mode", "you")  # "you" | "auto"（这里两种都能跑）

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect((host, port))
        print(f"[CLIENT] Connected to {host}:{port}")

        # 1) 发送 HI（不带换行）
        send_json_from_client(sock, {"type": "HI", "username": username})
        print(f"[CLIENT] Sent HI as {username}")

        # 2) 循环接收服务器消息
        while True:
            msg = recv_json_line_from_server(sock)
            if not msg:
                print("[CLIENT] Server closed connection.")
                break

            mtype = msg.get("type")
            print("[CLIENT] Received:", msg)

            if mtype == "READY":
                print("[CLIENT] Ready received. Waiting for question...")

            elif mtype == "QUESTION":
                short_q = msg.get("short_question", "")
                trivia_q = msg.get("trivia_question", "")
                print(f"[QUESTION] {trivia_q}")

                if mode == "auto":
                    # 最简单的“auto”：只支持 2+1 这种示例（演示用）
                    # A tiny demo auto that handles the sample "2+1"
                    answer = str(eval(short_q, {"__builtins__": {}}, {}))  # 演示用：请勿在正式作业用 eval
                    print(f"[AUTO] Answered: {answer}")
                else:
                    answer = input("[YOU] Your answer: ").strip()

                send_json_from_client(sock, {"type": "ANSWER", "answer": answer})

            elif mtype == "RESULT":
                print(f"[RESULT] {'✅ Correct' if msg.get('correct') else '❌ Incorrect'} - {msg.get('feedback','')}")

            elif mtype == "FINISHED":
                print("[CLIENT] Game over. Bye!")
                break

            else:
                print("[CLIENT] Unknown message:", msg)

    finally:
        sock.close()

if __name__ == "__main__":
    main()
