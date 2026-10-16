"""Decides which link(s) the traffic goes out over.

One interface is set as primary by configuration and the rest act as a dynamic
fallback: second place goes to whichever measures the best RTT against the
VPS, not to whichever appears first in a file. It works the same with two
interfaces as with five.

The rules that keep this from oscillating:

hysteresis
    Degrading requires crossing the threshold; recovering requires dropping
    below another, stricter threshold. So a link hovering around 300ms does
    not cause flapping.
sustain
    A single-packet spike switches nothing; the degradation has to last
    several seconds.
minimum dwell and cooldown
    After switching, some time must pass before switching again.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable


class Mode(str, Enum):
    #: a single link, the best available. Saves mobile data.
    SINGLE = "single"
    #: several links at once; the effective latency is the minimum among them.
    DUPLICATE = "duplicate"
    #: single while the primary is healthy, duplicating as soon as it degrades.
    ADAPTIVE = "adaptive"


@runtime_checkable
class Health(Protocol):
    """The minimum the policy needs to know about a link."""

    rtt_ms: float | None
    jitter_ms: float
    alive: bool

    @property
    def loss_pct(self) -> float: ...


@dataclass(frozen=True)
class Thresholds:
    """Limits beyond which a link is considered degraded."""

    #: the threshold that makes Tibia unplayable
    rtt_ms: float = 300.0
    loss_pct: float = 5.0
    jitter_ms: float = 60.0
    #: seconds the degradation must last before acting
    sustain_s: float = 2.0
    #: to count as recovered it has to drop below this, not just below the threshold
    recover_rtt_ms: float = 200.0
    #: and stay healthy for this long
    recover_s: float = 10.0
    #: minimum time between two consecutive switches
    cooldown_s: float = 5.0
    #: minimum stay on a link before it can be abandoned
    min_dwell_s: float = 3.0

    def __post_init__(self) -> None:
        if self.recover_rtt_ms > self.rtt_ms:
            raise ValueError(
                "recover_rtt_ms debe ser menor que rtt_ms para que haya histeresis"
            )


@dataclass(frozen=True)
class Decision:
    """Result of an evaluation."""

    #: preferred link; when going back to single mode the traffic stays here
    primary: str | None
    #: links being sent over right now (more than one = duplicating)
    senders: tuple[str, ...]
    #: effective mode, already resolved (ADAPTIVE never shows up here)
    mode: Mode
    reason: str
    changed: bool = False

    @property
    def duplicating(self) -> bool:
        return len(self.senders) > 1


@dataclass
class SwitchEvent:
    at: float
    reason: str
    senders: tuple[str, ...]
    primary: str | None


def is_degraded(health: Health, thresholds: Thresholds) -> tuple[bool, str]:
    """Evaluate a link against the thresholds and explain the verdict."""
    if not health.alive:
        return True, "enlace caido"
    if health.rtt_ms is None:
        return True, "sin medicion aun"
    if health.rtt_ms > thresholds.rtt_ms:
        return True, f"rtt {health.rtt_ms:.0f}ms > {thresholds.rtt_ms:.0f}ms"
    if health.loss_pct > thresholds.loss_pct:
        return True, f"perdida {health.loss_pct:.1f}% > {thresholds.loss_pct:.1f}%"
    if health.jitter_ms > thresholds.jitter_ms:
        return True, f"jitter {health.jitter_ms:.0f}ms > {thresholds.jitter_ms:.0f}ms"
    return False, "sano"


def is_recovered(health: Health, thresholds: Thresholds) -> bool:
    """Stricter than the opposite of :func:`is_degraded`: this is the hysteresis."""
    if not health.alive or health.rtt_ms is None:
        return False
    return (
        health.rtt_ms <= thresholds.recover_rtt_ms
        and health.loss_pct <= thresholds.loss_pct
        and health.jitter_ms <= thresholds.jitter_ms
    )


class PolicyEngine:
    """State machine that turns link health into a decision."""

    def __init__(
        self,
        thresholds: Thresholds | None = None,
        mode: Mode = Mode.ADAPTIVE,
        primary: str | None = None,
        max_senders: int = 2,
        history_limit: int = 200,
    ) -> None:
        self.thresholds = thresholds or Thresholds()
        self.mode = mode
        #: link pinned by configuration; None = choose it by measurement
        self.pinned = primary
        self.max_senders = max(1, max_senders)
        self.history: list[SwitchEvent] = []
        self._history_limit = history_limit

        self._primary: str | None = None
        self._senders: tuple[str, ...] = ()
        self._degraded_since: float | None = None
        self._recovered_since: float | None = None
        self._recovered_link: str | None = None
        self._last_switch = -math.inf
        self._primary_since = -math.inf

    # -- selection ------------------------------------------------------- #

    @staticmethod
    def rank(healths: dict[str, Health]) -> list[str]:
        """Live links ordered by RTT; dead ones are left out.

        This order is the dynamic fallback: whoever holds second place does so
        by measurement against the VPS.
        """
        alive = [(key, h) for key, h in healths.items() if h.alive]
        return [
            key
            for key, _ in sorted(
                alive,
                key=lambda item: (
                    item[1].rtt_ms if item[1].rtt_ms is not None else math.inf
                ),
            )
        ]

    def _candidates(
        self, healths: dict[str, Health], ranked: list[str], strict: bool
    ) -> list[str]:
        """Acceptable links, in measurement order.

        ``strict`` requires having dropped below the recovery threshold, not
        just not being degraded.
        """
        if strict:
            return [key for key in ranked if is_recovered(healths[key], self.thresholds)]
        return [
            key for key in ranked if not is_degraded(healths[key], self.thresholds)[0]
        ]

    def _choose_primary(
        self,
        healths: dict[str, Health],
        ranked: list[str],
        strict: bool = False,
    ) -> str | None:
        """The pinned link if it is usable; otherwise the best measured one that is.

        ``strict`` is used when deciding whether duplication can be released.
        There it is not enough for the link not to be degraded: it has to have
        dropped below the recovery threshold. Otherwise a primary stuck in the
        middle band never confirms anything and duplication stays on forever,
        wasting mobile data for no reason.
        """
        if not ranked:
            return None
        acceptable = self._candidates(healths, ranked, strict)
        if self.pinned and self.pinned in ranked:
            if self.pinned in acceptable:
                return self.pinned
            # The pinned link is not usable: give up its place only if some other
            # link is. If none is, stay put; switching to an equally bad link
            # risks the game session for no gain.
            return acceptable[0] if acceptable else self.pinned
        return acceptable[0] if acceptable else ranked[0]

    def _can_switch(self, now: float) -> bool:
        return (
            now - self._last_switch >= self.thresholds.cooldown_s
            and now - self._primary_since >= self.thresholds.min_dwell_s
        )

    def _failback(
        self,
        healths: dict[str, Health],
        ranked: list[str],
        base: str,
        now: float,
        now_duplicating: bool = False,
    ) -> str | None:
        """The pinned primary, if it is already possible to go back to it.

        Going back requires real recovery, not just the absence of
        degradation: the user pinned that interface for a reason, typically
        because it is the one that does not use mobile data.
        """
        del now_duplicating
        if not self.pinned or base == self.pinned or self.pinned not in ranked:
            return None
        if not is_recovered(healths[self.pinned], self.thresholds):
            return None
        if not self._can_switch(now):
            return None
        return self.pinned

    def _commit(
        self,
        primary: str | None,
        senders: tuple[str, ...],
        mode: Mode,
        reason: str,
        now: float,
    ) -> Decision:
        changed = primary != self._primary or set(senders) != set(self._senders)
        if changed:
            if primary != self._primary:
                self._primary_since = now
            self._last_switch = now
            self.history.append(
                SwitchEvent(at=now, reason=reason, senders=senders, primary=primary)
            )
            del self.history[: max(0, len(self.history) - self._history_limit)]
        self._primary = primary
        self._senders = senders
        return Decision(
            primary=primary, senders=senders, mode=mode, reason=reason, changed=changed
        )

    # -- evaluation ------------------------------------------------------ #

    def evaluate(self, healths: dict[str, Health], now: float) -> Decision:
        """Decide which links to operate with at instant ``now``."""
        ranked = self.rank(healths)
        if not ranked:
            self._degraded_since = None
            self._recovered_since = None
            self._recovered_link = None
            return self._commit(None, (), Mode.SINGLE, "ningun enlace vivo", now)

        # The current primary stays in charge while it is alive: changing it for
        # no reason would waste the minimum dwell.
        current = self._primary if self._primary in ranked else None
        base = current or self._choose_primary(healths, ranked)
        degraded, why = is_degraded(healths[base], self.thresholds)

        # Sustain: a single-packet spike triggers nothing.
        if degraded:
            if self._degraded_since is None:
                self._degraded_since = now
        else:
            self._degraded_since = None

        # Recovery is measured on the link where the traffic would settle, not on
        # the one carrying it now. If the current one is stuck in the middle
        # band, another one has to confirm stability.
        settle = self._choose_primary(healths, ranked, strict=True) or base
        if is_recovered(healths[settle], self.thresholds):
            if self._recovered_since is None or self._recovered_link != settle:
                self._recovered_since = now
                self._recovered_link = settle
        else:
            self._recovered_since = None
            self._recovered_link = None

        sustained = (
            self._degraded_since is not None
            and now - self._degraded_since >= self.thresholds.sustain_s
        )

        if self.mode is Mode.DUPLICATE:
            # The first in the list carries what is not duplicated: the preferred
            # link if it is alive (typically the one without mobile data),
            # otherwise the best RTT. RTT reordering raises no events: only
            # which links are present matters, not their order.
            primary = self.pinned if self.pinned in ranked else ranked[0]
            ordered = [primary] + [key for key in ranked if key != primary]
            senders = tuple(ordered[: self.max_senders])
            down = [key for key in healths if key not in ranked]
            reason = f"duplicando por {', '.join(senders)}"
            if down:
                reason += f"; sin respuesta: {', '.join(down)}"
            return self._commit(primary, senders, Mode.DUPLICATE, reason, now)

        if self.mode is Mode.SINGLE:
            return self._evaluate_single(
                healths, ranked, base, settle, sustained, why, now
            )
        return self._evaluate_adaptive(
            healths, ranked, base, settle, sustained, why, now
        )

    def _evaluate_single(
        self,
        healths: dict[str, Health],
        ranked: list[str],
        base: str,
        settle: str,
        sustained: bool,
        why: str,
        now: float,
    ) -> Decision:
        if sustained:
            alternatives = [
                key
                for key in self._candidates(healths, ranked, strict=False)
                if key != base
            ]
            if not alternatives:
                return self._commit(
                    base, (base,), Mode.SINGLE,
                    f"{base} degradado ({why}) pero no hay alternativa sana", now,
                )
            if not self._can_switch(now):
                return self._commit(
                    base, (base,), Mode.SINGLE,
                    f"{base} degradado ({why}); esperando enfriamiento", now,
                )
            target = alternatives[0]
            self._degraded_since = None
            return self._commit(
                target, (target,), Mode.SINGLE,
                f"conmutado de {base} a {target}: {why}", now,
            )

        restored = self._failback(healths, ranked, base, now)
        if restored:
            return self._commit(
                restored, (restored,), Mode.SINGLE,
                f"vuelta a la primaria {restored}, ya estable", now,
            )
        return self._commit(base, (base,), Mode.SINGLE, f"{base} {why}", now)

    def _evaluate_adaptive(
        self,
        healths: dict[str, Health],
        ranked: list[str],
        base: str,
        settle: str,
        sustained: bool,
        why: str,
        now: float,
    ) -> Decision:
        duplicating = len(self._senders) > 1

        if sustained and len(ranked) > 1:
            # Duplicating protects during the spike without waiting to find out
            # which link is better: the other end's dedup keeps the first copy
            # that arrives.
            senders = tuple(ranked[: self.max_senders])
            return self._commit(
                base, senders, Mode.DUPLICATE, f"duplicando: {base} {why}", now
            )

        if duplicating:
            confirmed = (
                self._recovered_since is not None
                and now - self._recovered_since >= self.thresholds.recover_s
            )
            if not confirmed:
                senders = tuple(ranked[: self.max_senders])
                return self._commit(
                    base, senders, Mode.DUPLICATE,
                    f"sigue duplicando hasta confirmar la recuperacion de {settle}",
                    now,
                )
            # When releasing duplication, settle on the link that confirmed
            # stability, not on the one that just failed.
            self._recovered_since = None
            self._recovered_link = None
            return self._commit(
                settle, (settle,), Mode.SINGLE,
                f"{settle} estable; duplicacion retirada", now,
            )

        if sustained:
            return self._commit(
                base, (base,), Mode.SINGLE,
                f"{base} degradado ({why}) y sin segundo enlace", now,
            )

        restored = self._failback(healths, ranked, base, now)
        if restored:
            return self._commit(
                restored, (restored,), Mode.SINGLE,
                f"vuelta a la primaria {restored}, ya estable", now,
            )
        return self._commit(base, (base,), Mode.SINGLE, f"{base} {why}", now)

    def snapshot(self) -> dict:
        return {
            "mode": self.mode.value,
            "pinned": self.pinned,
            "primary": self._primary,
            "senders": list(self._senders),
            "duplicating": len(self._senders) > 1,
            "max_senders": self.max_senders,
            "thresholds": {
                "rtt_ms": self.thresholds.rtt_ms,
                "recover_rtt_ms": self.thresholds.recover_rtt_ms,
                "loss_pct": self.thresholds.loss_pct,
                "jitter_ms": self.thresholds.jitter_ms,
                "sustain_s": self.thresholds.sustain_s,
            },
            "events": [
                {
                    "at": event.at,
                    "reason": event.reason,
                    "senders": list(event.senders),
                    "primary": event.primary,
                }
                for event in self.history[-20:]
            ],
        }
