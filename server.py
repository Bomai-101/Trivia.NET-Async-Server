# server.py
import json
import socket
import sys
from typing import Any, Dict

# ---------- 基础工具 / Utilities ----------

def send_json_from_server(sock: socket.socket, message: Dict[str, Any]) -> None:
    """
    服务器发送：JSON + '\n'（作为消息边界）
    Server messages MUST end with newline to mark message boundary.
    """
    data = json.dumps(message) + "\n"
    sock.sendall(data.encode("utf-8"))

def recv_json_from_client(sock: socket.socket) -> Dict[str, Any] | None:
    """
    客户端消息不带 '\n'，这里用“累积 + 尝试 json.loads”方式拼出一条完整 JSON。
    We accumulate bytes and keep trying json.loads until it parses.
    """
    buffer = b""
    sock.settimeout(120)  # 防止无限阻塞 / avoid hanging forever
    while True:
        try:
            # 尝试解析
            text = buffer.decode("utf-8")
            if text.strip():
                return json.loads(text)
        except json.JSONDecodeError:
            # 继续读更多字节
            pass

        chunk = sock.recv(1024)
        if not chunk:
            # 连接关闭 / connection closed
            return None
        buffer += chunk

# ---------- 配置加载 / Config loader ----------

def load_config() -> Dict[str, Any]:
    if len(sys.argv) != 3 or sys.argv[1] != "--config":
        print("Usage: python server.py --config <config_path>", file=sys.stderr)
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
    host = config.get("host", "0.0.0.0")
    port = int(config.get("port", 5000))

    # 这里只做 1 客户端最小演示（可扩展到多客户端）
    # Minimal demo for a single client (can be extended to multiple)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    try:
        server.bind((host, port))
        server.listen()
        print(f"[SERVER] Listening on {host}:{port} ...")

        conn, addr = server.accept()
        print(f"[SERVER] Client connected from {addr}")

        # 1) 接收 HI（客户端消息无换行）
        hi = recv_json_from_client(conn)
        print("[SERVER] Received:", hi)
        if not hi or hi.get("type") != "HI" or not hi.get("username", "").isalnum():
            send_json_from_server(conn, {"type": "FINISHED", "info": "Invalid HI/username"})
            conn.close()
            return

        # 2) READY
        send_json_from_server(conn, {"type": "READY", "info": "Game starting..."})

        # 3) QUESTION（简化：固定一道题）
        short_q = "2+1"
        trivia_q = "What is 2+1?"
        send_json_from_server(
            conn,
            {
                "type": "QUESTION",
                "short_question": short_q,
                "trivia_question": trivia_q,
            },
        )

        # 4) 接收 ANSWER
        ans = recv_json_from_client(conn)
        print("[SERVER] Received:", ans)
        correct = False
        if ans and ans.get("type") == "ANSWER":
            correct = (ans.get("answer") == "3")

        # 5) RESULT
        send_json_from_server(
            conn,
            {
                "type": "RESULT",
                "correct": bool(correct),
                "feedback": "Correct!" if correct else "Incorrect. Correct answer is 3.",
            },
        )

        # 6) FINISHED
        send_json_from_server(conn, {"type": "FINISHED", "info": "Game over."})
        print("[SERVER] Finished; closing connection.")
        conn.close()

    finally:
        server.close()

if __name__ == "__main__":
    main()
