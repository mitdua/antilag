"""Configure the TUN and the system routes while the tunnel is up.

Only the world's IPs go through the tunnel, each one as a host route (/32):
they are more specific than the default route, so they win without touching
it, and closing the tunnel just removes them. Tibia's login and website keep
going out directly with the home IP (CipSoft rejects those services from a VPS).

Avoiding a loop depends on each link's sockets leaving through their own
interface even when the table says TUN. ``IP_UNICAST_IF`` / ``SO_BINDTODEVICE``
guarantee that (see ``binding.py``); just in case, a host route to the VPS via
each link's gateway is added as well.

Everything goes into the *active* (non-persistent) store: if the process dies,
a reboot leaves the system clean, and on Windows the Wintun adapter takes its
routes with it when it disappears.
"""

from __future__ import annotations

import ipaddress
import subprocess
import sys
import time

from ..proc import NO_WINDOW



class RouteError(Exception):
    pass


def _run(argv: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(argv, capture_output=True, text=True, creationflags=NO_WINDOW)
    if check and result.returncode != 0:
        detail = (result.stdout + result.stderr).strip()
        raise RouteError(f"{' '.join(argv)}: {detail}")
    return result


class RouteManager:
    """Remember what was added so it can be undone, in reverse order, on close."""

    def __init__(self, platform: str | None = None, runner=_run) -> None:
        self.platform = platform or sys.platform
        self._run = runner
        #: added routes -> command that deletes them; in insertion order
        self._added: dict[tuple, list[str]] = {}

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def configure_tun(self, tun_name: str, tun_index: int | None, address: str, mtu: int) -> None:
        iface = ipaddress.ip_interface(address)
        if self.windows:
            # A freshly created adapter can take a moment to exist for netsh. Try
            # by index and, in case this netsh version does not accept it in
            # name=, by name.
            for attempt in range(10):
                ident = tun_index if attempt % 2 == 0 else tun_name
                try:
                    self._run([
                        "netsh", "interface", "ipv4", "set", "address", f"name={ident}",
                        "source=static", f"address={iface.ip}", f"mask={iface.netmask}",
                        "store=active",
                    ])
                    break
                except RouteError:
                    if attempt == 9:
                        raise
                    time.sleep(0.5)
            self._run([
                "netsh", "interface", "ipv4", "set", "subinterface", str(tun_index),
                f"mtu={mtu}", "store=active",
            ])
        else:
            self._run(["ip", "addr", "replace", str(iface), "dev", tun_name])
            self._run(["ip", "link", "set", tun_name, "mtu", str(mtu), "up"])

    def add_route(self, prefix: str, iface_name: str, iface_index: int | None,
                  gateway: str | None = None, metric: int = 1) -> None:
        net = ipaddress.ip_network(prefix)
        if self.windows:
            add = ["netsh", "interface", "ipv4", "add", "route", str(net), str(iface_index)]
            if gateway:
                add.append(gateway)
            add += [f"metric={metric}", "store=active"]
            delete = ["netsh", "interface", "ipv4", "delete", "route", str(net), str(iface_index)]
            if gateway:
                delete.append(gateway)
            delete.append("store=active")
            # An identical route left over from a previous run makes the add fail.
            self._run(delete, check=False)
        else:
            add = ["ip", "route", "replace", str(net)]
            if gateway:
                add += ["via", gateway]
            add += ["dev", iface_name, "metric", str(metric)]
            delete = ["ip", "route", "del", str(net), "dev", iface_name]
        self._run(add)
        self._added[self._key(prefix, iface_name, iface_index, gateway)] = delete

    @staticmethod
    def _key(prefix, iface_name, iface_index, gateway) -> tuple:
        return (str(ipaddress.ip_network(prefix)), iface_index if iface_index else iface_name,
                gateway)

    def remove_route(self, prefix: str, iface_name: str, iface_index: int | None,
                     gateway: str | None = None) -> None:
        delete = self._added.pop(self._key(prefix, iface_name, iface_index, gateway), None)
        if delete is not None:
            self._run(delete, check=False)

    def server_route_for(self, server_ip: str, link, offset: int = 0) -> None:
        """Host route to the VPS through ``link``'s gateway.

        Different metrics per link: on Linux two routes to the same prefix with
        the same metric overwrite each other.
        """
        if link.gateway:
            self.add_route(f"{server_ip}/32", link.name, link.index, link.gateway,
                           metric=(link.metric or 50) + offset)

    def drop_server_route_for(self, server_ip: str, link) -> None:
        if link.gateway:
            self.remove_route(f"{server_ip}/32", link.name, link.index, link.gateway)

    def setup(self, tun_name: str, tun_index: int | None, address: str, mtu: int,
              routes: tuple[str, ...], server_ip: str, links: list) -> None:
        self.configure_tun(tun_name, tun_index, address, mtu)
        for offset, link in enumerate(links):
            self.server_route_for(server_ip, link, offset)
        for prefix in routes:
            self.add_route(prefix, tun_name, tun_index, None, metric=1)

    def close(self) -> None:
        while self._added:
            _, delete = self._added.popitem()  # last added first
            self._run(delete, check=False)
