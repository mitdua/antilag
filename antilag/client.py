"""The PC end: the TUN on one side, one UDP socket per link on the other.

Threads, not asyncio: Wintun delivers packets through a Windows event that does
not fit the asyncio loop, and with threads the same code runs unchanged on the
Linux TUN, which is how it is tested end to end.

uplink (1 thread)
    Reads a packet from the TUN, seals it once and sends it over the links
    that apply. Game traffic goes over every link the policy has as a sender;
    anything else (only the world's IPs are routed into the tunnel, so it
    should not happen) goes over the primary only, so it never costs mobile
    data.
downlink (1 thread per link)
    Opens every datagram; the first copy of each seq goes to the TUN, the
    rest are dropped. PONGs feed their link's health.
control (1 thread)
    Every ``ping_interval`` sends a PING over each link, counts expired ones
    as lost and asks :class:`PolicyEngine` for a decision. PINGs travel over
    the same sockets as the data: they measure the real path and keep that
    path alive in the server's table.

Fast suspicion
    The policy is deliberately slow to *switch* (sustain, minimum dwell):
    changing primaries for no reason is expensive. But *duplicating the game*
    is cheap, and waiting 2-3s to do it is exactly the lag that kills. So, on
    top of the policy, a sending link becomes "suspect" as soon as its oldest
    unanswered PING exceeds ``2*rtt + 40ms`` (80ms minimum): that catches a
    spike as well as an outage, without waiting for the reply to arrive late
    or not at all. While there is suspicion, and for ``suspect_hold_s``
    afterwards, game traffic also goes out over every live link. Entering is
    immediate and leaving is slow: it does not oscillate.
"""

from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from collections import deque

from .net.binding import make_udp_socket
from .net.probe import DEFAULT_HISTORY, LinkHealth
from .policy import Decision, PolicyEngine
from .proto import FLAG_DUP, KIND_DATA, KIND_PING, KIND_PONG, Codec, ProtocolError
from .traffic import GAME_PORTS, is_game

log = logging.getLogger(__name__)

#: five pings per second per link: about 3 kbps per link
DEFAULT_PING_INTERVAL = 0.2
DEFAULT_PING_TIMEOUT = 1.0
#: a ping pending for longer than max(SUSPECT_FLOOR_MS, 2*rtt + SUSPECT_MARGIN_MS)
SUSPECT_FLOOR_MS = 80.0
SUSPECT_MARGIN_MS = 40.0
#: how long emergency duplication lasts after the last suspicion
DEFAULT_SUSPECT_HOLD_S = 3.0
#: how long reads block before checking whether to stop
_POLL_S = 0.25
#: how often links are checked for late pings
_SUSPECT_CHECK_S = 0.05


class PathState:
    """A link: its bound socket, its health and its counters."""

    def __init__(self, link, sock: socket.socket, strategy: str, history: int) -> None:
        self.link = link
        self.sock = sock
        self.strategy = strategy
        self.health = LinkHealth(link=link, strategy=strategy)
        self.health.history = deque(maxlen=history)
        self.pending: dict[bytes, float] = {}
        self.tx = 0
        #: copies that arrived first over this link: who wins the race
        self.first = 0
        self.late = 0
        self.send_errors = 0
        #: until when emergency duplication lasts for this link
        self.suspect_until = float("-inf")
        #: replaced by another socket (IP change) or the client was closed
        self.closed = False

    @property
    def name(self) -> str:
        return self.link.id

    def stall_ms(self, now: float) -> float:
        """Age of the oldest unanswered PING; 0 if none is pending."""
        if not self.pending:
            return 0.0
        return (now - min(self.pending.values())) * 1000.0

    def suspect_threshold_ms(self) -> float:
        rtt = self.health.rtt_ms or 0.0
        return max(SUSPECT_FLOOR_MS, 2.0 * rtt + SUSPECT_MARGIN_MS)


