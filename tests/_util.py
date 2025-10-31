"""Utility helpers for subprocess-based integration tests."""
import json
import socket
import tempfile
import time
from pathlib import Path
from typing import Iterable
import subprocess
import threading
from queue import Queue, Empty
import sys
import os


def pick_free_port() -> int:
    """Return an available TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write_json_temp(data: dict) -> Path:
    """Write a JSON object to a temporary file and return its path."""
    fd, p = tempfile.mkstemp(prefix="cfg_", suffix=".json")
    Path(p).write_text(json.dumps(data), encoding="utf-8")
    return Path(p)


def _wait_until_listening(host: str, port: int, timeout: float = 4.0) -> bool:
    """Poll a TCP host:port until it accepts connections or a timeout occurs."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.2)
            try:
                if s.connect_ex((host, port)) == 0:
                    return True
            except Exception:
                pass
        time.sleep(0.05)
    return False


def start_server(cfg_path: Path) -> subprocess.Popen:
    """Start server.py with the given config and return the Popen handle."""
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    cmd = [sys.executable, "server.py", "--config", str(cfg_path)]
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    try:
        cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
        port = int(cfg.get("port", 5050))
    except Exception:
        port = None
    if port is not None:
        _wait_until_listening("127.0.0.1", port, timeout=4.0)
    else:
        time.sleep(0.3)
    return p


def start_client(cfg_path: Path) -> subprocess.Popen:
    """Start client.py with the given config and return the Popen handle."""
    env = os.environ.copy()
    env.setdefault("PYTHONUNBUFFERED", "1")
    if sys.platform == "win32":
        shim = (
            "import asyncio, runpy, sys; "
            "asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy()); "
            f"sys.argv=['client.py','--config', r'{str(cfg_path)}']; "
            'runpy.run_path("client.py", run_name="__main__")'
        )
        cmd = [sys.executable, "-c", shim]
    else:
        cmd = [sys.executable, "client.py", "--config", str(cfg_path)]
    p = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    return p


def enqueue_reader(stream, q: Queue):
    """Continuously read a text stream and enqueue lines until EOF."""
    for line in iter(stream.readline, ""):
        q.put(line)
    stream.close()


class proc_io:
    """Threaded stdout/stderr collector for a subprocess with convenience methods."""

    def __init__(self, proc: subprocess.Popen):
        """Initialize collectors for the given process."""
        self.proc = proc
        self.out_q: Queue[str] = Queue()
        self.err_q: Queue[str] = Queue()
        self._t_out = threading.Thread(target=enqueue_reader, args=(proc.stdout, self.out_q), daemon=True)
        self._t_err = threading.Thread(target=enqueue_reader, args=(proc.stderr, self.err_q), daemon=True)
        self._t_out.start()
        self._t_err.start()

    def send_lines(self, lines: Iterable[str], delay: float = 0.0):
        """Send lines to the process stdin, optionally delaying between lines."""
        for ln in lines:
            if self.proc.stdin:
                self.proc.stdin.write(ln if ln.endswith("\n") else ln + "\n")
                self.proc.stdin.flush()
            if delay:
                time.sleep(delay)

    def read_all_stdout_now(self) -> str:
        """Drain and return all currently buffered stdout text."""
        chunks = []
        while True:
            try:
                chunks.append(self.out_q.get_nowait())
            except Empty:
                break
        return "".join(chunks)

    def read_all_stderr_now(self) -> str:
        """Drain and return all currently buffered stderr text."""
        chunks = []
        while True:
            try:
                chunks.append(self.err_q.get_nowait())
            except Empty:
                break
        return "".join(chunks)

    def wait_for(self, substr: str, timeout: float = 6.0) -> str:
        """Block until substr appears in stdout or timeout expires, returning collected stdout."""
        end = time.time() + timeout
        buf = ""
        while time.time() < end:
            buf += self.read_all_stdout_now()
            if substr in buf:
                return buf
            time.sleep(0.05)
        buf += self.read_all_stdout_now()
        return buf

    def terminate(self, timeout: float = 3.0):
        """Terminate the process, close pipes, and join reader threads."""
        try:
            if self.proc.stdin and not self.proc.stdin.closed:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=timeout)
        except Exception:
            try:
                self.proc.kill()
                self.proc.wait(timeout=timeout)
            except Exception:
                pass
        try:
            self.read_all_stdout_now()
            self.read_all_stderr_now()
        except Exception:
            pass
        for pipe in (self.proc.stdout, self.proc.stderr):
            try:
                if pipe and not pipe.closed:
                    pipe.close()
            except Exception:
                pass
        try:
            if self._t_out.is_alive():
                self._t_out.join(timeout=1.0)
            if self._t_err.is_alive():
                self._t_err.join(timeout=1.0)
        except Exception:
            pass
