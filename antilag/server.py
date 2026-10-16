"""The VPS end: one session per player, dedup, NAT and replicated downlink.

Each player has their own secret, ``client_id`` and IP inside the tunnel (see
``registry.py``). The server picks the session by the header's id before
decrypting, and the downlink by the packet's destination IP: whatever happens
to one player (restart, dead link, new epoch) does not touch the others.

Each of a player's links arrives as a different UDP source address (that
link's public IP and port). The server does not need to know how many links
there are: it learns the paths from the frames that authenticate.

Uplink
    The first copy of each seq is written to the TUN; the VPS kernel routes it
    and sends it out through NAT with the VPS's fixed public IP. Only traffic
    to a game port and with that player's source IP gets through: the VPS
    cannot be used as a general VPN nor to impersonate someone else.
Downlink
    Each packet is sealed once and sent over every path of the player that
    carried DATA in the last ``active_s``: if the client duplicates, so does
    the downlink, with no extra signalling.
PING
    Answered with a PONG over the same path it came in on, with the same
    payload, so the client can measure each link separately.

Source address of the replies
    The socket listens on 0.0.0.0, and for UDP Linux picks a reply's source by
    the routing table, not by the address the request came to. On a VPS with
    more than one IP (a reserved or floating IP, which comes in through a
    private "anchor IP") the reply would leave from the native IP, and the
    client's NAT would drop it for coming from another address. So packets
    are received with ``IP_PKTINFO``, the local address the client wrote to
    is remembered per path, and replies leave from that same address.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import struct
import time
from dataclasses import dataclass

from .proto import (
    DIR_DOWN,
    KIND_DATA,
    KIND_PING,
    KIND_PONG,
    Codec,
    ProtocolError,
    derive_key,
    peek_client_id,
)
from .registry import ClientEntry
from .traffic import GAME_PORTS, ports

log = logging.getLogger(__name__)

Addr = tuple[str, int]

#: a path that carried DATA within this span receives the downlink
DEFAULT_ACTIVE_S = 2.0
#: a path with no frame at all within this span is forgotten
DEFAULT_PATH_TTL_S = 10.0
#: simultaneous paths per player; bounds memory against port-rotating NATs
MAX_PATHS = 16

_IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)
#: struct in_pktinfo { int ipi_ifindex; in_addr ipi_spec_dst; in_addr ipi_addr; }
_PKTINFO = struct.Struct("=I4s4s")


@dataclass
class Path:
    addr: Addr
    last_seen: float
    last_data: float = float("-inf")
    rx: int = 0
    tx: int = 0
    #: local address (4 bytes) the client wrote to; replies leave from there.
    #: None if the system did not report it.
    local: bytes | None = None


class ClientSession:
    """Everything about one player: their codec, paths and counters."""

    def __init__(self, entry: ClientEntry, game_ports: frozenset[int],
                 active_s: float, path_ttl_s: float) -> None:
        self.entry = entry
        self.codec = Codec(derive_key(entry.secret), DIR_DOWN, client_id=entry.id)
        self.address = ipaddress.IPv4Address(entry.address).packed
        self.game_ports = game_ports
        self.active_s = active_s
        self.path_ttl_s = path_ttl_s
        self.paths: dict[Addr, Path] = {}
        self._peer_epoch: bytes | None = None
        self.last_seen = float("-inf")
        self.up_packets = 0
        self.down_packets = 0
        self.spoofed = 0
        self.blocked = 0

    @property
    def name(self) -> str:
        return self.entry.name

    def same_config(self, entry: ClientEntry) -> bool:
        return (entry.secret, entry.address) == (self.entry.secret, self.entry.address)

    # -- uplink ----------------------------------------------------------- #

    def handle(self, datagram: bytes, addr: Addr, local: bytes | None, now: float,
               tun) -> list[tuple[bytes, Addr]]:
        frame = self.codec.open(datagram)  # ProtocolError: handled by the server
        if self.codec.peer_epoch != self._peer_epoch:
            # This player started again: their old paths no longer exist.
            if self._peer_epoch is not None:
                log.info("%s arranco de nuevo; se olvidan sus caminos", self.name)
            self._peer_epoch = self.codec.peer_epoch
            self.paths.clear()
        self.last_seen = now
        path = self._touch(addr, now)
        if local is not None:
            path.local = local
        # The header is already authenticated, so its kind also holds for a
        # duplicate copy: that path carries data even if it lost the race.
        if datagram[1] == KIND_DATA:
            path.last_data = now

        if frame is None:
            return []  # duplicate copy: the one that arrived first was already processed
        if frame.kind == KIND_DATA:
            if self._allowed(frame.payload):
                tun.write(frame.payload)
                self.up_packets += 1
            return []
        if frame.kind == KIND_PING:
            _, reply = self.codec.seal(frame.payload, kind=KIND_PONG)
            path.tx += 1
            return [(reply, addr)]
        return []

    def _touch(self, addr: Addr, now: float) -> Path:
        path = self.paths.get(addr)
        if path is None:
            if len(self.paths) >= MAX_PATHS:
                oldest = min(self.paths.values(), key=lambda p: p.last_seen)
                del self.paths[oldest.addr]
            path = self.paths[addr] = Path(addr=addr, last_seen=now)
        path.last_seen = now
        path.rx += 1
        return path

    def _allowed(self, packet: bytes) -> bool:
        """Only this player's game traffic goes out through the NAT.

        IPv4 with the player's source IP (otherwise they could impersonate
        another player) and to a game port (otherwise the VPS would work as a
        general VPN at the cost of its bandwidth).
        """
        if len(packet) < 20 or packet[0] >> 4 != 4 or packet[12:16] != self.address:
            self.spoofed += 1
            return False
        found = ports(packet)
        if found is None or found[1] not in self.game_ports:
            self.blocked += 1
            return False
        return True

    # -- downlink --------------------------------------------------------- #

    def targets(self, now: float) -> list[Addr]:
        for addr in [a for a, p in self.paths.items() if now - p.last_seen > self.path_ttl_s]:
            del self.paths[addr]
        active = [p.addr for p in self.paths.values() if now - p.last_data <= self.active_s]
        return active or list(self.paths)

    def seal_down(self, packet: bytes, now: float) -> list[tuple[bytes, Addr]]:
        targets = self.targets(now)
        if not targets:
            return []
        _, datagram = self.codec.seal(packet, kind=KIND_DATA)
        self.down_packets += 1
        for addr in targets:
            self.paths[addr].tx += 1
        return [(datagram, addr) for addr in targets]

    def snapshot(self, now: float) -> dict:
        return {
            "id": self.entry.id,
            "name": self.name,
            "address": self.entry.address,
            "idle_s": round(now - self.last_seen, 1) if self.last_seen > float("-inf") else None,
            "up_packets": self.up_packets,
            "down_packets": self.down_packets,
            "spoofed": self.spoofed,
            "blocked": self.blocked,
            "codec": self.codec.stats(),
            "paths": [
                {
                    "addr": f"{p.addr[0]}:{p.addr[1]}",
                    "idle_s": round(now - p.last_seen, 1),
                    "data": now - p.last_data <= self.active_s,
                    "rx": p.rx,
                    "tx": p.tx,
                }
                for p in self.paths.values()
            ],
        }


class TunnelServer:
    """Server logic, without sockets: a datagram comes in, sends come out.

    ``tun`` only needs ``write(packet)``. Replies are returned as a list of
    ``(datagram, address)`` for the network loop to send, which makes all of
    this testable without a network or privileges.
    """

    def __init__(
        self,
        clients: list[ClientEntry],
        tun,
        active_s: float = DEFAULT_ACTIVE_S,
        path_ttl_s: float = DEFAULT_PATH_TTL_S,
        game_ports: frozenset[int] = GAME_PORTS,
        clock=time.monotonic,
    ) -> None:
        self.tun = tun
        self.game_ports = game_ports
        self.active_s = active_s
        self.path_ttl_s = path_ttl_s
        self._clock = clock
        self.sessions: dict[int, ClientSession] = {}
        self._by_address: dict[bytes, ClientSession] = {}
        self.rejected = 0
        self.unknown = 0
        self.load(clients)

    def load(self, clients: list[ClientEntry]) -> dict[str, list[str]]:
        """Apply the player list without touching those that did not change.

        Returns which names were added, changed or removed, for the log. A
        removed player ceases to exist immediately: their frames become those
        of an unknown id.
        """
        wanted = {entry.id: entry for entry in clients}
        report: dict[str, list[str]] = {"agregados": [], "cambiados": [], "quitados": []}
        for cid in list(self.sessions):
            if cid not in wanted:
                report["quitados"].append(self.sessions.pop(cid).name)
        for cid, entry in wanted.items():
            current = self.sessions.get(cid)
            if current is not None and current.same_config(entry):
                current.entry = entry  # e.g. a name change
                continue
            report["cambiados" if current else "agregados"].append(entry.name)
            self.sessions[cid] = ClientSession(entry, self.game_ports, self.active_s,
                                               self.path_ttl_s)
        self._by_address = {s.address: s for s in self.sessions.values()}
        return report

    # -- uplink --------------------------------------------------------------

    def handle_datagram(
        self, datagram: bytes, addr: Addr, local: bytes | None = None
    ) -> list[tuple[bytes, Addr]]:
        try:
            session = self.sessions.get(peek_client_id(datagram))
        except ProtocolError as exc:
            self.rejected += 1
            log.debug("trama descartada de %s: %s", addr, exc)
            return []
        if session is None:
            self.unknown += 1  # player removed or that never existed
            return []
        try:
            return session.handle(datagram, addr, local, self._clock(), self.tun)
        except ProtocolError as exc:
            self.rejected += 1
            log.debug("trama de %s descartada (%s): %s", session.name, addr, exc)
            return []

    # -- downlink ------------------------------------------------------------

    def handle_tun_packet(self, packet: bytes) -> list[tuple[bytes, Addr]]:
        if len(packet) < 20 or packet[0] >> 4 != 4:
            return []
        session = self._by_address.get(packet[16:20])
        if session is None:
            return []
        return session.seal_down(packet, self._clock())

    def local_for(self, addr: Addr) -> bytes | None:
        """Which local address to answer ``addr`` from."""
        for session in self.sessions.values():
            path = session.paths.get(addr)
            if path is not None:
                return path.local
        return None

    def snapshot(self) -> dict:
        now = self._clock()
        return {
            "rejected": self.rejected,
            "unknown": self.unknown,
            "clients": [s.snapshot(now) for s in self.sessions.values()],
        }


class _UdpEndpoint:
    """UDP socket that answers from the same address it was written to."""

    def __init__(self, server: TunnelServer, host: str, port: int) -> None:
        self.server = server
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, _IP_PKTINFO, 1)
            self.pktinfo = True
        except OSError:
            self.pktinfo = False
        self.sock.bind((host, port))
        self.sock.setblocking(False)
        self._ancillary = socket.CMSG_SPACE(_PKTINFO.size)

    def on_readable(self) -> None:
        while True:
            try:
                data, ancdata, _flags, addr = self.sock.recvmsg(65535, self._ancillary)
            except BlockingIOError:
                return
            except OSError as exc:
                # ICMP "port unreachable" from a path that died; not fatal.
                log.debug("error de socket: %s", exc)
                continue
            local = None
            for level, kind, cdata in ancdata:
                if level == socket.IPPROTO_IP and kind == _IP_PKTINFO:
                    # ipi_addr: the header's destination address, that is, the one
                    # the client used to get here.
                    local = _PKTINFO.unpack_from(cdata)[2]
            for datagram, dest in self.server.handle_datagram(data, addr[:2], local):
                self.send(datagram, dest)

    def send(self, datagram: bytes, dest: Addr) -> None:
        local = self.server.local_for(dest)
        try:
            if local is not None and self.pktinfo:
                cmsg = (socket.IPPROTO_IP, _IP_PKTINFO, _PKTINFO.pack(0, local, bytes(4)))
                self.sock.sendmsg([datagram], [cmsg], 0, dest)
            else:
                self.sock.sendto(datagram, dest)
        except BlockingIOError:
            pass  # queue full: drop it, as a router would
        except OSError as exc:
            log.debug("envio fallido a %s: %s", dest, exc)

    def close(self) -> None:
        self.sock.close()


async def serve(
    server: TunnelServer,
    tun,
    host: str,
    port: int,
    stats_every: float = 0,
    ready: asyncio.Event | None = None,
    status_path: str | None = None,
    status_every: float = 5.0,
) -> None:
    """Network loop: UDP socket on one side, TUN on the other. Runs until cancelled.

    With ``status_path``, every ``status_every`` seconds it leaves each
    player's status there; ``antilag clients list`` reads it to show who is
    online.
    """
    loop = asyncio.get_running_loop()
    udp = _UdpEndpoint(server, host, port)

    def on_tun_readable() -> None:
        while True:
            try:
                packet = tun.read()
            except BlockingIOError:
                return
            for datagram, dest in server.handle_tun_packet(packet):
                udp.send(datagram, dest)

    fd = tun.fd
    loop.add_reader(udp.sock.fileno(), udp.on_readable)
    loop.add_reader(fd, on_tun_readable)
    log.info("escuchando en udp %s:%d, interfaz %s", host, port, getattr(tun, "name", "?"))
    if ready is not None:
        ready.set()
    try:
        last_stats = time.monotonic()
        while True:
            await asyncio.sleep(status_every if status_path else (stats_every or 3600))
            if status_path:
                write_status(status_path, server.snapshot())
            if stats_every and time.monotonic() - last_stats >= stats_every:
                last_stats = time.monotonic()
                log.info("estado: %s", server.snapshot())
    finally:
        loop.remove_reader(fd)
        loop.remove_reader(udp.sock.fileno())
        udp.close()


def write_status(path: str, snapshot: dict) -> None:
    """Atomic write: a reader never sees a half-written file."""
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"at": time.time(), **snapshot}, handle)
        os.replace(tmp, path)
    except OSError as exc:
        log.debug("no se pudo escribir el estado en %s: %s", path, exc)
