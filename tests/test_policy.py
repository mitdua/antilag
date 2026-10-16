"""Tests for the policy engine: thresholds, hysteresis and no oscillation."""

from __future__ import annotations

import unittest
from dataclasses import dataclass

from antilag.policy import (
    Decision,
    Mode,
    PolicyEngine,
    Thresholds,
    is_degraded,
    is_recovered,
)


@dataclass
class FakeHealth:
    """Hand-controlled link health, for deterministic traces."""

    rtt_ms: float | None = 30.0
    jitter_ms: float = 1.0
    _loss_pct: float = 0.0
    alive: bool = True

    @property
    def loss_pct(self) -> float:
        return self._loss_pct


def cable(rtt=30.0, **kw) -> FakeHealth:
    return FakeHealth(rtt_ms=rtt, **kw)


def wifi(rtt=80.0, **kw) -> FakeHealth:
    return FakeHealth(rtt_ms=rtt, **kw)


class TestThresholds(unittest.TestCase):
    def test_hysteresis_is_enforced(self):
        """Without a margin between degrading and recovering, the system would oscillate."""
        with self.assertRaises(ValueError):
            Thresholds(rtt_ms=300.0, recover_rtt_ms=400.0)

    def test_defaults_match_the_problem(self):
        self.assertEqual(Thresholds().rtt_ms, 300.0)


class TestDegradation(unittest.TestCase):
    def setUp(self):
        self.t = Thresholds()

    def test_healthy_link(self):
        degraded, _ = is_degraded(cable(30), self.t)
        self.assertFalse(degraded)

    def test_high_rtt(self):
        degraded, why = is_degraded(cable(450), self.t)
        self.assertTrue(degraded)
        self.assertIn("rtt", why)

    def test_exactly_at_threshold_is_healthy(self):
        degraded, _ = is_degraded(cable(300.0), self.t)
        self.assertFalse(degraded)

    def test_loss_triggers(self):
        degraded, why = is_degraded(cable(30, _loss_pct=20.0), self.t)
        self.assertTrue(degraded)
        self.assertIn("perdida", why)

    def test_jitter_triggers(self):
        degraded, why = is_degraded(cable(30, jitter_ms=120.0), self.t)
        self.assertTrue(degraded)
        self.assertIn("jitter", why)

    def test_dead_link(self):
        degraded, why = is_degraded(cable(alive=False), self.t)
        self.assertTrue(degraded)
        self.assertIn("caido", why)

    def test_unmeasured_link(self):
        degraded, _ = is_degraded(cable(rtt=None), self.t)
        self.assertTrue(degraded)

    def test_recovery_is_stricter_than_degradation(self):
        """At 250ms it is no longer degraded, but it does not count as recovered yet."""
        middling = cable(250)
        self.assertFalse(is_degraded(middling, self.t)[0])
        self.assertFalse(is_recovered(middling, self.t))
        self.assertTrue(is_recovered(cable(150), self.t))


class TestRanking(unittest.TestCase):
    def test_orders_by_rtt(self):
        ranked = PolicyEngine.rank(
            {"wifi": wifi(80), "cable": cable(30), "usb": FakeHealth(rtt_ms=50)}
        )
        self.assertEqual(ranked, ["cable", "usb", "wifi"])

    def test_dead_links_excluded(self):
        ranked = PolicyEngine.rank({"cable": cable(alive=False), "wifi": wifi(80)})
        self.assertEqual(ranked, ["wifi"])

    def test_unmeasured_goes_last(self):
        ranked = PolicyEngine.rank({"nuevo": FakeHealth(rtt_ms=None), "wifi": wifi(80)})
        self.assertEqual(ranked, ["wifi", "nuevo"])

    def test_scales_past_two_links(self):
        healths = {f"l{i}": FakeHealth(rtt_ms=float(100 - i)) for i in range(5)}
        self.assertEqual(PolicyEngine.rank(healths)[0], "l4")


