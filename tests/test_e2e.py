"""Runs the network-namespace end-to-end scripts without root."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "e2e_netns.py")
CLIENT_SCRIPT = os.path.join(HERE, "e2e_client.py")


def _netns_available() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("unshare"):
        return False
    probe = subprocess.run(
        ["unshare", "-rn", "ip", "tuntap", "add", "dev", "probe0", "mode", "tun"],
        capture_output=True,
    )
    return probe.returncode == 0


@unittest.skipUnless(_netns_available(), "requiere Linux con namespaces de usuario y /dev/net/tun")
class TestEndToEnd(unittest.TestCase):
    def test_tunnel_through_real_tun(self):
        result = subprocess.run(
            ["unshare", "-rn", sys.executable, SCRIPT],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("OK", result.stdout)


@unittest.skipUnless(_netns_available(), "requiere Linux con namespaces de usuario y /dev/net/tun")
@unittest.skipUnless(os.environ.get("ANTILAG_E2E"), "lenta (~40s por modo): ANTILAG_E2E=1 para correrla")
class TestClientEndToEnd(unittest.TestCase):
    """Real client and server, veth links, spikes, outages and several players."""

    def run_mode(self, mode: str, scenario: str = "pico+corte"):
        result = subprocess.run(
            ["unshare", "-rn", "--fork", sys.executable, CLIENT_SCRIPT, mode, scenario],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])

    def test_duplicate_hides_spike_and_cut(self):
        self.run_mode("duplicate")

    def test_adaptive_survives_cut(self):
        self.run_mode("adaptive", "corte")

    def test_links_that_appear_or_change_ip_join_live(self):
        self.run_mode("duplicate", "redescubrimiento")

    def test_two_players_do_not_disturb_each_other(self):
        self.run_mode("duplicate", "dos-jugadores")


if __name__ == "__main__":
    unittest.main()
