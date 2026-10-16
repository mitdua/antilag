"""Tests for the server logic, without sockets or privileges."""

from __future__ import annotations

import select
import socket
import struct
import sys
import unittest

from antilag.proto import (
    DIR_UP,
    FLAG_DUP,
    KIND_DATA,
    KIND_PING,
    KIND_PONG,
    Codec,
    derive_key,
)
from antilag.registry import ClientEntry
from antilag.server import MAX_PATHS, TunnelServer, _UdpEndpoint

ANA = ClientEntry(id=1, name="ana", secret="secreto-de-ana", address="10.66.0.2")
BETO = ClientEntry(id=2, name="beto", secret="secreto-de-beto", address="10.66.0.3")
ETH = ("200.1.1.1", 40000)
WIFI = ("10.47.0.9", 50000)
BETO_ETH = ("190.2.2.2", 41000)
WORLD = "100.25.151.1"


def raw(a: str) -> bytes:
    return bytes(int(x) for x in a.split("."))


def udp(src: str, dst: str, sport: int, dport: int, body: bytes = b"") -> bytes:
    """Minimal IPv4/UDP packet; the server looks at version, IPs and ports."""
    total = 20 + 8 + len(body)
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total, 0, 0, 64, 17, 0, raw(src), raw(dst))
    return header + struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body


def to_game(src: str, body: bytes = b"") -> bytes:
    return udp(src, WORLD, 50000, 7171, body)


def from_game(dst: str, body: bytes = b"") -> bytes:
    return udp(WORLD, dst, 7171, 50000, body)


class FakeTun:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, packet: bytes) -> None:
        self.written.append(packet)


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def player(entry: ClientEntry) -> Codec:
    return Codec(derive_key(entry.secret), DIR_UP, client_id=entry.id, max_skew_s=None)


def make(*entries: ClientEntry):
    clock = Clock()
    tun = FakeTun()
    server = TunnelServer(list(entries or (ANA,)), tun, clock=clock)
    return server, tun, clock


class TestUpstream(unittest.TestCase):
    def test_first_copy_reaches_tun_once(self):
        server, tun, _ = make()
        _, wire = player(ANA).seal(to_game("10.66.0.2"))
        server.handle_datagram(wire, ETH)
        server.handle_datagram(wire, WIFI)
        self.assertEqual(len(tun.written), 1)
        self.assertEqual(set(server.sessions[1].paths), {ETH, WIFI})

    def test_only_game_ports_leave(self):
        """The VPS is not a general VPN: nothing that does not go to 7171/7172."""
        server, tun, _ = make()
        ana = player(ANA)
        server.handle_datagram(ana.seal(udp("10.66.0.2", "1.1.1.1", 5000, 443))[1], ETH)
        server.handle_datagram(ana.seal(udp("10.66.0.2", "1.1.1.1", 7171, 443))[1], ETH)
        self.assertEqual(tun.written, [])
        self.assertEqual(server.sessions[1].blocked, 2)
        server.handle_datagram(ana.seal(udp("10.66.0.2", WORLD, 5000, 7172))[1], ETH)
        self.assertEqual(len(tun.written), 1)

    def test_spoofed_source_dropped(self):
        server, tun, _ = make()
        server.handle_datagram(player(ANA).seal(to_game("8.8.8.8"))[1], ETH)
        self.assertEqual(tun.written, [])
        self.assertEqual(server.sessions[1].spoofed, 1)

    def test_non_ipv4_dropped(self):
        server, tun, _ = make()
        server.handle_datagram(player(ANA).seal(b"\x60" + b"\0" * 39)[1], ETH)
        self.assertEqual(tun.written, [])

    def test_garbage_is_ignored(self):
        server, _, _ = make()
        self.assertEqual(server.handle_datagram(b"\x00" * 64, ETH), [])
        self.assertEqual(server.rejected, 1)

    def test_ping_answered_on_same_path(self):
        server, _, _ = make()
        ana = player(ANA)
        replies = server.handle_datagram(ana.seal(b"token", kind=KIND_PING)[1], WIFI)
        self.assertEqual(len(replies), 1)
        wire, addr = replies[0]
        self.assertEqual(addr, WIFI)
        frame = ana.open(wire)
        self.assertEqual((frame.kind, frame.payload), (KIND_PONG, b"token"))

    def test_unknown_client_is_silent(self):
        server, tun, _ = make(ANA)
        replies = server.handle_datagram(player(BETO).seal(b"t", kind=KIND_PING)[1], ETH)
        self.assertEqual((replies, tun.written, server.unknown), ([], [], 1))


