"""Find the game server's real IP from the active connections.

CipSoft does not publish its worlds' addresses, and DNS does not help:
``tibia.com`` points to AWS and ``www.tibia.com`` to Cloudflare, that is, to
the website. Measuring against those names measures a CDN, not the server your
character lives on.

The reliable source is the machine itself with the game open: look at the
client process's established TCP connections, which give the IP to measure
against and the region where the VPS should go.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass

from ..proc import NO_WINDOW

#: Fragments of the Tibia client's process name, lowercase.
GAME_PROCESS_HINTS = ("client", "tibia")

#: Classic Tibia ports: 7171 login, 7172 game.
GAME_PORTS = (7171, 7172)


def is_public_address(address: str) -> bool:
    """True if the address is routable on the internet.

    Delegated to ``ipaddress`` instead of a text pattern, so both families are
    covered. A filter written only for IPv4 lets the IPv6 loopback (``::1``)
    through, which is exactly what local services use.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        parsed.is_loopback
        or parsed.is_private
        or parsed.is_link_local
        or parsed.is_multicast
        or parsed.is_unspecified
        or parsed.is_reserved
    )


_COMMAND_TIMEOUT = 20


@dataclass(frozen=True)
class Connection:
    process: str
    pid: int | None
    remote_address: str
    remote_port: int
    local_address: str = ""

    @property
    def is_public(self) -> bool:
        return is_public_address(self.remote_address)

    @property
    def looks_like_game(self) -> bool:
        """Heuristic: a known Tibia port, or a process that matches."""
        if not self.is_public:
            return False
        if self.remote_port in GAME_PORTS:
            return True
        name = self.process.lower()
        return any(hint in name for hint in GAME_PROCESS_HINTS)

    @property
    def target(self) -> str:
        return f"{self.remote_address}:{self.remote_port}"


class ConnectionError_(Exception):
    """The system's connections could not be queried."""


