"""Pin a socket's outgoing interface.

Binding the source IP is not enough on its own: the route is chosen by
destination, so the system may send the packet through the default interface.
For every copy to really leave through its own link this has to be requested
explicitly, and each system has its own socket option:

* Windows  ``IP_UNICAST_IF``   interface index, in network byte order (see below)
* Linux    ``SO_BINDTODEVICE`` by name; requires root or CAP_NET_RAW
* Linux    ``IP_UNICAST_IF``   unprivileged alternative
* macOS    ``IP_BOUND_IF``     interface index

If none succeeds, only the plain source bind remains, which usually works on
Windows thanks to the strong host model but is unreliable on Linux. That is why
:func:`bind_to_link` returns the strategy it managed to apply: the panel warns
when a link is held only by the weak one.
"""

from __future__ import annotations

import socket
import struct
import sys

from .discovery import Link

# Constants CPython does not expose on every platform.
_SO_BINDTODEVICE = 25
_IP_UNICAST_IF_LINUX = 50
_IP_UNICAST_IF_WINDOWS = 31
_IP_BOUND_IF_DARWIN = 25

#: The binding is firm: the system guarantees the outgoing interface.
STRATEGY_STRONG = ("unicast_if", "bindtodevice", "bound_if")
#: Only the source address was bound; routing could divert the traffic.
STRATEGY_WEAK = "source_only"


class BindError(Exception):
    """The socket could not be bound to the requested link."""


def _ifindex(link: Link) -> int | None:
    if link.index:
        return int(link.index)
    try:
        return socket.if_nametoindex(link.name)
    except (OSError, AttributeError):
        return None


def bind_to_link(
    sock: socket.socket,
    link: Link,
    platform: str | None = None,
) -> str:
    """Bind ``sock`` to the link and return the name of the applied strategy.

    The source address is always bound too, so the remote end sees a distinct
    endpoint per link and can keep its path table.
    """
    platform = platform or sys.platform
    index = _ifindex(link)
    strategy = STRATEGY_WEAK

    if platform in ("win32", "cygwin") and index:
        # Careful: for IPv4 this option expects the index in network byte order,
        # unlike IPv6. It is a documented asymmetry of the Windows API, and
        # sending it in host order fails silently, diverting the traffic.
        try:
            sock.setsockopt(
                socket.IPPROTO_IP,
                _IP_UNICAST_IF_WINDOWS,
                struct.pack("!I", index),
            )
            strategy = "unicast_if"
        except OSError:
            pass

    elif platform.startswith("linux"):
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                _SO_BINDTODEVICE,
                link.name.encode("utf-8") + b"\x00",
            )
            strategy = "bindtodevice"
        except (OSError, PermissionError):
            # Unprivileged; IP_UNICAST_IF does not need privileges.
            if index:
                try:
                    sock.setsockopt(
                        socket.IPPROTO_IP,
                        _IP_UNICAST_IF_LINUX,
                        struct.pack("I", index),
                    )
                    strategy = "unicast_if"
                except OSError:
                    pass

    elif platform == "darwin" and index:
        try:
            sock.setsockopt(
                socket.IPPROTO_IP, _IP_BOUND_IF_DARWIN, struct.pack("I", index)
            )
            strategy = "bound_if"
        except OSError:
            pass

    try:
        sock.bind((link.address, 0))
    except OSError as exc:
        raise BindError(
            f"no se pudo bindear a {link.address} del enlace {link.name}: {exc}"
        ) from exc

    return strategy


def is_strong(strategy: str) -> bool:
    """True if the strategy guarantees the outgoing interface."""
    return strategy in STRATEGY_STRONG


def make_udp_socket(link: Link, platform: str | None = None) -> tuple[socket.socket, str]:
    """Create a non-blocking UDP socket bound to ``link``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)
        strategy = bind_to_link(sock, link, platform)
    except BaseException:
        sock.close()
        raise
    return sock, strategy


def make_tcp_socket(link: Link, platform: str | None = None) -> tuple[socket.socket, str]:
    """Create a non-blocking TCP socket bound to ``link``.

    Used by the unprivileged probe: the TCP handshake time is an RTT measure
    that follows the real path and, unlike ICMP, needs no root.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        strategy = bind_to_link(sock, link, platform)
    except BaseException:
        sock.close()
        raise
    return sock, strategy