class TestDownstream(unittest.TestCase):
    def test_follows_duplicating_client(self):
        server, _, _ = make()
        _, wire = player(ANA).seal(to_game("10.66.0.2"), flags=FLAG_DUP)
        server.handle_datagram(wire, ETH)
        server.handle_datagram(wire, WIFI)
        sends = server.handle_tun_packet(from_game("10.66.0.2"))
        self.assertEqual({addr for _, addr in sends}, {ETH, WIFI})
        # a single sealed frame, replicated as is
        self.assertEqual(len({wire for wire, _ in sends}), 1)

    def test_follows_single_link_client(self):
        """If only one path carries data, the downlink uses only that one (saves mobile data)."""
        server, _, _ = make()
        ana = player(ANA)
        server.handle_datagram(ana.seal(to_game("10.66.0.2"))[1], ETH)
        server.handle_datagram(ana.seal(b"t", kind=KIND_PING)[1], WIFI)
        sends = server.handle_tun_packet(from_game("10.66.0.2"))
        self.assertEqual([addr for _, addr in sends], [ETH])

    def test_idle_client_uses_any_live_path(self):
        server, _, clock = make()
        ana = player(ANA)
        server.handle_datagram(ana.seal(to_game("10.66.0.2"))[1], ETH)
        clock.now += 5  # more than active_s, less than the ttl
        server.handle_datagram(ana.seal(b"t", kind=KIND_PING)[1], WIFI)
        sends = server.handle_tun_packet(from_game("10.66.0.2"))
        self.assertEqual({addr for _, addr in sends}, {ETH, WIFI})

    def test_dead_paths_expire(self):
        server, _, clock = make()
        server.handle_datagram(player(ANA).seal(to_game("10.66.0.2"))[1], ETH)
        clock.now += 60
        self.assertEqual(server.handle_tun_packet(from_game("10.66.0.2")), [])
        self.assertEqual(server.sessions[1].paths, {})

    def test_client_decodes_downstream(self):
        server, _, _ = make()
        ana = player(ANA)
        server.handle_datagram(ana.seal(to_game("10.66.0.2"))[1], ETH)
        packet = from_game("10.66.0.2", b"hola")
        wire, _ = server.handle_tun_packet(packet)[0]
        frame = ana.open(wire)
        self.assertEqual((frame.kind, frame.payload), (KIND_DATA, packet))

    def test_client_restart_forgets_old_paths(self):
        server, _, _ = make()
        server.handle_datagram(player(ANA).seal(to_game("10.66.0.2"))[1], ETH)
        server.handle_datagram(player(ANA).seal(to_game("10.66.0.2"))[1], WIFI)
        self.assertEqual(set(server.sessions[1].paths), {WIFI})

    def test_path_table_is_bounded(self):
        server, _, _ = make()
        ana = player(ANA)
        for port in range(MAX_PATHS + 5):
            server.handle_datagram(ana.seal(b"t", kind=KIND_PING)[1], ("1.2.3.4", port))
        self.assertEqual(len(server.sessions[1].paths), MAX_PATHS)


