"""Tests for the client's link selection and fast suspicion."""

from __future__ import annotations

import socket
import struct
import unittest

from antilag.client import TunnelClient
from antilag.net.discovery import Link
from antilag.policy import Decision, Mode, PolicyEngine
from antilag.proto import DIR_UP, Codec, derive_key

ETH = Link(name="eth", address="127.0.0.1")
WIFI = Link(name="wifi", address="127.0.0.1")


def ipv4(proto: int, sport: int, dport: int) -> bytes:
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 28, 0, 0, 64, proto, 0,
                         b"\x0a\x42\x00\x02", b"\x01\x01\x01\x01")
    return header + struct.pack("!HHHH", sport, dport, 8, 0)


GAME = ipv4(6, 50000, 7171)
WEB = ipv4(6, 50000, 443)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def factory(link):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock, "bindtodevice"


class ClientTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.client = TunnelClient(
            Codec(derive_key("x"), DIR_UP, max_skew_s=None), tun=None,
            links=[ETH, WIFI], server=("127.0.0.1", 9), engine=PolicyEngine(mode=Mode.SINGLE),
            socket_factory=factory, clock=self.clock,
        )
        self.eth = self.client.paths["eth"]
        self.wifi = self.client.paths["wifi"]

    def tearDown(self):
        for path in self.client.paths.values():
            path.sock.close()

    def alive(self, *paths, rtt=100.0):
        for path in paths:
            path.health.record(rtt)

    def decide(self, *senders, mode=Mode.SINGLE):
        self.client.decision = Decision(primary=senders[0], senders=senders, mode=mode, reason="")

    def names(self, packet):
        return [p.name for p in self.client.senders_for(packet)]


class TestSenders(ClientTest):
    def test_startup_tries_every_link(self):
        self.assertEqual(self.names(GAME), ["eth", "wifi"])

    def test_duplicate_mode_duplicates_only_the_game(self):
        self.alive(self.eth, self.wifi)
        self.decide("eth", "wifi", mode=Mode.DUPLICATE)
        self.assertEqual(self.names(GAME), ["eth", "wifi"])
        self.assertEqual(self.names(WEB), ["eth"])

    def test_single_mode_uses_primary(self):
        self.alive(self.eth, self.wifi)
        self.decide("eth")
        self.assertEqual(self.names(GAME), ["eth"])

    def test_dead_primary_does_not_wait_for_policy(self):
        self.alive(self.wifi)
        self.decide("eth")
        self.assertEqual(self.names(WEB), ["wifi"])


class TestSuspicion(ClientTest):
    def ping(self, path, age_s):
        path.pending[struct.pack("!Q", len(path.pending))] = self.clock.now - age_s

    def test_late_ping_triggers_emergency_duplication(self):
        self.alive(self.eth, self.wifi)
        self.decide("eth")
        self.ping(self.eth, 0.3)  # threshold: 2*100 + 40 = 240ms
        self.client.check_suspects()
        self.assertEqual(self.names(GAME), ["eth", "wifi"])
        self.assertEqual(self.names(WEB), ["eth"], "solo se duplica el juego")

    def test_ping_within_threshold_is_not_suspect(self):
        self.alive(self.eth, self.wifi)
        self.decide("eth")
        self.ping(self.eth, 0.2)
        self.client.check_suspects()
        self.assertEqual(self.names(GAME), ["eth"])

    def test_emergency_holds_after_recovery(self):
        self.alive(self.eth, self.wifi)
        self.decide("eth")
        self.ping(self.eth, 0.3)
        self.client.check_suspects()
        self.eth.pending.clear()  # the pong arrived
        self.clock.now += 2.0
        self.assertEqual(self.names(GAME), ["eth", "wifi"])
        self.clock.now += 1.5
        self.assertEqual(self.names(GAME), ["eth"])

    def test_threshold_has_a_floor(self):
        self.alive(self.eth, rtt=1.0)
        self.assertEqual(self.eth.suspect_threshold_ms(), 80.0)


if __name__ == "__main__":
    unittest.main()
