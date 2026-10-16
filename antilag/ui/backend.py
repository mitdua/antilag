"""What the panel's wizard asks of the system, real or simulated.

The panel only talks to this interface, so demo mode walks the whole wizard
(capture, validation, tunnel) without Tibia, a VPS or administrator rights.
Every method blocks: the panel calls them with ``asyncio.to_thread``.
"""

from __future__ import annotations

import time

from ..net.connections import game_connections
from ..net.discovery import Link
from ..session import (
    LinkCheck,
    SessionError,
    TunnelConfig,
    TunnelSession,
    capture_game_ips,
    is_admin,
    probe_links,
    select_links,
)


class RealBackend:
    demo = False

    def is_admin(self) -> bool:
        return is_admin()

    def capture(self) -> list[str]:
        return capture_game_ips()

    def in_world(self) -> bool:
        """Whether the game is still connected to a world (outside the tunnel)."""
        try:
            return bool(game_connections())
        except Exception:  # noqa: BLE001 - when in doubt, do not block the user
            return False

    def links(self) -> list[Link]:
        return select_links([], require=False)

    def probe(self, config: TunnelConfig) -> list[LinkCheck]:
        server = config.server_addr()
        links = self.links()
        if not links:
            raise SessionError("no se detecto ninguna salida a internet")
        return probe_links(server, config.secret, links, client_id=config.client_id)

    def session(self, config: TunnelConfig):
        return TunnelSession(config)


class DemoBackend:
    """Simulated: Talera with two IPs, healthy Ethernet and Wi-Fi."""

    demo = True
    IPS = ["100.25.151.1", "172.65.219.118"]
    LINKS = [
        Link(name="Ethernet", address="10.0.0.55", gateway="10.0.0.1", kind="ethernet"),
        Link(name="Wi-Fi", address="10.47.197.115", gateway="10.47.197.217", kind="wifi"),
    ]

    def __init__(self) -> None:
        self._captured = False

    def is_admin(self) -> bool:
        return True

    def capture(self) -> list[str]:
        time.sleep(1.0)
        self._captured = True
        return list(self.IPS)

    def in_world(self) -> bool:
        return False

    def links(self) -> list[Link]:
        return list(self.LINKS)

    def probe(self, config: TunnelConfig) -> list[LinkCheck]:
        time.sleep(1.2)
        return [LinkCheck(self.LINKS[0], 101.4, "", "unicast_if"),
                LinkCheck(self.LINKS[1], 93.2, "", "unicast_if")]

    def session(self, config: TunnelConfig):
        from .demo import DemoSession

        return DemoSession(config)
