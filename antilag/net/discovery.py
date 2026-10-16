"""Link discovery: active interfaces with their own way out to the internet.

A candidate link is a physical interface that is up, has an IPv4 address and
has its own default route. That last condition is what separates a real link
from a stray interface: without its own gateway it cannot send traffic out by
itself.

Virtual interfaces are excluded on purpose. Sending the tunnel's outer traffic
through another tunnel (or through our own TUN) creates a routing loop.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from ..proc import NO_WINDOW

#: Interfaces that are never candidates: tunnels, virtualization bridges and
#: our own TUN. Order does not matter; matched as a substring.
EXCLUDED_PATTERNS = (
    "antilag",
    "loopback",
    "wireguard",
    "surfshark",
    "openvpn",
    "fortinet",
    "wintun",
    "vethernet",
    "docker",
    "bluetooth",
    "tailscale",
    "zerotier",
    "teredo",
    "isatap",
    "pseudo-interface",
    "vmware",
    "virtualbox",
    "hyper-v",
)

KIND_ETHERNET = "ethernet"
KIND_WIFI = "wifi"
KIND_CELLULAR = "cellular"
KIND_UNKNOWN = "unknown"

_COMMAND_TIMEOUT = 15


@dataclass(frozen=True)
class Link:
    """A candidate path to the internet."""

    #: interface name/alias as the system reports it
    name: str
    #: local IPv4 address, used as the source when binding the link's socket
    address: str
    #: next hop of its default route
    gateway: str | None = None
    #: system interface index, needed to manage routes
    index: int | None = None
    #: current route metric; lower wins the system's tie-break
    metric: int | None = None
    kind: str = KIND_UNKNOWN
    #: state according to the operating system. Only a hint: Windows has been
    #: seen reporting 'Disconnected' on a Wi-Fi that already had an IP, a
    #: gateway and a 72 Mbps link. The real probe decides whether a link works.
    os_state: str = ""
    #: raw backend data, useful for diagnostics
    detail: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def id(self) -> str:
        """Stable identifier for configuration and the panel."""
        return self.name

    def describe(self) -> str:
        parts = [self.name, f"[{self.kind}]", self.address]
        if self.gateway:
            parts.append(f"via {self.gateway}")
        if self.metric is not None:
            parts.append(f"metric={self.metric}")
        if self.os_state and self.os_state.lower() != "up":
            parts.append(f"(el sistema lo reporta {self.os_state})")
        return " ".join(parts)


class DiscoveryError(Exception):
    """The system's network configuration could not be queried."""


