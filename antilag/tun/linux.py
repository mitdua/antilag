"""TUN interface on Linux: what the VPS server uses.

``/dev/net/tun`` is opened in TUN mode (IP packets, no Ethernet header) and
without ``IFF_PI``, so each ``read`` returns exactly one IP packet and each
``write`` injects one. Creating the interface requires ``CAP_NET_ADMIN``: root
on the VPS, or a user namespace in the tests.
"""

from __future__ import annotations

import fcntl
import os
import select
import struct
import subprocess

TUNSETIFF = 0x400454CA
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
#: ip(20) + udp(8) + header(22) + tag(16) over 1500 leaves 1434; rounded down
#: to leave room for PPPoE and other encapsulations along the path.
DEFAULT_MTU = 1400


class TunDevice:
    """Non-blocking descriptor of an already created TUN interface."""

    def __init__(self, name: str = "antilag0") -> None:
        fd = os.open("/dev/net/tun", os.O_RDWR | os.O_NONBLOCK)
        try:
            request = struct.pack("16sH", name.encode(), IFF_TUN | IFF_NO_PI)
            reply = fcntl.ioctl(fd, TUNSETIFF, request)
        except OSError:
            os.close(fd)
            raise
        self.fd = fd
        self.name = reply[:16].rstrip(b"\0").decode()

    def configure(self, address: str, mtu: int = DEFAULT_MTU) -> None:
        """Assign the address (with prefix, e.g. ``10.66.0.1/24``) and bring it up."""
        for argv in (
            ["ip", "addr", "replace", address, "dev", self.name],
            ["ip", "link", "set", self.name, "mtu", str(mtu), "up"],
        ):
            subprocess.run(argv, check=True, capture_output=True, text=True)

    def read(self) -> bytes:
        return os.read(self.fd, 65535)

    def recv(self, timeout: float) -> bytes | None:
        """One packet, or None if none arrives within ``timeout`` seconds."""
        if self.fd < 0:
            return None
        readable, _, _ = select.select([self.fd], [], [], timeout)
        if not readable:
            return None
        try:
            return os.read(self.fd, 65535)
        except BlockingIOError:
            return None

    def write(self, packet: bytes) -> None:
        os.write(self.fd, packet)

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1
