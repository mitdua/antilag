"""Which packets belong to the game.

Only traffic to the world's IPs enters the tunnel, and the VPS only forwards
packets bound for the game ports. On the client this decides what gets
duplicated across links: game traffic is a few kbps, and it is the only place
where a latency spike gets a character killed.
"""

from __future__ import annotations

import struct

#: Classic Tibia ports: 7171 login, 7172 game.
GAME_PORTS = frozenset({7171, 7172})

_TCP = 6
_UDP = 17


def ports(packet: bytes) -> tuple[int, int] | None:
    """(source, destination) ports of an IPv4 TCP/UDP packet, or None if not applicable."""
    if len(packet) < 20 or packet[0] >> 4 != 4:
        return None
    if packet[9] not in (_TCP, _UDP):
        return None
    # A fragment other than the first one carries no transport header.
    if struct.unpack_from("!H", packet, 6)[0] & 0x1FFF:
        return None
    ihl = (packet[0] & 0x0F) * 4
    if len(packet) < ihl + 4:
        return None
    return struct.unpack_from("!HH", packet, ihl)


def is_game(packet: bytes, game_ports: frozenset[int] = GAME_PORTS) -> bool:
    """True if the packet goes to, or comes back from, a game port."""
    found = ports(packet)
    if found is None:
        return False
    src, dst = found
    return src in game_ports or dst in game_ports