def _run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=_COMMAND_TIMEOUT, check=False, creationflags=NO_WINDOW,
        )
    except FileNotFoundError as exc:
        raise ConnectionError_(f"no se encontro el comando: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ConnectionError_(f"{command[0]} no respondio en tiempo") from exc
    if result.returncode != 0 and not result.stdout.strip():
        raise ConnectionError_(
            f"{command[0]} fallo: {result.stderr.strip()[:200]}"
        )
    return result.stdout


_WINDOWS_PROBE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @()
foreach ($c in Get-NetTCPConnection -State Established) {
    $p = Get-Process -Id $c.OwningProcess -ErrorAction SilentlyContinue
    $out += [pscustomobject]@{
        Process    = if ($p) { $p.ProcessName } else { '' }
        Pid        = [int]$c.OwningProcess
        Local      = [string]$c.LocalAddress
        Remote     = [string]$c.RemoteAddress
        RemotePort = [int]$c.RemotePort
    }
}
$out | ConvertTo-Json -Depth 2 -Compress
"""


def _powershell() -> str:
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return rf"{root}\System32\WindowsPowerShell\v1.0\powershell.exe"


def parse_windows(raw: str) -> list[Connection]:
    try:
        payload = json.loads(raw or "[]")
    except json.JSONDecodeError as exc:
        raise ConnectionError_("PowerShell devolvio un JSON ilegible") from exc
    if isinstance(payload, dict):
        payload = [payload]
    return [
        Connection(
            process=entry.get("Process") or "",
            pid=entry.get("Pid"),
            remote_address=entry.get("Remote") or "",
            remote_port=int(entry.get("RemotePort") or 0),
            local_address=entry.get("Local") or "",
        )
        for entry in payload
        if entry.get("Remote")
    ]


# 'ss -tnp' format: state, queues, local, remote and then the quoted process,
# e.g.  users:(("client",pid=1234,fd=9))
_SS_LINE = re.compile(
    r"^\S+\s+\d+\s+\d+\s+(?P<local>\S+)\s+(?P<remote>\S+)(?:\s+(?P<proc>.*))?$"
)
_SS_PROC = re.compile(r'\("(?P<name>[^"]+)",pid=(?P<pid>\d+)')


def parse_ss(raw: str) -> list[Connection]:
    connections: list[Connection] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith(("State", "Netid")):
            continue
        match = _SS_LINE.match(line)
        if not match:
            continue
        remote = match.group("remote")
        host, _, port = remote.rpartition(":")
        if not port.isdigit():
            continue
        proc = _SS_PROC.search(match.group("proc") or "")
        connections.append(
            Connection(
                process=proc.group("name") if proc else "",
                pid=int(proc.group("pid")) if proc else None,
                remote_address=host.strip("[]"),
                remote_port=int(port),
                local_address=match.group("local"),
            )
        )
    return connections


# 'lsof -nP -iTCP -sTCP:ESTABLISHED' -> NAME holds  local->remote
_LSOF_NAME = re.compile(r"(?P<local>\S+)->(?P<remote>\S+)")


def parse_lsof(raw: str) -> list[Connection]:
    connections: list[Connection] = []
    for line in raw.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 9:
            continue
        match = _LSOF_NAME.search(fields[-1] if "->" in fields[-1] else line)
        if not match:
            continue
        host, _, port = match.group("remote").rpartition(":")
        if not port.isdigit():
            continue
        connections.append(
            Connection(
                process=fields[0],
                pid=int(fields[1]) if fields[1].isdigit() else None,
                remote_address=host.strip("[]"),
                remote_port=int(port),
                local_address=match.group("local"),
            )
        )
    return connections


def established(platform: str | None = None) -> list[Connection]:
    """The system's established TCP connections, with their process."""
    platform = platform or sys.platform
    if platform in ("win32", "cygwin"):
        return parse_windows(
            _run([_powershell(), "-NoProfile", "-NonInteractive", "-Command",
                  _WINDOWS_PROBE])
        )
    if platform.startswith("linux"):
        return parse_ss(_run(["ss", "-tnp", "state", "established"]))
    if platform == "darwin":
        return parse_lsof(_run(["lsof", "-nP", "-iTCP", "-sTCP:ESTABLISHED"]))
    raise ConnectionError_(f"plataforma no soportada: {platform}")


# Only the game ports and without looking up the process name: the panel runs
# this query every few seconds, so it has to be light.
_WINDOWS_GAME_PROBE = r"""
$ErrorActionPreference = 'SilentlyContinue'
$out = @(Get-NetTCPConnection -State Established -RemotePort %s | ForEach-Object {
    [pscustomobject]@{
        Process    = ''
        Pid        = [int]$_.OwningProcess
        Local      = [string]$_.LocalAddress
        Remote     = [string]$_.RemoteAddress
        RemotePort = [int]$_.RemotePort
    }
})
ConvertTo-Json -InputObject $out -Depth 2 -Compress
"""


def host_of(address: str) -> str:
    """'10.66.0.2:51234' or '[::1]:80' -> the address without the port."""
    if address.startswith("["):
        return address[1:].split("]", 1)[0]
    if address.count(":") == 1:
        return address.split(":", 1)[0]
    return address


def game_connections(platform: str | None = None) -> list[Connection]:
    """Established connections to a game port on a public IP.

    Unlike :func:`find_game_servers`, it ignores the process name: the Tibia
    client also opens HTTPS connections to www.tibia.com, and those must stay
    out of the tunnel (CipSoft rejects logins from datacenter IPs).
    """
    platform = platform or sys.platform
    if platform == "win32":
        ports = ",".join(str(p) for p in sorted(GAME_PORTS))
        raw = _run([_powershell(), "-NoProfile", "-NonInteractive", "-Command",
                    _WINDOWS_GAME_PROBE % ports])
        conns = parse_windows(raw)
    else:
        conns = established(platform)
    return [c for c in conns if c.remote_port in GAME_PORTS and c.is_public]


def find_game_servers(platform: str | None = None) -> list[Connection]:
    """Connections that look like the game server, without duplicates."""
    seen: set[str] = set()
    found: list[Connection] = []
    for conn in established(platform):
        if not conn.looks_like_game or conn.target in seen:
            continue
        seen.add(conn.target)
        found.append(conn)
    # Known Tibia ports first: they are the strongest signal.
    found.sort(key=lambda c: (c.remote_port not in GAME_PORTS, c.remote_port))
    return found
