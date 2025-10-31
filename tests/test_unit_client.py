import unittest
import importlib
import sys, os

THIS_DIR = os.path.dirname(__file__)
PARENT = os.path.abspath(os.path.join(THIS_DIR, ".."))
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

client = importlib.import_module("client")

class TestClientPureHelpers(unittest.TestCase):
    def test_eval_plus_minus(self):
        self.assertEqual(client._eval_plus_minus("12 + 3 - 4 + 5"), "16")
        self.assertEqual(client._eval_plus_minus("68 + 81 + 90"), "239")
        self.assertEqual(client._eval_plus_minus("1 - 2 - 3 - 4"), "-8")
        self.assertEqual(client._eval_plus_minus("bad"), "")
        self.assertEqual(client._eval_plus_minus(""), "")

    def test_roman_to_int(self):
        self.assertEqual(client._roman_to_int("I"), 1)
        self.assertEqual(client._roman_to_int("XLV"), 45)
        self.assertEqual(client._roman_to_int("mcmxcix"), 1999)
        self.assertEqual(client._roman_to_int("CM"), 900)

    def test_ipv4_usable(self):
        self.assertEqual(client._usable_ipv4_addresses("192.168.1.0/24"), "254")
        self.assertEqual(client._usable_ipv4_addresses("10.0.0.0/16"), "65534")
        self.assertEqual(client._usable_ipv4_addresses("10.0.0.0/31"), "0")
        self.assertEqual(client._usable_ipv4_addresses("10.0.0.0/32"), "0")
        self.assertEqual(client._usable_ipv4_addresses("bad"), "")

    def test_network_and_broadcast(self):
        self.assertEqual(
            client._network_broadcast_answer("192.168.1.37/24"),
            "192.168.1.0 and 192.168.1.255"
        )
        self.assertEqual(
            client._network_broadcast_answer("10.0.0.5/30"),
            "10.0.0.4 and 10.0.0.7"
        )
        self.assertEqual(client._network_broadcast_answer("bad"), "")
