"""Tests for the game traffic classifier."""

from __future__ import annotations

import struct
import unittest

from antilag.traffic import is_game, ports


def packet(proto: int, sport: int, dport: int, frag: int = 0, ihl: int = 5) -> bytes:
    header = struct.pack(
        "!BBHHHBBH4s4s", 0x40 | ihl, 0, 0, 0, frag, 64, proto, 0, b"\x0a\x42\x00\x02", b"\x01\x01\x01\x01"
    )
    return header + b"\0" * ((ihl - 5) * 4) + struct.pack("!HH", sport, dport) + b"\0" * 16


class TestTraffic(unittest.TestCase):
    def test_tcp_to_game_port(self):
        self.assertTrue(is_game(packet(6, 51000, 7171)))

    def test_tcp_from_game_port(self):
        self.assertTrue(is_game(packet(6, 7172, 51000)))

    def test_web_is_not_game(self):
        self.assertFalse(is_game(packet(6, 51000, 443)))

    def test_udp_counts(self):
        self.assertTrue(is_game(packet(17, 40000, 7171)))

    def test_icmp_is_not_game(self):
        self.assertFalse(is_game(packet(1, 7171, 7171)))

    def test_ip_options_respected(self):
        self.assertEqual(ports(packet(6, 1234, 7171, ihl=6)), (1234, 7171))

    def test_later_fragment_has_no_ports(self):
        self.assertIsNone(ports(packet(6, 7171, 7171, frag=100)))

    def test_ipv6_and_garbage(self):
        self.assertFalse(is_game(b"\x60" + b"\0" * 60))
        self.assertFalse(is_game(b"\x45\x00"))

    def test_custom_ports(self):
        self.assertTrue(is_game(packet(6, 5, 9000), frozenset({9000})))


if __name__ == "__main__":
    unittest.main()
