"""Per-link health measurement: RTT, jitter and loss.

Two probes, for different reasons:

:class:`TcpConnectProbe`
    The TCP handshake time. Unlike ICMP it needs no privileges, and it follows
    the real path to a specific port. It is the probe for the measurement phase,
    before the tunnel exists, and the one used to compare candidate VPS
    regions. A *connection refused* also measures: the round trip happened
    anyway.

:class:`UdpEchoProbe`
    Tunnel-level PING/PONG against the antilag server. It measures exactly the
    path the data will take, with the same protocol and port, so it does not
    suffer the deprioritization networks often apply to ICMP traffic.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from .binding import is_strong, make_tcp_socket, make_udp_socket
from .discovery import Link

#: weight of the new sample in the RTT exponential moving average
RTT_ALPHA = 0.2
#: jitter smoothing divisor, as in RFC 3550
JITTER_DIVISOR = 16.0
#: samples that go into the loss calculation
DEFAULT_HISTORY = 60
DEFAULT_INTERVAL = 1.0
DEFAULT_TIMEOUT = 2.0
#: consecutive failures before a link is declared down
DEAD_AFTER_LOSSES = 4


@dataclass
class LinkHealth:
    """Accumulated state of a link."""

    link: Link
    #: binding strategy achieved; if it is the weak one, the measurement is suspect
    strategy: str = "source_only"
    rtt_ms: float | None = None
    last_rtt_ms: float | None = None
    jitter_ms: float = 0.0
    sent: int = 0
    received: int = 0
    consecutive_losses: int = 0
    last_error: str = ""
    history: deque[float | None] = field(
        default_factory=lambda: deque(maxlen=DEFAULT_HISTORY), repr=False
    )

    def record(self, rtt_ms: float | None, error: str = "") -> None:
        self.sent += 1
        self.history.append(rtt_ms)
        if rtt_ms is None:
            self.consecutive_losses += 1
            self.last_error = error
            return

        self.received += 1
        self.consecutive_losses = 0
        self.last_error = ""
        if self.last_rtt_ms is not None:
            delta = abs(rtt_ms - self.last_rtt_ms)
            self.jitter_ms += (delta - self.jitter_ms) / JITTER_DIVISOR
        self.last_rtt_ms = rtt_ms
        if self.rtt_ms is None:
            self.rtt_ms = rtt_ms
        else:
            self.rtt_ms = RTT_ALPHA * rtt_ms + (1.0 - RTT_ALPHA) * self.rtt_ms

    @property
    def loss_pct(self) -> float:
        if not self.history:
            return 0.0
        lost = sum(1 for sample in self.history if sample is None)
        return 100.0 * lost / len(self.history)

    @property
    def alive(self) -> bool:
        if self.consecutive_losses >= DEAD_AFTER_LOSSES:
            return False
        return self.received > 0

    @property
    def trustworthy(self) -> bool:
        """False if the socket was not firmly bound to the interface."""
        return is_strong(self.strategy)

    def percentile(self, pct: float) -> float | None:
        """RTT percentile over the history; reveals the spikes."""
        samples = sorted(s for s in self.history if s is not None)
        if not samples:
            return None
        index = min(len(samples) - 1, int(round((pct / 100.0) * (len(samples) - 1))))
        return samples[index]

    def snapshot(self) -> dict:
        return {
            "name": self.link.name,
            "kind": self.link.kind,
            "address": self.link.address,
            "strategy": self.strategy,
            "trustworthy": self.trustworthy,
            "rtt_ms": round(self.rtt_ms, 1) if self.rtt_ms is not None else None,
            "last_rtt_ms": (
                round(self.last_rtt_ms, 1) if self.last_rtt_ms is not None else None
            ),
            "p95_ms": (
                round(p, 1) if (p := self.percentile(95)) is not None else None
            ),
            "jitter_ms": round(self.jitter_ms, 1),
            "loss_pct": round(self.loss_pct, 1),
            "sent": self.sent,
            "received": self.received,
            "alive": self.alive,
            "last_error": self.last_error,
        }


class Probe(Protocol):
    """Measures one round trip over a specific link."""

    async def measure(self, link: Link) -> tuple[float | None, str]:
        """Return (rtt in ms, error). ``None`` as the RTT means a loss."""
        ...

    @property
    def target(self) -> str:
        ...


class TcpConnectProbe:
    """RTT from the TCP handshake time. Needs no privileges."""

    def __init__(self, host: str, port: int, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self._resolved: str | None = None

    @property
    def target(self) -> str:
        return f"tcp://{self.host}:{self.port}"

    async def _address(self) -> str:
        if self._resolved is None:
            loop = asyncio.get_running_loop()
            infos = await loop.getaddrinfo(
                self.host, self.port, family=socket.AF_INET, type=socket.SOCK_STREAM
            )
            if not infos:
                raise OSError(f"no se pudo resolver {self.host}")
            self._resolved = infos[0][4][0]
        return self._resolved

    async def measure(self, link: Link) -> tuple[float | None, str]:
        loop = asyncio.get_running_loop()
        try:
            address = await self._address()
        except OSError as exc:
            return None, f"dns: {exc}"

        sock, _strategy = make_tcp_socket(link)
        start = time.perf_counter()
        try:
            await asyncio.wait_for(
                loop.sock_connect(sock, (address, self.port)), self.timeout
            )
        except ConnectionRefusedError:
            # The destination answered with RST: the full round trip happened anyway.
            return (time.perf_counter() - start) * 1000.0, ""
        except asyncio.TimeoutError:
            return None, "timeout"
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            sock.close()
        return (time.perf_counter() - start) * 1000.0, ""


class UdpEchoProbe:
    """Tunnel-level PING/PONG against the antilag server."""

    def __init__(
        self,
        host: str,
        port: int,
        codec,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        # A single codec, the tunnel's own: a new one per probe would mean a new
        # epoch per ping, and the server would retire the data session's epoch.
        self.codec = codec
        self.timeout = timeout

    @property
    def target(self) -> str:
        return f"udp://{self.host}:{self.port}"

    async def measure(self, link: Link) -> tuple[float | None, str]:
        from ..proto import KIND_PING, KIND_PONG, ProtocolError

        loop = asyncio.get_running_loop()
        sock, _strategy = make_udp_socket(link)
        codec = self.codec
        start = time.perf_counter()
        try:
            # The PONG echoes the PING's payload, which pairs it with its probe.
            token = struct.pack("!Q", codec.next_seq)
            _, datagram = codec.seal(token, kind=KIND_PING)
            await loop.sock_sendto(sock, datagram, (self.host, self.port))
            deadline = start + self.timeout
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise asyncio.TimeoutError
                reply = await asyncio.wait_for(loop.sock_recv(sock, 2048), remaining)
                try:
                    frame = codec.open(reply)
                except ProtocolError as exc:
                    return None, f"trama invalida: {exc}"
                if frame is not None and frame.kind == KIND_PONG and frame.payload == token:
                    return (time.perf_counter() - start) * 1000.0, ""
        except asyncio.TimeoutError:
            return None, "timeout"
        except OSError as exc:
            return None, f"{type(exc).__name__}: {exc}"
        finally:
            sock.close()


class LinkMonitor:
    """Probes every link in parallel and keeps their health."""

    def __init__(
        self,
        links: list[Link],
        probe: Probe,
        interval: float = DEFAULT_INTERVAL,
        history: int = DEFAULT_HISTORY,
    ) -> None:
        self.probe = probe
        self.interval = interval
        self.health: dict[str, LinkHealth] = {}
        for link in links:
            # A test socket is bound only to learn which strategy is available;
            # the panel warns if any link ended up held by the weak one.
            try:
                sock, strategy = make_udp_socket(link)
                sock.close()
            except OSError as exc:
                strategy = "source_only"
                del exc
            health = LinkHealth(link=link, strategy=strategy)
            health.history = deque(maxlen=history)
            self.health[link.id] = health
        self._stop = asyncio.Event()

    async def _probe_once(self, health: LinkHealth) -> None:
        rtt_ms, error = await self.probe.measure(health.link)
        health.record(rtt_ms, error)

    async def tick(self) -> None:
        """Take one sample from every link, all at once."""
        await asyncio.gather(
            *(self._probe_once(health) for health in self.health.values()),
            return_exceptions=True,
        )

    async def run(self, rounds: int | None = None, on_tick=None) -> None:
        """Probe up to ``rounds`` times, or forever if it is ``None``."""
        completed = 0
        while not self._stop.is_set():
            started = time.perf_counter()
            await self.tick()
            if on_tick is not None:
                on_tick(self.snapshot())
            completed += 1
            if rounds is not None and completed >= rounds:
                return
            remaining = self.interval - (time.perf_counter() - started)
            if remaining > 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), remaining)
                except asyncio.TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()

    def ranked(self) -> list[LinkHealth]:
        """Live links from best to worst RTT; dead ones go last.

        This is the basis of the dynamic fallback: second place is earned by
        measurement, not by a fixed order in the configuration.
        """
        def sort_key(health: LinkHealth):
            return (
                not health.alive,
                health.rtt_ms if health.rtt_ms is not None else float("inf"),
            )

        return sorted(self.health.values(), key=sort_key)

    def snapshot(self) -> dict:
        return {
            "target": self.probe.target,
            "links": [health.snapshot() for health in self.ranked()],
        }
