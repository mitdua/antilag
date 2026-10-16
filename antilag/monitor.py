"""Episode detection: latency spikes and connection outages.

An average is useless for this problem. A link that spends 99% of the time at
100ms and 1% at 500ms averages 104ms and looks healthy, but that 1% is exactly
where the character dies. What has to be measured is the shape of the
episodes: how many there are, how long they last and how far they go.

Two kinds of episode:

spike
    The RTT goes over the threshold. It ends when it drops below the recovery
    threshold, which is stricter, so oscillation does not chop one episode into
    pieces.
outage
    Several consecutive samples without a reply. The serious case: a few
    seconds without connection in Tibia is certain death.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

EPISODE_SPIKE = "pico"
EPISODE_OUTAGE = "corte"

DEFAULT_SPIKE_MS = 300.0
DEFAULT_RECOVER_MS = 200.0
#: consecutive lost samples before declaring an outage. With a 1s interval,
#: two samples are ~2s, which is already dangerous in game.
DEFAULT_OUTAGE_AFTER = 2


@dataclass
class Episode:
    """A continuous stretch during which the link was bad."""

    link: str
    kind: str
    started_at: float
    ended_at: float | None = None
    #: the worst RTT seen during a spike
    peak_rtt_ms: float | None = None
    #: samples that fell inside the episode
    samples: int = 0

    @property
    def ongoing(self) -> bool:
        return self.ended_at is None

    def duration_s(self, now: float | None = None) -> float:
        end = self.ended_at if self.ended_at is not None else now
        if end is None:
            return 0.0
        return max(0.0, end - self.started_at)

    def as_dict(self, now: float | None = None) -> dict:
        return {
            "link": self.link,
            "kind": self.kind,
            "started_at": round(self.started_at, 3),
            "duration_s": round(self.duration_s(now), 1),
            "peak_rtt_ms": (
                round(self.peak_rtt_ms, 1) if self.peak_rtt_ms is not None else None
            ),
            "samples": self.samples,
            "ongoing": self.ongoing,
        }


class EpisodeDetector:
    """Turn a link's successive samples into episodes.

    Fed one sample at a time with :meth:`observe`. An ``rtt_ms`` of ``None``
    stands for a lost sample.
    """

    def __init__(
        self,
        spike_ms: float = DEFAULT_SPIKE_MS,
        recover_ms: float = DEFAULT_RECOVER_MS,
        outage_after: int = DEFAULT_OUTAGE_AFTER,
    ) -> None:
        if recover_ms > spike_ms:
            raise ValueError("recover_ms debe ser menor o igual que spike_ms")
        if outage_after < 1:
            raise ValueError("outage_after debe ser al menos 1")
        self.spike_ms = spike_ms
        self.recover_ms = recover_ms
        self.outage_after = outage_after
        self.episodes: list[Episode] = []
        self._open: dict[str, Episode] = {}
        self._losses: dict[str, int] = {}
        self._loss_started: dict[str, float] = {}
        self.samples: dict[str, int] = {}

    def _close(self, link: str, at: float) -> Episode | None:
        episode = self._open.pop(link, None)
        if episode is not None:
            episode.ended_at = at
        return episode

    def observe(self, link: str, rtt_ms: float | None, now: float) -> Episode | None:
        """Record a sample. Returns the episode that just closed, if any."""
        self.samples[link] = self.samples.get(link, 0) + 1
        current = self._open.get(link)

        if rtt_ms is None:
            self._losses[link] = self._losses.get(link, 0) + 1
            # The outage is dated at the first loss, not when it is confirmed;
            # otherwise its duration would be underestimated exactly in the
            # serious case.
            self._loss_started.setdefault(link, now)
            if current is not None and current.kind == EPISODE_SPIKE:
                # A spike that degenerates into an outage counts as two separate episodes.
                closed = self._close(link, now)
            else:
                closed = None

            if self._losses[link] >= self.outage_after:
                outage = self._open.get(link)
                if outage is None or outage.kind != EPISODE_OUTAGE:
                    outage = Episode(
                        link=link,
                        kind=EPISODE_OUTAGE,
                        started_at=self._loss_started[link],
                    )
                    self._open[link] = outage
                    self.episodes.append(outage)
                outage.samples = self._losses[link]
            return closed

        # A reply arrived: any streak of losses is over.
        self._losses[link] = 0
        self._loss_started.pop(link, None)
        closed = None
        if current is not None and current.kind == EPISODE_OUTAGE:
            closed = self._close(link, now)
            current = None

        if current is None:
            if rtt_ms > self.spike_ms:
                spike = Episode(
                    link=link,
                    kind=EPISODE_SPIKE,
                    started_at=now,
                    peak_rtt_ms=rtt_ms,
                    samples=1,
                )
                self._open[link] = spike
                self.episodes.append(spike)
            return closed

        # Spike in progress: it extends until it drops below the recovery threshold.
        current.samples += 1
        if current.peak_rtt_ms is None or rtt_ms > current.peak_rtt_ms:
            current.peak_rtt_ms = rtt_ms
        if rtt_ms <= self.recover_ms:
            closed = self._close(link, now)
        return closed

    def close_all(self, now: float) -> None:
        """Close the open episodes; called when the capture ends."""
        for link in list(self._open):
            self._close(link, now)

    def summary(self, link: str | None = None, now: float | None = None) -> dict:
        """Summary of the episodes, for the final report."""
        chosen = [
            e for e in self.episodes if link is None or e.link == link
        ]
        spikes = [e for e in chosen if e.kind == EPISODE_SPIKE]
        outages = [e for e in chosen if e.kind == EPISODE_OUTAGE]
        peaks = [e.peak_rtt_ms for e in spikes if e.peak_rtt_ms is not None]
        return {
            "link": link,
            "samples": self.samples.get(link, 0) if link else sum(self.samples.values()),
            "spikes": len(spikes),
            "spike_seconds": round(sum(e.duration_s(now) for e in spikes), 1),
            "worst_spike_ms": round(max(peaks), 1) if peaks else None,
            "outages": len(outages),
            "outage_seconds": round(sum(e.duration_s(now) for e in outages), 1),
            "longest_outage_s": (
                round(max(e.duration_s(now) for e in outages), 1) if outages else 0.0
            ),
        }


def load_log(path: str) -> list[dict]:
    """Read a JSONL log written by ``antilag watch``."""
    rows: list[dict] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # line truncated by an abrupt stop
    return rows


def replay(
    rows: list[dict],
    spike_ms: float = DEFAULT_SPIKE_MS,
    recover_ms: float = DEFAULT_RECOVER_MS,
    outage_after: int = DEFAULT_OUTAGE_AFTER,
) -> EpisodeDetector:
    """Rebuild the episodes from an already captured log.

    Allows re-analysing with other thresholds without measuring again, which
    is what you need when you do not know in advance where to set the cutoff.
    """
    detector = EpisodeDetector(spike_ms, recover_ms, outage_after)
    last = 0.0
    for row in rows:
        moment = float(row.get("t", last))
        for name, rtt in (row.get("links") or {}).items():
            detector.observe(name, rtt, moment)
        last = moment
    detector.close_all(last)
    return detector