class TestNoLinks(unittest.TestCase):
    def test_no_links_at_all(self):
        engine = PolicyEngine()
        decision = engine.evaluate({}, now=0.0)
        self.assertIsNone(decision.primary)
        self.assertEqual(decision.senders, ())

    def test_all_dead(self):
        engine = PolicyEngine()
        decision = engine.evaluate({"cable": cable(alive=False)}, now=0.0)
        self.assertIsNone(decision.primary)


class TestSingleMode(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")

    def test_uses_pinned_primary_when_healthy(self):
        """The pinned link wins even if the Wi-Fi measures better."""
        decision = self.engine.evaluate({"cable": cable(30), "wifi": wifi(10)}, 0.0)
        self.assertEqual(decision.primary, "cable")
        self.assertEqual(decision.senders, ("cable",))

    def test_single_spike_does_not_switch(self):
        """An isolated spike must not move anything."""
        self.engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        decision = self.engine.evaluate({"cable": cable(900), "wifi": wifi(80)}, 0.5)
        self.assertEqual(decision.primary, "cable")

    def test_sustained_spike_switches(self):
        healths = {"cable": cable(30), "wifi": wifi(80)}
        self.engine.evaluate(healths, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        self.engine.evaluate(bad, 10.0)
        decision = self.engine.evaluate(bad, 13.0)  # sustained for 3s
        self.assertEqual(decision.primary, "wifi")
        self.assertTrue(decision.changed)

    def test_no_switch_without_healthy_alternative(self):
        """If both are bad, stay put: switching gains nothing and risks the session."""
        bad = {"cable": cable(900), "wifi": wifi(800)}
        self.engine.evaluate(bad, 0.0)
        decision = self.engine.evaluate(bad, 5.0)
        self.assertEqual(decision.primary, "cable")
        self.assertIn("no hay alternativa", decision.reason)

    def test_min_dwell_blocks_immediate_reswitch(self):
        healths = {"cable": cable(30), "wifi": wifi(80), "usb": FakeHealth(rtt_ms=90)}
        self.engine.evaluate(healths, 0.0)
        bad_cable = {"cable": cable(900), "wifi": wifi(80), "usb": FakeHealth(rtt_ms=90)}
        self.engine.evaluate(bad_cable, 10.0)
        decision = self.engine.evaluate(bad_cable, 13.0)
        self.assertEqual(decision.primary, "wifi")
        # The Wi-Fi also drops immediately: the cooldown must hold it back.
        bad_both = {
            "cable": cable(900), "wifi": wifi(900), "usb": FakeHealth(rtt_ms=90)
        }
        self.engine.evaluate(bad_both, 14.0)
        # At 17s the degradation has already been sustained for 3s, so the
        # remaining brake is the cooldown: only 4s have passed since the
        # previous switch.
        decision = self.engine.evaluate(bad_both, 17.0)
        self.assertEqual(decision.primary, "wifi")
        self.assertIn("enfriamiento", decision.reason)


class TestDuplicateMode(unittest.TestCase):
    def test_always_duplicates(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE)
        decision = engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        self.assertTrue(decision.duplicating)
        self.assertEqual(set(decision.senders), {"cable", "wifi"})

    def test_respects_max_senders(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE, max_senders=2)
        healths = {
            "cable": cable(30), "wifi": wifi(80), "usb": FakeHealth(rtt_ms=50)
        }
        decision = engine.evaluate(healths, 0.0)
        self.assertEqual(decision.senders, ("cable", "usb"))

    def test_single_link_is_not_duplicating(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE)
        decision = engine.evaluate({"cable": cable(30)}, 0.0)
        self.assertFalse(decision.duplicating)


class TestAdaptiveMode(unittest.TestCase):
    def setUp(self):
        self.engine = PolicyEngine(mode=Mode.ADAPTIVE, primary="cable")
        self.good = {"cable": cable(30), "wifi": wifi(80)}

    def test_stays_single_while_healthy(self):
        """Key for a hotspot on a metered plan: do not spend data for no reason."""
        decision = self.engine.evaluate(self.good, 0.0)
        self.assertFalse(decision.duplicating)
        self.assertEqual(decision.senders, ("cable",))

    def test_engages_duplication_on_sustained_spike(self):
        self.engine.evaluate(self.good, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        self.engine.evaluate(bad, 10.0)
        decision = self.engine.evaluate(bad, 13.0)
        self.assertTrue(decision.duplicating)
        self.assertEqual(set(decision.senders), {"cable", "wifi"})

    def test_holds_duplication_until_recovery_confirmed(self):
        self.engine.evaluate(self.good, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        self.engine.evaluate(bad, 10.0)
        self.engine.evaluate(bad, 13.0)
        # The cable comes back, but the hysteresis requires sustaining it for recover_s.
        decision = self.engine.evaluate(self.good, 14.0)
        self.assertTrue(decision.duplicating)
        decision = self.engine.evaluate(self.good, 20.0)
        self.assertTrue(decision.duplicating)
        decision = self.engine.evaluate(self.good, 30.0)
        self.assertFalse(decision.duplicating)

    def test_does_not_fall_back_to_the_link_that_failed(self):
        """When releasing duplication, do not go back to the link that just failed."""
        self.engine.evaluate(self.good, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        self.engine.evaluate(bad, 10.0)
        self.engine.evaluate(bad, 13.0)
        # The cable improves but is still weak; the Wi-Fi is clearly better.
        mixed = {"cable": cable(260), "wifi": wifi(80)}
        for t in (14.0, 20.0, 26.0, 40.0):
            decision = self.engine.evaluate(mixed, t)
        self.assertFalse(decision.duplicating)
        self.assertEqual(decision.primary, "wifi")

    def test_no_duplication_without_second_link(self):
        engine = PolicyEngine(mode=Mode.ADAPTIVE)
        bad = {"cable": cable(900)}
        engine.evaluate(bad, 0.0)
        decision = engine.evaluate(bad, 5.0)
        self.assertFalse(decision.duplicating)
        self.assertIn("sin segundo enlace", decision.reason)

    def test_dead_primary_moves_traffic(self):
        """Unplugging the cable live: the acceptance test."""
        self.engine.evaluate(self.good, 0.0)
        unplugged = {"cable": cable(alive=False), "wifi": wifi(80)}
        for t in (10.0, 13.0, 25.0, 40.0):
            decision = self.engine.evaluate(unplugged, t)
        self.assertEqual(decision.primary, "wifi")
        self.assertIn("wifi", decision.senders)


class TestFailback(unittest.TestCase):
    """Going back to the pinned primary: usually the one without mobile data."""

    def test_returns_to_pinned_once_stable(self):
        engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        engine.evaluate(bad, 10.0)
        decision = engine.evaluate(bad, 13.0)
        self.assertEqual(decision.primary, "wifi")
        # The cable comes back fully: return to it.
        good = {"cable": cable(40), "wifi": wifi(80)}
        decision = engine.evaluate(good, 30.0)
        self.assertEqual(decision.primary, "cable")
        self.assertIn("vuelta a la primaria", decision.reason)

    def test_no_failback_while_only_half_recovered(self):
        """At 250ms the cable is no longer degraded, but not good enough to go back to."""
        engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        engine.evaluate(bad, 10.0)
        engine.evaluate(bad, 13.0)
        decision = engine.evaluate({"cable": cable(250), "wifi": wifi(80)}, 40.0)
        self.assertEqual(decision.primary, "wifi")

    def test_failback_respects_cooldown(self):
        engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        bad = {"cable": cable(900), "wifi": wifi(80)}
        engine.evaluate(bad, 10.0)
        engine.evaluate(bad, 13.0)
        # Barely 1s after switching, it is not time to go back yet.
        decision = engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 14.0)
        self.assertEqual(decision.primary, "wifi")

    def test_no_pin_means_no_failback(self):
        engine = PolicyEngine(mode=Mode.SINGLE)
        engine.evaluate({"a": cable(30), "b": wifi(80)}, 0.0)
        bad = {"a": cable(900), "b": wifi(80)}
        engine.evaluate(bad, 10.0)
        engine.evaluate(bad, 13.0)
        decision = engine.evaluate({"a": cable(30), "b": wifi(80)}, 40.0)
        self.assertEqual(decision.primary, "b")

    def test_dead_pinned_link_is_not_restored(self):
        engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        unplugged = {"cable": cable(alive=False), "wifi": wifi(80)}
        for moment in (10.0, 13.0, 40.0):
            decision = engine.evaluate(unplugged, moment)
        self.assertEqual(decision.primary, "wifi")


class TestFlapResistance(unittest.TestCase):
    def test_oscillating_link_does_not_thrash(self):
        """A cable alternating good and bad must not produce one switch per sample."""
        engine = PolicyEngine(mode=Mode.SINGLE, primary="cable")
        now = 0.0
        for index in range(60):
            rtt = 900.0 if index % 2 == 0 else 30.0
            engine.evaluate({"cable": cable(rtt), "wifi": wifi(80)}, now)
            now += 1.0
        self.assertLessEqual(
            len(engine.history), 3, f"demasiadas conmutaciones: {engine.history}"
        )

    def test_borderline_rtt_does_not_thrash(self):
        """Hovering around the threshold is the classic oscillation case."""
        engine = PolicyEngine(mode=Mode.ADAPTIVE, primary="cable")
        now = 0.0
        for index in range(60):
            rtt = 310.0 if index % 2 == 0 else 290.0
            engine.evaluate({"cable": cable(rtt), "wifi": wifi(80)}, now)
            now += 1.0
        self.assertLessEqual(
            len(engine.history), 4, f"demasiadas conmutaciones: {engine.history}"
        )

    def test_history_is_bounded(self):
        engine = PolicyEngine(mode=Mode.SINGLE, history_limit=5)
        now = 0.0
        for index in range(200):
            healths = {
                "a": FakeHealth(rtt_ms=30.0 if index % 2 else 900.0),
                "b": FakeHealth(rtt_ms=900.0 if index % 2 else 30.0),
            }
            engine.evaluate(healths, now)
            now += 10.0
        self.assertLessEqual(len(engine.history), 5)


class TestSnapshot(unittest.TestCase):
    def test_snapshot_is_serialisable(self):
        import json

        engine = PolicyEngine(mode=Mode.ADAPTIVE, primary="cable")
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        payload = json.dumps(engine.snapshot())
        self.assertIn("adaptive", payload)

    def test_snapshot_reports_senders(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE)
        engine.evaluate({"cable": cable(30), "wifi": wifi(80)}, 0.0)
        snapshot = engine.snapshot()
        self.assertTrue(snapshot["duplicating"])
        self.assertEqual(len(snapshot["senders"]), 2)


if __name__ == "__main__":
    unittest.main()


class TestDuplicateOrdering(unittest.TestCase):
    """In duplicate mode: the preferred link goes first and RTT order makes no noise."""

    def test_pinned_link_leads_even_if_slower(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE, primary="eth")
        decision = engine.evaluate({"eth": FakeHealth(rtt_ms=120), "wifi": FakeHealth(rtt_ms=90)}, 0)
        self.assertEqual(decision.senders, ("eth", "wifi"))
        self.assertEqual(decision.primary, "eth")

    def test_rtt_reordering_is_not_an_event(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE)
        engine.evaluate({"a": FakeHealth(rtt_ms=90), "b": FakeHealth(rtt_ms=100)}, 0)
        before = len(engine.history)
        decision = engine.evaluate({"a": FakeHealth(rtt_ms=110), "b": FakeHealth(rtt_ms=100)}, 1)
        self.assertEqual(len(engine.history), before + 1, "cambio el primario: un evento")
        decision = engine.evaluate({"a": FakeHealth(rtt_ms=120), "b": FakeHealth(rtt_ms=100)}, 2)
        self.assertFalse(decision.changed)
        self.assertEqual(len(engine.history), before + 1)

    def test_all_links_duplicate_by_default_limit(self):
        engine = PolicyEngine(mode=Mode.DUPLICATE, max_senders=16)
        healths = {name: FakeHealth(rtt_ms=100 + i) for i, name in enumerate("abc")}
        self.assertEqual(len(engine.evaluate(healths, 0).senders), 3)
