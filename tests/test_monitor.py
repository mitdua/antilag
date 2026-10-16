"""Tests for the episode detector: spikes, outages and their durations."""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from antilag.monitor import (
    EPISODE_OUTAGE,
    EPISODE_SPIKE,
    EpisodeDetector,
    load_log,
    replay,
)


def feed(detector: EpisodeDetector, link: str, serie, inicio: float = 0.0, paso=1.0):
    """Feed a series of RTTs (None = lost sample)."""
    momento = inicio
    for rtt in serie:
        detector.observe(link, rtt, momento)
        momento += paso
    detector.close_all(momento)
    return momento


class TestConstruction(unittest.TestCase):
    def test_recover_must_not_exceed_spike(self):
        with self.assertRaises(ValueError):
            EpisodeDetector(spike_ms=300, recover_ms=400)

    def test_outage_after_must_be_positive(self):
        with self.assertRaises(ValueError):
            EpisodeDetector(outage_after=0)


class TestSpikes(unittest.TestCase):
    def test_healthy_series_has_no_episodes(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 110, 105, 98, 120])
        self.assertEqual(d.episodes, [])

    def test_single_spike_detected(self):
        """The case that describes the problem: a jump to 500ms."""
        d = EpisodeDetector()
        feed(d, "cable", [100, 500, 100])
        self.assertEqual(len(d.episodes), 1)
        episodio = d.episodes[0]
        self.assertEqual(episodio.kind, EPISODE_SPIKE)
        self.assertEqual(episodio.peak_rtt_ms, 500)

    def test_spike_peak_is_the_worst_value(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 350, 780, 420, 100])
        self.assertEqual(d.episodes[0].peak_rtt_ms, 780)

    def test_spike_duration(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 500, 500, 500, 100], paso=1.0)
        # Starts at t=1 and closes when dropping to 100 at t=4.
        self.assertAlmostEqual(d.episodes[0].duration_s(), 3.0)

    def test_hysteresis_keeps_one_episode(self):
        """Oscillating between 250 and 400 is a single episode, not four."""
        d = EpisodeDetector(spike_ms=300, recover_ms=200)
        feed(d, "cable", [100, 400, 250, 400, 250, 400, 100])
        self.assertEqual(len(d.episodes), 1)

    def test_below_threshold_is_not_a_spike(self):
        d = EpisodeDetector(spike_ms=300)
        feed(d, "cable", [100, 299, 100])
        self.assertEqual(d.episodes, [])

    def test_two_separate_spikes(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 500, 100, 100, 600, 100])
        self.assertEqual(len(d.episodes), 2)
        self.assertEqual([e.peak_rtt_ms for e in d.episodes], [500, 600])

    def test_spike_open_at_end_is_closed(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 500, 500])
        self.assertFalse(d.episodes[0].ongoing)


class TestOutages(unittest.TestCase):
    def test_single_loss_is_not_an_outage(self):
        """One lost packet is not an outage; it would be constant noise."""
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, None, 100])
        self.assertEqual(d.episodes, [])

    def test_consecutive_losses_make_an_outage(self):
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, None, None, 100])
        self.assertEqual(len(d.episodes), 1)
        self.assertEqual(d.episodes[0].kind, EPISODE_OUTAGE)

    def test_outage_is_dated_from_the_first_loss(self):
        """Dating the outage when it is confirmed would underestimate its duration."""
        d = EpisodeDetector(outage_after=3)
        feed(d, "cable", [100, None, None, None, None, 100], paso=1.0)
        corte = d.episodes[0]
        self.assertAlmostEqual(corte.started_at, 1.0)
        self.assertAlmostEqual(corte.duration_s(), 4.0)

    def test_long_outage_duration(self):
        """Several seconds without connection: certain death in the game."""
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100] + [None] * 8 + [100], paso=1.0)
        self.assertAlmostEqual(d.episodes[0].duration_s(), 8.0)

    def test_spike_degenerating_into_outage_is_two_episodes(self):
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, 500, None, None, 100])
        tipos = [e.kind for e in d.episodes]
        self.assertEqual(tipos, [EPISODE_SPIKE, EPISODE_OUTAGE])

    def test_outage_then_spike(self):
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, None, None, 500, 100])
        tipos = [e.kind for e in d.episodes]
        self.assertEqual(tipos, [EPISODE_OUTAGE, EPISODE_SPIKE])

    def test_outage_open_at_end_is_closed(self):
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, None, None])
        self.assertFalse(d.episodes[0].ongoing)


