"""Simulated session to see and test the panel without a VPS or administrator.

Mimics the interface of :class:`antilag.session.TunnelSession` and produces
snapshots shaped like the real client's. The script repeats every 40s: the
cable gets a 500ms spike and then a 4s outage, which is the phenomenon measured
in Venezuela.
"""

from __future__ import annotations

import random
import time

from ..policy import Mode

_CYCLE_S = 40.0


class DemoSession:
    def __init__(self, config) -> None:
        self.config = config
        self.running = False
        self.links = []
        self._started = 0.0
        self._last = 0.0
        self._rng = random.Random(7)
        self._events: list[dict] = []
        self._state = {
            name: {"rtt": None, "last": None, "jitter": 4.0, "history": [], "first": 0,
                   "tx": 0, "late": 0, "sent": 0, "received": 0}
            for name in ("Ethernet", "Wi-Fi")
        }
        self._counters = {"up": 0, "dup": 0, "down": 0, "discarded": 0, "emergency": 0}
        self._phase = "sano"

    def start(self) -> None:
        time.sleep(0.6)  # let the "connecting" state be seen
        self.running = True
        self._started = self._last = time.monotonic()
        self._event("tunel arriba: los dos enlaces responden")

    def stop(self) -> None:
        self.running = False

    def _event(self, reason: str) -> None:
        self._events.append({"at": time.monotonic(), "reason": reason, "senders": [],
                             "primary": "Ethernet"})

    def _phase_at(self, t: float) -> str:
        t %= _CYCLE_S
        if 12 <= t < 18:
            return "pico"
        if 28 <= t < 32:
            return "corte"
        return "sano"

    def _sample(self, name: str, phase: str) -> float | None:
        if name == "Ethernet":
            if phase == "corte":
                return None
            base = 480.0 if phase == "pico" else 104.0
        else:
            base = 138.0
        return max(1.0, self._rng.gauss(base, base * 0.06))

    def snapshot(self) -> dict | None:
        if not self.running:
            return None
        now = time.monotonic()
        phase = self._phase_at(now - self._started)
        if phase != self._phase:
            self._event({
                "pico": "sospecha en Ethernet: ping sin respuesta hace 260ms",
                "corte": "Ethernet sin respuesta: el juego sigue por Wi-Fi",
                "sano": "Ethernet estable de nuevo",
            }[phase])
            self._phase = phase

        mode = self.config.mode
        rows = []
        samples = {name: self._sample(name, phase) for name in self._state}
        alive = [n for n, v in samples.items() if v is not None]
        winner = min(alive, key=lambda n: samples[n]) if alive else None
        packets = self._rng.randint(18, 30)
        dup = mode == Mode.DUPLICATE.value or phase != "sano"
        for name, state in self._state.items():
            value = samples[name]
            state["sent"] += 5
            state["history"] = (state["history"] + [value])[-60:]
            if value is not None:
                state["received"] += 5
                if state["last"] is not None:
                    state["jitter"] += (abs(value - state["last"]) - state["jitter"]) / 16
                state["last"] = value
                state["rtt"] = value if state["rtt"] is None else 0.2 * value + 0.8 * state["rtt"]
            sending = dup or name == "Ethernet" or "Ethernet" not in alive
            if sending:
                state["tx"] += packets
            if name == winner:
                state["first"] += packets
            elif sending and value is not None:
                state["late"] += packets
            got = [v for v in state["history"] if v is not None]
            lost = sum(1 for v in state["history"] if v is None)
            rows.append({
                "name": name,
                "kind": "ethernet" if name == "Ethernet" else "wifi",
                "address": "10.0.0.55" if name == "Ethernet" else "10.47.197.115",
                "strategy": "unicast_if",
                "trustworthy": True,
                "rtt_ms": round(state["rtt"], 1) if state["rtt"] is not None else None,
                "last_rtt_ms": round(value, 1) if value is not None else None,
                "p95_ms": round(sorted(got)[int(0.95 * (len(got) - 1))], 1) if got else None,
                "jitter_ms": round(state["jitter"], 1),
                "loss_pct": round(100.0 * lost / len(state["history"]), 1),
                "sent": state["sent"],
                "received": state["received"],
                "alive": value is not None,
                "last_error": "" if value is not None else "timeout",
                "tx": state["tx"],
                "first": state["first"],
                "late": state["late"],
                "send_errors": 0,
            })

        self._counters["up"] += packets
        self._counters["down"] += packets
        if dup:
            self._counters["dup"] += packets
            self._counters["discarded"] += packets if len(alive) > 1 else 0
        if phase != "sano" and mode != Mode.DUPLICATE.value:
            self._counters["emergency"] += packets

        senders = alive if dup else (["Ethernet"] if "Ethernet" in alive else alive[:1])
        return {
            "server": self.config.server or "demo:51888",
            "mode": mode,
            "senders": senders,
            "reason": {
                "sano": "Ethernet sano",
                "pico": "Ethernet con pico: el juego va por los dos enlaces",
                "corte": "Ethernet caido: todo por Wi-Fi",
            }[phase],
            "up_packets": self._counters["up"],
            "up_duplicated": self._counters["dup"],
            "emergency_packets": self._counters["emergency"],
            "suspect": ["Ethernet"] if phase == "pico" and mode != Mode.DUPLICATE.value else [],
            "down_packets": self._counters["down"],
            "codec": {"sealed": 0, "accepted": 0, "duplicates": self._counters["discarded"],
                      "invalid": 0, "stale": 0, "peer_restarts": 0},
            "links": rows,
            "events": self._events[-20:],
            # At 6 s the "player" enters the world, through the tunnel.
            "game": {"checked": True, "outside": [],
                     "inside": ["100.25.151.1:7171", "172.65.219.118:7171"]
                     if now - self._started > 6 else []},
        }