def is_excluded(name: str, description: str = "") -> bool:
    """True if the interface is virtual and therefore useless as an outside link."""
    haystack = f"{name} {description}".lower()
    return any(pattern in haystack for pattern in EXCLUDED_PATTERNS)


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=_COMMAND_TIMEOUT,
            check=False,
            creationflags=NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise DiscoveryError(f"no se encontro el comando: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise DiscoveryError(f"{command[0]} no respondio en tiempo") from exc
    if result.returncode != 0:
        raise DiscoveryError(
            f"{' '.join(command)} fallo con codigo {result.returncode}: "
            f"{result.stderr.strip()[:200]}"
        )
    return result.stdout


# --------------------------------------------------------------------------- #
# Linux
# --------------------------------------------------------------------------- #

def _linux_kind(name: str) -> str:
    # The kernel exposes a 'wireless' subdirectory only on 802.11 interfaces.
    if os.path.isdir(f"/sys/class/net/{name}/wireless"):
        return KIND_WIFI
    if name.startswith(("wwan", "wwp", "ppp")):
        return KIND_CELLULAR
    if name.startswith(("en", "eth", "eno", "enp", "ens")):
        return KIND_ETHERNET
    return KIND_UNKNOWN


def discover_linux() -> list[Link]:
    routes = json.loads(_run(["ip", "-j", "route", "show", "default"]) or "[]")
    addrs = json.loads(_run(["ip", "-j", "addr", "show"]) or "[]")

    address_of: dict[str, str] = {}
    index_of: dict[str, int] = {}
    for entry in addrs:
        name = entry.get("ifname", "")
        if entry.get("operstate") not in ("UP", "UNKNOWN"):
            continue
        index_of[name] = entry.get("ifindex")
        for info in entry.get("addr_info", []):
            if info.get("family") == "inet" and not info.get("local", "").startswith(
                "169.254."
            ):
                address_of.setdefault(name, info["local"])

    links: list[Link] = []
    seen: set[str] = set()
    for route in routes:
        name = route.get("dev", "")
        if not name or name in seen or is_excluded(name):
            continue
        address = address_of.get(name)
        if not address:
            continue
        seen.add(name)
        links.append(
            Link(
                name=name,
                address=address,
                gateway=route.get("gateway"),
                index=index_of.get(name),
                metric=route.get("metric"),
                kind=_linux_kind(name),
                os_state="up",  # ip route show default only lists active ones
                detail=route,
            )
        )
    return links


# --------------------------------------------------------------------------- #
# Windows
# --------------------------------------------------------------------------- #

# Joins the three views needed (IP configuration, adapter and metric) by
# InterfaceIndex, and emits a single JSON to avoid paying for three PowerShell
# startups.
_WINDOWS_PROBE = r"""
$ErrorActionPreference = 'Stop'
$adapters = @{}
Get-NetAdapter | ForEach-Object { $adapters[[int]$_.InterfaceIndex] = $_ }
$metrics = @{}
Get-NetIPInterface -AddressFamily IPv4 | ForEach-Object {
    $metrics[[int]$_.InterfaceIndex] = $_
}
$out = @()
foreach ($cfg in Get-NetIPConfiguration) {
    $index = [int]$cfg.InterfaceIndex
    $adapter = $adapters[$index]
    $metric = $metrics[$index]
    $out += [pscustomobject]@{
        Name        = $cfg.InterfaceAlias
        Index       = $index
        Address     = ($cfg.IPv4Address | Select-Object -First 1).IPAddress
        Gateway     = ($cfg.IPv4DefaultGateway | Select-Object -First 1).NextHop
        Description = if ($adapter) { $adapter.InterfaceDescription } else { '' }
        Media       = if ($adapter) { [string]$adapter.PhysicalMediaType } else { '' }
        Status      = if ($adapter) { [string]$adapter.Status } else { '' }
        Metric      = if ($metric) { [int]$metric.InterfaceMetric } else { $null }
        State       = if ($metric) { [string]$metric.ConnectionState } else { '' }
    }
}
$out | ConvertTo-Json -Depth 3 -Compress
"""


def _windows_kind(media: str, description: str) -> str:
    blob = f"{media} {description}".lower()
    if "802.11" in blob or "wireless" in blob or "wi-fi" in blob:
        return KIND_WIFI
    if "wwan" in blob or "cellular" in blob or "mobile broadband" in blob:
        return KIND_CELLULAR
    if "802.3" in blob or "ethernet" in blob:
        return KIND_ETHERNET
    return KIND_UNKNOWN


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return rf"{root}\System32\WindowsPowerShell\v1.0\powershell.exe"


def discover_windows() -> list[Link]:
    raw = _run(
        [_powershell(), "-NoProfile", "-NonInteractive", "-Command", _WINDOWS_PROBE]
    )
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise DiscoveryError("PowerShell devolvio un JSON ilegible") from exc
    if isinstance(payload, dict):  # a single object does not come as a list
        payload = [payload]

    links: list[Link] = []
    for entry in payload:
        name = entry.get("Name") or ""
        address = entry.get("Address") or ""
        gateway = entry.get("Gateway")
        description = entry.get("Description") or ""
        # Without a gateway or with 169.254.x (autoconfiguration, what disconnected
        # adapters show) there was no DHCP: no way out is possible. But a
        # 'Disconnected' Status is not taken as a verdict.
        if not gateway or not address or address.startswith("169.254."):
            continue
        if is_excluded(name, description):
            continue
        links.append(
            Link(
                name=name,
                address=address,
                gateway=gateway,
                index=entry.get("Index"),
                metric=entry.get("Metric"),
                kind=_windows_kind(entry.get("Media") or "", description),
                os_state=entry.get("Status") or "",
                detail=entry,
            )
        )
    return links


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #

_DARWIN_SERVICE_ORDER = re.compile(r"\(Hardware Port: ([^,]+), Device: ([^)]+)\)")


def _darwin_kind(port: str, device: str) -> str:
    blob = f"{port} {device}".lower()
    if "wi-fi" in blob or "airport" in blob or device.startswith("en1"):
        return KIND_WIFI
    if "iphone" in blob or "usb modem" in blob or "cellular" in blob:
        return KIND_CELLULAR
    if "ethernet" in blob or "lan" in blob:
        return KIND_ETHERNET
    return KIND_UNKNOWN


def discover_darwin() -> list[Link]:
    ports: dict[str, str] = {}
    for port, device in _DARWIN_SERVICE_ORDER.findall(
        _run(["networksetup", "-listallhardwareports"])
    ):
        ports[device.strip()] = port.strip()

    links: list[Link] = []
    # scutil exposes the service order; from each service we take router and address.
    for device in ports:
        try:
            info = _run(["ipconfig", "getsummary", device])
        except DiscoveryError:
            continue
        address = re.search(r"yiaddr\s*=\s*([\d.]+)", info) or re.search(
            r"\bip_address\s*:\s*([\d.]+)", info
        )
        router = re.search(r"router\s*=\s*\{?\s*([\d.]+)", info)
        if not address or not router:
            continue
        local = address.group(1)
        if local.startswith("169.254."):
            continue
        port = ports[device]
        if is_excluded(device, port):
            continue
        links.append(
            Link(
                name=device,
                address=local,
                gateway=router.group(1),
                kind=_darwin_kind(port, device),
                detail={"hardware_port": port},
            )
        )
    return links


# --------------------------------------------------------------------------- #
# Facade
# --------------------------------------------------------------------------- #

def discover(platform: str | None = None) -> list[Link]:
    """List the current system's candidate links.

    The order implies no preference: :mod:`antilag.policy` decides, based on
    the RTT measured against the VPS.
    """
    platform = platform or sys.platform
    if platform.startswith("linux"):
        return discover_linux()
    if platform in ("win32", "cygwin"):
        return discover_windows()
    if platform == "darwin":
        return discover_darwin()
    raise DiscoveryError(f"plataforma no soportada: {platform}")
