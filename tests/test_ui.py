"""Tests for what the panel computes and stores, without opening windows."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from antilag import config as store
from antilag.ui import viewmodel as vm


def row(name="eth", alive=True, last=100.0, first=0):
    return {"name": name, "alive": alive, "last_rtt_ms": last if alive else None,
            "last_error": "" if alive else "timeout", "first": first}


class TestViewModel(unittest.TestCase):
    def test_rtt_tones(self):
        self.assertEqual(vm.rtt_tone(100), vm.GOOD)
        self.assertEqual(vm.rtt_tone(200), vm.WARN)
        self.assertEqual(vm.rtt_tone(300), vm.BAD)
        self.assertEqual(vm.rtt_tone(None), vm.BAD)

    def test_link_states(self):
        self.assertEqual(vm.link_state(row(alive=False), ["eth"], []).label, "CAÍDO")
        self.assertEqual(vm.link_state(row(), ["eth"], ["eth"]).label, "SOSPECHA")
        self.assertEqual(vm.link_state(row(), ["eth"], []).label, "EMISOR")
        self.assertEqual(vm.link_state(row(), ["wifi"], []).label, "EN ESPERA")

    def test_overall_status(self):
        self.assertEqual(vm.overall_status([row(), row("w")], [])[0], "CONECTADO")
        self.assertEqual(vm.overall_status([row(alive=False), row("w")], [])[0], "1 ENLACE CAÍDO")
        self.assertEqual(vm.overall_status([row(alive=False)], [])[0], "SIN ENLACES")
        self.assertEqual(vm.overall_status([row()], ["eth"])[0], "PROTEGIENDO")

    def test_win_shares(self):
        shares = vm.win_shares([row(first=3), row("w", first=1)])
        self.assertAlmostEqual(shares["eth"], 0.75)
        self.assertEqual(vm.win_shares([row()]), {"eth": 0.0})

    def test_bars_scale_is_fixed_and_right_aligned(self):
        history = vm.RttHistory(size=4)
        history.push([row(last=600.0)])
        history.push([row(alive=False)])
        bars = history.bars("eth", height=50)
        self.assertEqual(bars[:2], [(0.0, vm.IDLE), (0.0, vm.IDLE)])
        self.assertEqual(bars[2], (50.0, vm.BAD))   # 600ms fills the scale
        self.assertEqual(bars[3], (50.0, vm.BAD))   # loss: full red bar

    def test_small_rtt_still_visible(self):
        history = vm.RttHistory(size=1)
        history.push([row(last=0.5)])
        self.assertEqual(history.bars("eth", 50)[0][0], 2.0)

    def test_event_times_and_order(self):
        events = [{"at": 90.0, "reason": "a"}, {"at": 95.0, "reason": "b"}]
        lines = vm.event_lines(events, now_mono=100.0, now_wall=1_000_000.0)
        self.assertEqual([reason for _, reason in lines], ["b", "a"])

    def test_formats(self):
        self.assertEqual(vm.fmt_ms(None), "--")
        self.assertEqual(vm.fmt_ms(3.14), "3.1")
        self.assertEqual(vm.fmt_ms(104.6), "105")
        self.assertEqual(vm.fmt_count(1234), "1.234")
        self.assertEqual(vm.fmt_count(45_000), "45k")


class TestGameBanner(unittest.TestCase):
    IPS = ["100.25.151.1", "172.65.219.118"]

    def test_before_first_check_invites_to_play(self):
        title, _, tone = vm.game_banner(None, self.IPS)
        self.assertIn("puedes entrar", title)
        self.assertEqual(tone, vm.GOOD)

    def test_waiting_mentions_captured_ips(self):
        _, detail, _ = vm.game_banner({"checked": True, "inside": [], "outside": []}, self.IPS)
        self.assertIn("100.25.151.1", detail)

    def test_inside(self):
        title, _, tone = vm.game_banner(
            {"checked": True, "inside": ["100.25.151.1:7171"], "outside": []}, self.IPS)
        self.assertEqual((title, tone), ("Jugando a través del túnel", vm.GOOD))

    def test_outside_wins_and_explains(self):
        title, detail, tone = vm.game_banner(
            {"checked": True, "inside": ["100.25.151.1:7171"], "outside": ["5.5.5.5:7171"]},
            self.IPS)
        self.assertEqual(tone, vm.BAD)
        self.assertIn("fuera del túnel", title)
        self.assertIn("5.5.5.5:7171", detail)
        self.assertIn("vuelve a capturar", detail)


class TestSavedConfig(unittest.TestCase):
    def test_round_trip_and_secret_not_in_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.save(store.SavedConfig(server="1.2.3.4:51888", mode="adaptive",
                                         primary="Ethernet", secret="s3cr3t-largo"), tmp)
            with open(os.path.join(tmp, "config.json"), encoding="utf-8") as handle:
                raw = handle.read()
            self.assertNotIn("s3cr3t-largo", raw)
            loaded = store.load(tmp)
            self.assertEqual((loaded.server, loaded.mode, loaded.primary, loaded.secret),
                             ("1.2.3.4:51888", "adaptive", "Ethernet", "s3cr3t-largo"))

    def test_player_fields_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            store.save(store.SavedConfig(server="x:1", secret="s", client_id=3,
                                         address="10.66.0.4/24", name="amigo2"), tmp)
            loaded = store.load(tmp)
            self.assertEqual((loaded.client_id, loaded.address, loaded.name),
                             (3, "10.66.0.4/24", "amigo2"))

    def test_old_config_is_player_one(self):
        """What was saved before access codes existed still works."""
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as handle:
                json.dump({"server": "203.0.113.10:51888", "mode": "duplicate"}, handle)
            loaded = store.load(tmp)
            self.assertEqual((loaded.client_id, loaded.address), (1, "10.66.0.2/24"))

    def test_missing_or_corrupt_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(store.load(tmp).server, "")
            with open(os.path.join(tmp, "config.json"), "w") as handle:
                handle.write("{no es json")
            self.assertEqual(store.load(tmp).mode, "duplicate")

    def test_unreadable_secret_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "config.json"), "w") as handle:
                json.dump({"server": "x:1", "secret": "dpapi:@@no-base64@@"}, handle)
            loaded = store.load(tmp)
            self.assertEqual((loaded.server, loaded.secret), ("x:1", ""))


if __name__ == "__main__":
    unittest.main()