class TestMultipleLinks(unittest.TestCase):
    def test_links_are_independent(self):
        d = EpisodeDetector()
        for momento, (cable, wifi) in enumerate([(100, 90), (500, 90), (100, 90)]):
            d.observe("cable", cable, float(momento))
            d.observe("wifi", wifi, float(momento))
        d.close_all(3.0)
        self.assertEqual(len(d.episodes), 1)
        self.assertEqual(d.episodes[0].link, "cable")

    def test_summary_per_link(self):
        d = EpisodeDetector()
        for momento, (cable, wifi) in enumerate([(100, 90), (500, 900), (100, 90)]):
            d.observe("cable", cable, float(momento))
            d.observe("wifi", wifi, float(momento))
        d.close_all(3.0)
        self.assertEqual(d.summary("cable")["spikes"], 1)
        self.assertEqual(d.summary("wifi")["spikes"], 1)
        self.assertEqual(d.summary("cable")["worst_spike_ms"], 500)


class TestSummary(unittest.TestCase):
    def test_empty_summary(self):
        resumen = EpisodeDetector().summary("cable")
        self.assertEqual(resumen["spikes"], 0)
        self.assertEqual(resumen["outages"], 0)
        self.assertIsNone(resumen["worst_spike_ms"])
        self.assertEqual(resumen["longest_outage_s"], 0.0)

    def test_counts_and_totals(self):
        d = EpisodeDetector(outage_after=2)
        feed(d, "cable", [100, 500, 100, None, None, 100, 700, 100], paso=1.0)
        resumen = d.summary("cable")
        self.assertEqual(resumen["spikes"], 2)
        self.assertEqual(resumen["outages"], 1)
        self.assertEqual(resumen["worst_spike_ms"], 700)
        self.assertEqual(resumen["samples"], 8)

    def test_global_summary_spans_links(self):
        d = EpisodeDetector()
        feed(d, "cable", [100, 500, 100])
        feed(d, "wifi", [90, 600, 90])
        self.assertEqual(d.summary()["spikes"], 2)


class TestLogReplay(unittest.TestCase):
    def _write(self, rows) -> str:
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        for row in rows:
            handle.write(json.dumps(row) + "\n")
        handle.close()
        return handle.name

    def test_round_trip(self):
        rows = [
            {"t": 0.0, "links": {"cable": 100}},
            {"t": 1.0, "links": {"cable": 500}},
            {"t": 2.0, "links": {"cable": 100}},
        ]
        path = self._write(rows)
        try:
            self.assertEqual(len(load_log(path)), 3)
            detector = replay(load_log(path))
            self.assertEqual(len(detector.episodes), 1)
            self.assertEqual(detector.episodes[0].peak_rtt_ms, 500)
        finally:
            os.unlink(path)

    def test_truncated_line_is_skipped(self):
        """An abrupt stop can leave the last line half-written."""
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".jsonl", delete=False, encoding="utf-8"
        )
        handle.write('{"t": 0.0, "links": {"cable": 100}}\n')
        handle.write('{"t": 1.0, "links": {"cab')
        handle.close()
        try:
            self.assertEqual(len(load_log(handle.name)), 1)
        finally:
            os.unlink(handle.name)

    def test_replay_allows_different_thresholds(self):
        """Re-analyse without measuring again: the log keeps the raw RTTs."""
        rows = [
            {"t": 0.0, "links": {"cable": 100}},
            {"t": 1.0, "links": {"cable": 250}},
            {"t": 2.0, "links": {"cable": 100}},
        ]
        path = self._write(rows)
        try:
            log = load_log(path)
            self.assertEqual(len(replay(log, spike_ms=300).episodes), 0)
            self.assertEqual(len(replay(log, spike_ms=200, recover_ms=150).episodes), 1)
        finally:
            os.unlink(path)

    def test_losses_in_log(self):
        rows = [
            {"t": 0.0, "links": {"cable": 100}},
            {"t": 1.0, "links": {"cable": None}},
            {"t": 2.0, "links": {"cable": None}},
            {"t": 3.0, "links": {"cable": 100}},
        ]
        path = self._write(rows)
        try:
            detector = replay(load_log(path))
            self.assertEqual(detector.summary("cable")["outages"], 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
