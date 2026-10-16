"""End-to-end server test with a real TUN and two players.

Runs inside ``unshare -rn``: its own network namespace where the user has
CAP_NET_ADMIN without being root. The players are Codecs over UDP sockets; the
"world" is a UDP echo on 10.66.0.1:7171 inside the same namespace, to which the
kernel delivers whatever the server writes to the TUN. There is also an echo
on a non-game port, which must receive nothing.

Prints OK at the end, or exits with an error.
"""

from __future__ import annotations

import asyncio
import os
import socket
import struct
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from antilag.proto import DIR_UP, KIND_DATA, KIND_PING, KIND_PONG, Codec, derive_key  # noqa: E402
from antilag.registry import ClientEntry  # noqa: E402
from antilag.server import TunnelServer, serve  # noqa: E402
from antilag.tun.linux import TunDevice  # noqa: E402

ANA = ClientEntry(id=1, name="ana", secret="secreto-ana", address="10.66.0.2")
BETO = ClientEntry(id=2, name="beto", secret="secreto-beto", address="10.66.0.3")
GAME = ("10.66.0.1", 7171)
OTHER = ("10.66.0.1", 9999)


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return ~total & 0xFFFF


def udp_packet(src: str, dst: tuple[str, int], body: bytes, sport: int = 40000) -> bytes:
    # UDP checksum 0: in IPv4 it means "no checksum" and the kernel accepts it.
    udp = struct.pack("!HHHH", sport, dst[1], 8 + len(body), 0) + body
    header = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, 0, 64, 17, 0,
                         socket.inet_aton(src), socket.inet_aton(dst[0]))
    header = header[:10] + struct.pack("!H", checksum(header)) + header[12:]
    return header + udp


def echo(addr: tuple[str, int], seen: list) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(addr)
    while True:
        data, peer = sock.recvfrom(2048)
        seen.append((addr[1], peer, data))
        sock.sendto(b"eco:" + data, peer)


class Player:
    def __init__(self, entry: ClientEntry) -> None:
        self.entry = entry
        self.codec = Codec(derive_key(entry.secret), DIR_UP, client_id=entry.id, max_skew_s=None)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.connect(("127.0.0.1", 51888))
        self.sock.settimeout(1.0)

    def restart(self) -> None:
        """Like closing and reopening the panel: new epoch, seq from 0."""
        self.codec = Codec(derive_key(self.entry.secret), DIR_UP, client_id=self.entry.id,
                           max_skew_s=None)

    def send(self, dst: tuple[str, int], body: bytes) -> None:
        self.sock.send(self.codec.seal(udp_packet(self.entry.address, dst, body))[1])

    def ping(self) -> bytes | None:
        self.sock.send(self.codec.seal(b"tok", kind=KIND_PING)[1])
        frame = self.recv()
        return frame.payload if frame is not None and frame.kind == KIND_PONG else None

    def recv(self, timeout: float = 1.0):
        self.sock.settimeout(timeout)
        try:
            return self.codec.open(self.sock.recv(4096))
        except socket.timeout:
            return None

    def reply_body(self, timeout: float = 1.0) -> bytes | None:
        frame = self.recv(timeout)
        if frame is None or frame.kind != KIND_DATA:
            return None
        return frame.payload[28:]  # without the IP (20) and UDP (8) headers


async def main() -> None:
    os.system("ip link set lo up")
    tun = TunDevice("e2e0")
    tun.configure("10.66.0.1/24")
    server = TunnelServer([ANA, BETO], tun)
    ready = asyncio.Event()
    task = asyncio.create_task(serve(server, tun, "127.0.0.1", 51888, ready=ready))
    await ready.wait()

    seen: list = []
    for addr in (GAME, OTHER):
        threading.Thread(target=echo, args=(addr, seen), daemon=True).start()

    def players() -> None:
        ana, beto = Player(ANA), Player(BETO)
        assert ana.ping() == b"tok" and beto.ping() == b"tok", "sin PONG"

        # Both play at once; each one gets only their own reply.
        ana.send(GAME, b"de ana")
        beto.send(GAME, b"de beto")
        assert ana.reply_body() == b"eco:de ana", "ana no recibio su eco"
        assert beto.reply_body() == b"eco:de beto", "beto no recibio su eco"
        assert ana.recv(0.3) is None and beto.recv(0.3) is None, "llego algo ajeno"

        # A non-game port does not leave the VPS.
        ana.send(OTHER, b"web")
        assert ana.reply_body(0.5) is None
        assert not [s for s in seen if s[0] == OTHER[1]], "paso trafico que no es del juego"
        assert server.sessions[1].blocked == 1

        # Beto cannot use ana's IP.
        beto.sock.send(beto.codec.seal(udp_packet(ANA.address, GAME, b"suplanta"))[1])
        assert beto.reply_body(0.5) is None and ana.recv(0.3) is None
        assert server.sessions[2].spoofed == 1

        # Beto restarts the panel: ana does not notice.
        beto.restart()
        beto.send(GAME, b"beto otra vez")
        assert beto.reply_body() == b"eco:beto otra vez"
        ana.send(GAME, b"ana sigue")
        assert ana.reply_body() == b"eco:ana sigue", "el reinicio de beto corto a ana"

        # Beto is removed: out immediately, ana carries on.
        server.load([ANA])
        beto.send(GAME, b"sigo?")
        assert beto.reply_body(0.5) is None
        ana.send(GAME, b"ana al final")
        assert ana.reply_body() == b"eco:ana al final"

    await asyncio.get_running_loop().run_in_executor(None, players)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    tun.close()
    print("OK")


if __name__ == "__main__":
    asyncio.run(main())
