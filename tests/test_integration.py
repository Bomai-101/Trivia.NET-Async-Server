"""End-to-end tests for client/server integration."""
import time
import unittest
from pathlib import Path
import os
import sys

THIS_DIR = os.path.dirname(__file__)
PARENT = os.path.abspath(os.path.join(THIS_DIR, ".."))

try:
    from tests._util import pick_free_port, write_json_temp, start_server, start_client, proc_io
except Exception:
    if THIS_DIR not in sys.path:
        sys.path.insert(0, THIS_DIR)
    from _util import pick_free_port, write_json_temp, start_server, start_client, proc_io


class TestEndToEndOneClient(unittest.TestCase):
    """Integration tests covering client auto/you modes and server coordination."""

    def _server_cfg(self, port: int, players: int, qtypes, qsec=0.4, qgap=0.2):
        """Return a minimal server config dictionary for a parametrized quiz."""
        return {
            "port": port,
            "players": players,
            "question_types": qtypes,
            "question_formats": {
                "Mathematics": "Evaluate {}",
                "Roman Numerals": "Calculate the decimal value of {}",
                "Usable IP Addresses of a Subnet": "How many usable addresses in {}?",
                "Network and Broadcast Address of a Subnet": "Network and broadcast addresses of {}?",
            },
            "question_seconds": qsec,
            "question_interval_seconds": qgap,
            "ready_info": "Game starts in {question_interval_seconds} seconds!",
            "question_word": "Question",
            "correct_answer": "Woohoo! Great job! You got it!",
            "incorrect_answer": "Maybe next time :(",
            "points_noun_singular": "point",
            "points_noun_plural": "points",
            "final_standings_heading": "Final standings:",
            "one_winner": "The winner is: {}",
            "multiple_winners": "The winners are: {}",
        }

    def _client_cfg(self, username: str, mode: str, ai=None):
        """Return a minimal client config dictionary."""
        cfg = {"username": username, "client_mode": mode}
        if ai is not None:
            cfg["ollama_config"] = ai
        return cfg

    def test_client_connection_failed_exits(self):
        """Client should print connection failure and exit when server is absent."""
        bad_port = pick_free_port()
        client_cfg = write_json_temp(self._client_cfg("alice", "auto"))
        p = start_client(client_cfg)
        io = proc_io(p)
        try:
            io.send_lines([f"CONNECT 127.0.0.1:{bad_port}"])
            buf = io.wait_for("Connection failed", timeout=6.0)
            self.assertIn("Connection failed", buf)
            t0 = time.time()
            while p.poll() is None and time.time() - t0 < 2.0:
                time.sleep(0.05)
            self.assertIsNotNone(p.poll())
        finally:
            try:
                io.terminate()
            except Exception:
                pass

    def test_full_game_auto_mode_correct_answers(self):
        """Auto mode should answer deterministically and finish the game."""
        port = pick_free_port()
        srv_cfg = write_json_temp(
            self._server_cfg(
                port,
                players=1,
                qtypes=[
                    "Roman Numerals",
                    "Usable IP Addresses of a Subnet",
                    "Network and Broadcast Address of a Subnet",
                    "Mathematics",
                ],
                qsec=0.35,
                qgap=0.15,
            )
        )
        srv = start_server(srv_cfg)
        client_cfg = write_json_temp(self._client_cfg("alice", "auto"))
        cli = start_client(client_cfg)
        io = proc_io(cli)
        try:
            io.send_lines([f"CONNECT 127.0.0.1:{port}"])
            out = io.wait_for("Game starts in", timeout=6.0)
            self.assertIn("Game starts in", out)
            out2 = io.wait_for("Final standings:", timeout=8.0)
            self.assertIn("Final standings:", out2)
            self.assertIn("alice", out2)
            all_out = out + out2 + io.wait_for("The winner is:", timeout=2.0)
            self.assertIn("Woohoo! Great job! You got it!", all_out)
            io.send_lines(["EXIT"])
            cli.wait(timeout=3.0)
        finally:
            try:
                io.terminate()
            except Exception:
                pass
            try:
                srv.terminate()
                srv.wait(timeout=2.0)
            except Exception:
                pass

    def test_you_mode_incorrect_answer_feedback(self):
        """Manual mode should accept stdin answer and return incorrect feedback."""
        port = pick_free_port()
        srv_cfg = write_json_temp(
            self._server_cfg(port, players=1, qtypes=["Mathematics"], qsec=0.8, qgap=0.2)
        )
        srv = start_server(srv_cfg)
        client_cfg = write_json_temp(self._client_cfg("bob", "you"))
        cli = start_client(client_cfg)
        io = proc_io(cli)
        try:
            io.send_lines([f"CONNECT 127.0.0.1:{port}"])
            out = io.wait_for("Question 1", timeout=6.0)
            self.assertIn("Question 1", out)
            io.send_lines(["-999999"])
            out2 = io.wait_for("Maybe next time :(", timeout=6.0)
            self.assertIn("Maybe next time :(", out2)
            out3 = io.wait_for("Final standings:", timeout=6.0)
            self.assertIn("Final standings:", out3)
            io.send_lines(["EXIT"])
            cli.wait(timeout=3.0)
        finally:
            try:
                io.terminate()
            except Exception:
                pass
            try:
                srv.terminate()
                srv.wait(timeout=2.0)
            except Exception:
                pass