class TestSeveralPlayers(unittest.TestCase):
    """What happens to one player does not touch another."""

    def setUp(self):
        self.server, self.tun, self.clock = make(ANA, BETO)
        self.ana, self.beto = player(ANA), player(BETO)
        self.server.handle_datagram(self.ana.seal(to_game("10.66.0.2"))[1], ETH)
        self.server.handle_datagram(self.beto.seal(to_game("10.66.0.3"))[1], BETO_ETH)

    def test_both_play_at_once(self):
        self.assertEqual(len(self.tun.written), 2)

    def test_downstream_goes_to_its_owner(self):
        to_ana = self.server.handle_tun_packet(from_game("10.66.0.2", b"para ana"))
        to_beto = self.server.handle_tun_packet(from_game("10.66.0.3", b"para beto"))
        self.assertEqual([addr for _, addr in to_ana], [ETH])
        self.assertEqual([addr for _, addr in to_beto], [BETO_ETH])
        self.assertEqual(self.ana.open(to_ana[0][0]).payload[-8:], b"para ana")
        self.assertEqual(self.beto.open(to_beto[0][0]).payload[-9:], b"para beto")

    def test_restart_of_one_does_not_cut_the_other(self):
        """Before (single client) this retired ana's epoch forever."""
        restarted = player(BETO)
        self.server.handle_datagram(restarted.seal(to_game("10.66.0.3"))[1], BETO_ETH)
        before = len(self.tun.written)
        self.server.handle_datagram(self.ana.seal(to_game("10.66.0.2"))[1], ETH)
        self.assertEqual(len(self.tun.written), before + 1)

    def test_cannot_use_anothers_ip(self):
        self.server.handle_datagram(self.beto.seal(to_game("10.66.0.2"))[1], BETO_ETH)
        self.assertEqual(len(self.tun.written), 2)
        self.assertEqual(self.server.sessions[2].spoofed, 1)

    def test_cannot_forge_anothers_frames(self):
        """Beto knows ana's id but not her secret."""
        forger = Codec(derive_key(BETO.secret), DIR_UP, client_id=ANA.id, max_skew_s=None)
        self.server.handle_datagram(forger.seal(to_game("10.66.0.2"))[1], BETO_ETH)
        self.assertEqual(len(self.tun.written), 2)
        self.assertEqual(self.server.rejected, 1)
        self.assertNotIn(BETO_ETH, self.server.sessions[1].paths)

    def test_removed_player_is_cut_at_once(self):
        report = self.server.load([ANA])
        self.assertEqual(report["quitados"], ["beto"])
        self.server.handle_datagram(self.beto.seal(to_game("10.66.0.3"))[1], BETO_ETH)
        self.assertEqual(len(self.tun.written), 2)
        self.assertEqual(self.server.unknown, 1)
        self.assertEqual(self.server.handle_tun_packet(from_game("10.66.0.3")), [])

    def test_reload_keeps_untouched_sessions(self):
        session = self.server.sessions[1]
        third = ClientEntry(id=3, name="caro", secret="s3", address="10.66.0.4")
        report = self.server.load([ANA, BETO, third])
        self.assertEqual(report, {"agregados": ["caro"], "cambiados": [], "quitados": []})
        self.assertIs(self.server.sessions[1], session)

    def test_new_secret_replaces_session(self):
        changed = ClientEntry(id=2, name="beto", secret="otro", address="10.66.0.3")
        self.assertEqual(self.server.load([ANA, changed])["cambiados"], ["beto"])
        self.server.handle_datagram(self.beto.seal(to_game("10.66.0.3"))[1], BETO_ETH)
        self.assertEqual(len(self.tun.written), 2)  # the old secret no longer works


@unittest.skipUnless(sys.platform.startswith("linux"), "IP_PKTINFO y 127.0.0.2 son de Linux")
class TestReplySource(unittest.TestCase):
    """With several IPs (e.g. a reserved IP), answer from the one that was used."""

    def test_reply_comes_from_the_address_the_client_used(self):
        server = TunnelServer([ANA], FakeTun())
        endpoint = _UdpEndpoint(server, "0.0.0.0", 0)
        port = endpoint.sock.getsockname()[1]
        ana = player(ANA)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(("127.0.0.1", 0))
            # Connected, as behind a NAT: only accepts replies from 127.0.0.2.
            sock.connect(("127.0.0.2", port))
            sock.settimeout(2)
            sock.send(ana.seal(b"tok", kind=KIND_PING)[1])
            select.select([endpoint.sock], [], [], 2)
            endpoint.on_readable()
            frame = ana.open(sock.recv(2048))
            self.assertEqual((frame.kind, frame.payload), (KIND_PONG, b"tok"))
        finally:
            sock.close()
            endpoint.close()


if __name__ == "__main__":
    unittest.main()
