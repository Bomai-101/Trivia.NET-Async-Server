import unittest
import importlib
import sys, os

THIS_DIR = os.path.dirname(__file__)
PARENT = os.path.abspath(os.path.join(THIS_DIR, ".."))
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

server = importlib.import_module("server")

class TestServerPureHelpers(unittest.TestCase):
    def setUp(self):
        # Minimal CFG for text templates
        server.CFG.clear()
        server.CFG.update({
            "points_noun_singular": "point",
            "points_noun_plural": "points",
            "final_standings_heading": "Final standings:",
            "one_winner": "{} is the sole victor!",
            "multiple_winners": "Say congratulations to {}!",
        })
        server.PLAYERS.clear()

    def test_compute_correct_answer(self):
        self.assertEqual(server.compute_correct_answer("Mathematics", "1 + 2 - 3"), "0")
        self.assertEqual(server.compute_correct_answer("Roman Numerals", "XLV"), "45")
        self.assertEqual(server.compute_correct_answer("Usable IP Addresses of a Subnet", "192.168.1.0/24"), "254")
        self.assertEqual(
            server.compute_correct_answer("Network and Broadcast Address of a Subnet", "192.168.1.37/24"),
            "192.168.1.0 and 192.168.1.255"
        )
        self.assertIsNone(server.compute_correct_answer("Unknown", "foo"))

    def test_pluralize_points(self):
        self.assertEqual(server.pluralize_points(1), "point")
        self.assertEqual(server.pluralize_points(0), "points")
        self.assertEqual(server.pluralize_points(2), "points")

    def test_leaderboard_and_final(self):
        # Two players with a tie to exercise lexicographic ordering and rank ties
        server.PLAYERS["p1"] = {"name": "Apple", "score": 2}
        server.PLAYERS["p2"] = {"name": "Banana", "score": 2}
        server.PLAYERS["p3"] = {"name": "Cherry", "score": 1}

        state = server.build_leaderboard_state()
        self.assertIn("1. Apple: 2 points", state)
        self.assertIn("1. Banana: 2 points", state)
        self.assertIn("3. Cherry: 1 point", state)

        final_ = server.build_final_standings()
        self.assertTrue(final_.splitlines()[0].startswith("Final standings:"))
        self.assertIn("1. Apple: 2 points", final_)
        self.assertIn("1. Banana: 2 points", final_)
        self.assertIn("3. Cherry: 1 point", final_)
        self.assertIn("Say congratulations to Apple, Banana!", final_)
