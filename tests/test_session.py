"""Tests for the session's preliminary steps and watchers."""

from __future__ import annotations

import select
import socket
import sys
import threading
import unittest
from unittest import mock

from antilag import session as sess
from antilag.net.connections import Connection
from antilag.net.discovery import Link
from antilag.registry import ClientEntry
from antilag.server import TunnelServer, _UdpEndpoint

ETH = Link(name="Ethernet", address="10.0.0.55", gateway="10.0.0.1", index=22)
WIFI = Link(name="Wi-Fi", address="10.47.197.115", gateway="10.47.197.217", index=10)


def conn(local: str, remote: str, port: int = 7171, process: str = "client") -> Connection:
    return Connection(process=process, pid=1, remote_address=remote, remote_port=port,
                      local_address=local)


class TestDiffLinks(unittest.TestCase):
    def test_new_link_is_added(self):
        usb = Link(name="Ethernet 4", address="192.168.42.10", gateway="192.168.42.129", index=31)
        added, changed, gone = sess.diff_links({"Ethernet": ETH}, set(), [ETH, usb])
        self.assertEqual((added, changed, gone), ([usb], [], []))

    def test_new_ip_is_a_change(self):
        moved = Link(name="Wi-Fi", address="10.47.197.90", gateway="10.47.197.217", index=10)
        added, changed, gone = sess.diff_links({"Wi-Fi": WIFI}, set(), [moved])
        self.assertEqual((added, changed, gone), ([], [moved], []))

    def test_missing_link_is_gone_once(self):
        known = {"Ethernet": ETH, "Wi-Fi": WIFI}
        self.assertEqual(sess.diff_links(known, set(), [ETH])[2], ["Wi-Fi"])
        self.assertEqual(sess.diff_links(known, {"Wi-Fi"}, [ETH])[2], [])

    def test_returning_link_gets_a_new_socket(self):
        """Same IP, but Windows removed it and gave it back: the old socket is useless."""
        added, changed, _ = sess.diff_links({"Wi-Fi": WIFI}, {"Wi-Fi"}, [WIFI])
        self.assertEqual((added, changed), ([], [WIFI]))

    def test_unchanged_is_left_alone(self):
        self.assertEqual(sess.diff_links({"Ethernet": ETH}, set(), [ETH]), ([], [], []))


class TestGameStatus(unittest.TestCase):
    def test_inside_and_outside(self):
        status = sess.classify_game(
            [conn("10.66.0.2", "100.25.151.1"), conn("192.0.2.50", "172.65.219.118")],
            "10.66.0.2",
        )
        self.assertEqual(status.inside, ["100.25.151.1:7171"])
        self.assertEqual(status.outside, ["172.65.219.118:7171"])

    def test_linux_local_with_port(self):
        status = sess.classify_game([conn("10.66.0.2:51234", "100.25.151.1")], "10.66.0.2")
        self.assertEqual(status.inside, ["100.25.151.1:7171"])


class TestCapture(unittest.TestCase):
    def test_only_game_ports_and_unique(self):
        conns = [
            conn("10.0.0.55", "100.25.151.1"),
            conn("10.0.0.55", "172.65.219.118"),
            conn("10.0.0.55", "100.25.151.1", port=7172),
        ]
        with mock.patch.object(sess, "game_connections", return_value=conns):
            self.assertEqual(sess.capture_game_ips(), ["100.25.151.1", "172.65.219.118"])

    def test_https_of_the_client_is_not_captured(self):
        """The login to www.tibia.com:443 must stay out of the tunnel."""
        from antilag.net import connections

        raw = [conn("10.0.0.55", "104.18.10.10", port=443), conn("10.0.0.55", "100.25.151.1")]
        with mock.patch.object(connections, "established", return_value=raw):
            found = connections.game_connections(platform="linux")
        self.assertEqual([c.remote_address for c in found], ["100.25.151.1"])

    def test_routes_for(self):
        self.assertEqual(sess.routes_for(["1.2.3.4", "5.6.7.8"]), "1.2.3.4/32,5.6.7.8/32")


class TestConfig(unittest.TestCase):
    """World IPs only: antilag is not a general VPN."""

    def cfg(self, routes: str) -> sess.TunnelConfig:
        return sess.TunnelConfig(server="1.2.3.4:51888", secret="s", routes=routes)

    def test_world_ips_accepted(self):
        self.assertEqual(self.cfg("100.25.151.1/32, 172.65.219.118").game_prefixes(),
                         ("100.25.151.1/32", "172.65.219.118/32"))

    def test_wide_prefixes_rejected(self):
        for routes in ("0.0.0.0/1", "128.0.0.0/1", "100.25.0.0/16", "full"):
            with self.assertRaises(sess.SessionError, msg=routes):
                self.cfg(routes).game_prefixes()

    def test_empty_explains(self):
        with self.assertRaises(sess.SessionError) as ctx:
            self.cfg("").game_prefixes()
        self.assertIn("IPs del mundo", str(ctx.exception))

    def test_from_access_code(self):
        from antilag.registry import Registry, make_access_code

        reg = Registry(endpoint="203.0.113.10:51888")
        reg.add("principal")
        entry = reg.add("amigo1")
        cfg = sess.TunnelConfig.from_access_code(make_access_code(reg, entry),
                                                 routes="100.25.151.1/32")
        self.assertEqual((cfg.server, cfg.client_id, cfg.address, cfg.name),
                         ("203.0.113.10:51888", 2, "10.66.0.3/24", "amigo1"))
        self.assertEqual(cfg.secret, entry.secret)

    def test_bad_access_code(self):
        with self.assertRaises(sess.SessionError):
            sess.TunnelConfig.from_access_code("antilag1:roto")


@unittest.skipUnless(sys.platform.startswith("linux"), "el servidor del VPS es solo Linux")
class TestProbeLinks(unittest.TestCase):
    """Validation against a real server on localhost."""

    def setUp(self):
        entry = ClientEntry(id=4, name="amigo", secret="correcto", address="10.66.0.5")
        self.server = TunnelServer([entry], tun=None)
        self.endpoint = _UdpEndpoint(self.server, "127.0.0.1", 0)
        self.addr = self.endpoint.sock.getsockname()
        self.stop = threading.Event()

        def pump():
            while not self.stop.is_set():
                ready, _, _ = select.select([self.endpoint.sock], [], [], 0.05)
                if ready:
                    self.endpoint.on_readable()

        self.thread = threading.Thread(target=pump, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.stop.set()
        self.thread.join(1)
        self.endpoint.close()

    @staticmethod
    def factory(link):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        return sock, "bindtodevice"

    def test_right_secret_answers(self):
        checks = sess.probe_links(self.addr, "correcto", [ETH, WIFI], client_id=4,
                                  timeout=0.5, rounds=2, socket_factory=self.factory)
        self.assertTrue(all(c.ok for c in checks), checks)
        self.assertTrue(all(c.rtt_ms < 500 for c in checks))

    def test_wrong_secret_looks_like_silence(self):
        checks = sess.probe_links(self.addr, "equivocado", [ETH], client_id=4,
                                  timeout=0.3, rounds=1, socket_factory=self.factory)
        self.assertFalse(checks[0].ok)
        self.assertIn("sin respuesta", checks[0].error)

    def test_revoked_or_wrong_id_looks_like_silence(self):
        checks = sess.probe_links(self.addr, "correcto", [ETH], client_id=9,
                                  timeout=0.3, rounds=1, socket_factory=self.factory)
        self.assertFalse(checks[0].ok)
        self.assertIn("sin respuesta", checks[0].error)


if __name__ == "__main__":
    unittest.main()