class TunnelClient:
    def __init__(
        self,
        codec: Codec,
        tun,
        links: list,
        server: tuple[str, int],
        engine: PolicyEngine,
        game_ports: frozenset[int] = GAME_PORTS,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        ping_timeout: float = DEFAULT_PING_TIMEOUT,
        history: int = DEFAULT_HISTORY,
        suspect_hold_s: float = DEFAULT_SUSPECT_HOLD_S,
        socket_factory=make_udp_socket,
        clock=time.monotonic,
        on_decision=None,
    ) -> None:
        if not links:
            raise ValueError("hace falta al menos un enlace")
        self.codec = codec
        self.tun = tun
        self.server = server
        self.engine = engine
        self.game_ports = game_ports
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.suspect_hold_s = suspect_hold_s
        self._clock = clock
        self._on_decision = on_decision

        self._socket_factory = socket_factory
        self._history = history
        #: Replaced as a whole, never modified in place: threads read a
        #: consistent snapshot without a lock and rediscovery publishes the new
        #: dictionary with a single assignment.
        self.paths: dict[str, PathState] = {}
        self._paths_lock = threading.Lock()
        self._started = False
        paths = {}
        for link in links:
            paths[link.id] = self._open_path(link)
        self.paths = paths

        #: the codec is not thread-safe: seal and open run under this lock
        self._codec_lock = threading.Lock()
        #: health and pending pings, shared between downlink and control
        self._health_lock = threading.Lock()
        self.decision: Decision | None = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._ping_counter = 0

        self.up_packets = 0
        self.up_duplicated = 0
        self.emergency_packets = 0
        self.down_packets = 0
        self.non_ipv4 = 0
        self.no_path = 0
        self.tun_errors = 0

    # -- lifecycle -------------------------------------------------------- #

    def _open_path(self, link) -> PathState:
        sock, strategy = self._socket_factory(link)
        try:
            sock.setblocking(True)
            sock.settimeout(_POLL_S)
            # Connected socket: the system only delivers replies coming from this
            # same IP:port, just like the home or hotspot NAT would. So a
            # server that answers from another address fails here too, not
            # only behind a NAT.
            sock.connect(self.server)
        except BaseException:
            sock.close()
            raise
        return PathState(link, sock, strategy, self._history)

    def _spawn(self, name: str, func, *args) -> None:
        thread = threading.Thread(target=func, args=args, name=name, daemon=True)
        thread.start()
        self._threads.append(thread)

    def start(self) -> None:
        self._started = True
        self._spawn("antilag-subida", self._uplink)
        self._spawn("antilag-control", self._control)
        for path in self.paths.values():
            self._spawn(f"antilag-bajada-{path.name}", self._downlink, path)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        for path in self.paths.values():
            path.closed = True
            path.sock.close()

    # -- hot-plugged links ------------------------------------------------ #

    def add_link(self, link) -> PathState:
        """Add a new link, or replace the socket of one that changed.

        On replacement the health history and counters are kept: to the
        policy it is the same link with another IP, not a new one. The old
        socket is closed and its downlink thread ends on its own.
        """
        path = self._open_path(link)
        with self._paths_lock:
            old = self.paths.get(link.id)
            if old is not None:
                with self._health_lock:
                    path.health = old.health
                    path.health.link = link
                    path.tx, path.first, path.late = old.tx, old.first, old.late
                    path.send_errors = old.send_errors
            paths = dict(self.paths)
            paths[link.id] = path
            self.paths = paths
        if old is not None:
            old.closed = True
            old.sock.close()
        if self._started:
            self._spawn(f"antilag-bajada-{path.name}", self._downlink, path)
        log.info("enlace %s: %s", "reemplazado" if old else "agregado", link.describe())
        return path

    # -- link selection --------------------------------------------------- #

    def senders_for(self, packet: bytes) -> list[PathState]:
        """Which links this packet goes out over."""
        current = self.paths  # a single snapshot: rediscovery may replace it
        paths = list(current.values())
        alive = [p for p in paths if p.health.alive]
        decision = self.decision
        if decision is None or not decision.senders:
            # Startup, or everything down according to the probes: better to try
            # all of them than to send nothing.
            return alive or paths
        chosen = [current[name] for name in decision.senders if name in current]
        if not any(p.health.alive for p in chosen) and alive:
            # The primary died between two evaluations and the policy has not
            # switched yet (sustain): do not wait to use what does work.
            chosen = alive
        game = is_game(packet, self.game_ports)
        if not game:
            return chosen[:1]
        if len(chosen) < len(alive):
            now = self._clock()
            if any(p.suspect_until > now for p in chosen):
                self.emergency_packets += 1
                chosen = chosen + [p for p in alive if p not in chosen]
        return chosen

    def _send(self, path: PathState, datagram: bytes) -> None:
        try:
            path.sock.send(datagram)
            path.tx += 1
        except OSError as exc:
            # Link down or without a route: the probes will reflect it.
            path.send_errors += 1
            log.debug("envio fallido por %s: %s", path.name, exc)

    # -- threads ---------------------------------------------------------- #

    def _uplink(self) -> None:
        while not self._stop.is_set():
            try:
                packet = self.tun.recv(_POLL_S)
            except EOFError:
                log.error("el TUN se cerro")
                self._stop.set()
                return
            except OSError as exc:
                self.tun_errors += 1
                log.debug("lectura del TUN fallida: %s", exc)
                continue
            if packet is None:
                continue
            if not packet or packet[0] >> 4 != 4:
                self.non_ipv4 += 1  # IPv6: the tunnel is IPv4 only
                continue
            targets = self.senders_for(packet)
            if not targets:
                self.no_path += 1
                continue
            flags = FLAG_DUP if len(targets) > 1 else 0
            with self._codec_lock:
                _, datagram = self.codec.seal(packet, KIND_DATA, flags)
            for path in targets:
                self._send(path, datagram)
            self.up_packets += 1
            if flags:
                self.up_duplicated += 1

    def _downlink(self, path: PathState) -> None:
        while not self._stop.is_set() and not path.closed:
            try:
                datagram = path.sock.recv(65535)
            except socket.timeout:
                continue
            except OSError:
                # Windows returns WSAECONNRESET for an ICMP port unreachable; a
                # dead link fails here too, and so does a replaced socket.
                if path.closed or self._stop.wait(0.05):
                    return
                continue
            with self._codec_lock:
                try:
                    frame = self.codec.open(datagram)
                except ProtocolError as exc:
                    log.debug("trama descartada en %s: %s", path.name, exc)
                    continue
            if frame is None:
                path.late += 1
                continue
            path.first += 1
            if frame.kind == KIND_DATA:
                try:
                    self.tun.write(frame.payload)
                    self.down_packets += 1
                except OSError as exc:
                    self.tun_errors += 1
                    log.debug("escritura al TUN fallida: %s", exc)
            elif frame.kind == KIND_PONG:
                self._on_pong(path, frame.payload)

    def _on_pong(self, path: PathState, token: bytes) -> None:
        now = self._clock()
        with self._health_lock:
            sent = path.pending.pop(token, None)
            if sent is not None:
                path.health.record((now - sent) * 1000.0)

    def _control(self) -> None:
        next_tick = self._clock()
        while not self._stop.wait(_SUSPECT_CHECK_S):
            self.check_suspects()
            if self._clock() >= next_tick:
                next_tick += self.ping_interval
                self.tick()

    def check_suspects(self) -> None:
        """Mark every link with a late ping as suspect.

        Runs more often than the ping rounds: detection should not wait for
        the next round.
        """
        now = self._clock()
        with self._health_lock:
            for path in self.paths.values():
                if path.stall_ms(now) > path.suspect_threshold_ms():
                    if path.suspect_until <= now:
                        log.info("sospecha en %s: ping sin respuesta hace %.0fms",
                                 path.name, path.stall_ms(now))
                    path.suspect_until = now + self.suspect_hold_s

    def tick(self) -> Decision:
        """One control round: expire pings, send new ones, decide."""
        self.check_suspects()
        now = self._clock()
        with self._health_lock:
            for path in self.paths.values():
                expired = [t for t, sent in path.pending.items() if now - sent > self.ping_timeout]
                for token in expired:
                    del path.pending[token]
                    path.health.record(None, "timeout")

        for path in self.paths.values():
            self._ping_counter += 1
            token = struct.pack("!Q", self._ping_counter)
            with self._codec_lock:
                _, datagram = self.codec.seal(token, KIND_PING)
            with self._health_lock:
                path.pending[token] = self._clock()
            self._send(path, datagram)

        with self._health_lock:
            healths = {name: path.health for name, path in self.paths.items()}
            decision = self.engine.evaluate(healths, now)
        self.decision = decision
        if decision.changed:
            log.info("politica: %s -> %s", decision.reason, ",".join(decision.senders) or "-")
            if self._on_decision is not None:
                self._on_decision(decision)
        return decision

    # -- status ----------------------------------------------------------- #

    def snapshot(self) -> dict:
        decision = self.decision
        with self._health_lock:
            links = []
            for path in self.paths.values():
                row = path.health.snapshot()
                row.update(tx=path.tx, first=path.first, late=path.late,
                           send_errors=path.send_errors)
                links.append(row)
        return {
            "server": f"{self.server[0]}:{self.server[1]}",
            "mode": decision.mode.value if decision else None,
            "senders": list(decision.senders) if decision else [],
            "reason": decision.reason if decision else "arrancando",
            "up_packets": self.up_packets,
            "up_duplicated": self.up_duplicated,
            "emergency_packets": self.emergency_packets,
            "suspect": [p.name for p in self.paths.values() if p.suspect_until > self._clock()],
            "down_packets": self.down_packets,
            "codec": self.codec.stats(),
            "links": links,
        }
