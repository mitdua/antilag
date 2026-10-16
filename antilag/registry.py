"""VPS players and their access codes.

Stored in ``/etc/antilag/clients.json`` (0600 permissions):

    {
      "endpoint": "203.0.113.10:51888",
      "network": "10.66.0.0/24",
      "clients": [
        {"id": 1, "name": "principal", "secret": "...", "address": "10.66.0.2"}
      ]
    }

``endpoint`` is what players use to reach the VPS. The VPS cannot guess it (a
reserved IP does not show up on its interfaces), so it is set once and goes
inside every access code.

An access code bundles everything a panel needs to connect as that player, in
a single line to copy and paste. It includes the secret, so treat it like a
password.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
from dataclasses import asdict, dataclass, field

from .proto import MAX_CLIENT_ID, generate_secret

DEFAULT_PATH = "/etc/antilag/clients.json"
DEFAULT_NETWORK = "10.66.0.0/24"
ACCESS_PREFIX = "antilag1:"


class RegistryError(Exception):
    """Invalid player configuration; the message is shown to the user as is."""


@dataclass(frozen=True)
class ClientEntry:
    id: int
    name: str
    secret: str
    #: player's IP inside the tunnel, without prefix
    address: str


@dataclass
class Registry:
    endpoint: str = ""
    network: str = DEFAULT_NETWORK
    clients: list[ClientEntry] = field(default_factory=list)

    # -- queries ---------------------------------------------------------- #

    @property
    def net(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.network)

    @property
    def server_address(self) -> str:
        """The first IP of the network belongs to the VPS inside the tunnel."""
        return str(next(self.net.hosts()))

    def find(self, name: str) -> ClientEntry | None:
        lower = name.lower()
        return next((c for c in self.clients if c.name.lower() == lower), None)

    def validate(self) -> None:
        try:
            net = self.net
        except ValueError as exc:
            raise RegistryError(f"red invalida {self.network!r}: {exc}") from exc
        ids, names, addresses = set(), set(), set()
        reserved = {net.network_address, net.broadcast_address,
                    ipaddress.IPv4Address(self.server_address)}
        for client in self.clients:
            if not 1 <= client.id <= MAX_CLIENT_ID:
                raise RegistryError(f"{client.name}: id {client.id} fuera de rango")
            if client.id in ids:
                raise RegistryError(f"id repetido: {client.id}")
            if not client.name or client.name.lower() in names:
                raise RegistryError(f"nombre vacio o repetido: {client.name!r}")
            if not client.secret:
                raise RegistryError(f"{client.name}: sin secreto")
            try:
                address = ipaddress.IPv4Address(client.address)
            except ValueError as exc:
                raise RegistryError(f"{client.name}: IP invalida {client.address!r}") from exc
            if address not in net or address in reserved:
                raise RegistryError(f"{client.name}: {address} no es una IP de jugador en {net}")
            if address in addresses:
                raise RegistryError(f"IP repetida: {address}")
            ids.add(client.id)
            names.add(client.name.lower())
            addresses.add(address)

    # -- changes ---------------------------------------------------------- #

    def add(self, name: str, secret: str | None = None) -> ClientEntry:
        """Add a player with the next free id and the next free IP."""
        name = name.strip()
        if not name:
            raise RegistryError("el nombre no puede estar vacio")
        if self.find(name):
            raise RegistryError(f"ya existe un jugador llamado {name!r}")
        used_ids = {c.id for c in self.clients}
        used_ips = {c.address for c in self.clients}
        new_id = next(i for i in range(1, MAX_CLIENT_ID + 1) if i not in used_ids)
        free = (str(ip) for ip in self.net.hosts()
                if str(ip) != self.server_address and str(ip) not in used_ips)
        address = next(free, None)
        if address is None:
            raise RegistryError(f"no quedan IPs libres en {self.network}")
        entry = ClientEntry(id=new_id, name=name, secret=secret or generate_secret(),
                            address=address)
        self.clients.append(entry)
        self.validate()
        return entry

    def remove(self, name: str) -> ClientEntry:
        entry = self.find(name)
        if entry is None:
            raise RegistryError(f"no existe el jugador {name!r}")
        self.clients.remove(entry)
        return entry

    # -- file ------------------------------------------------------------- #

    @classmethod
    def load(cls, path: str = DEFAULT_PATH) -> "Registry":
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError as exc:
            raise RegistryError(f"no existe {path}; crealo con 'antilag clients init'") from exc
        except (OSError, ValueError) as exc:
            raise RegistryError(f"no se pudo leer {path}: {exc}") from exc
        try:
            registry = cls(
                endpoint=raw.get("endpoint", ""),
                network=raw.get("network", DEFAULT_NETWORK),
                clients=[ClientEntry(id=int(c["id"]), name=str(c["name"]),
                                     secret=str(c["secret"]), address=str(c["address"]))
                         for c in raw.get("clients", [])],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError(f"{path} tiene un jugador incompleto: {exc}") from exc
        registry.validate()
        return registry

    def save(self, path: str = DEFAULT_PATH) -> None:
        self.validate()
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"endpoint": self.endpoint, "network": self.network,
                       "clients": [asdict(c) for c in self.clients]}, handle, indent=2)
        os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Access code
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AccessCode:
    endpoint: str  # VPS host:port
    client_id: int
    name: str
    #: player's IP inside the tunnel, with prefix (e.g. 10.66.0.3/24)
    address: str
    secret: str


def make_access_code(registry: Registry, entry: ClientEntry) -> str:
    if not registry.endpoint:
        raise RegistryError("falta el endpoint del VPS: 'antilag clients endpoint IP:PUERTO'")
    payload = {
        "e": registry.endpoint,
        "i": entry.id,
        "n": entry.name,
        "a": f"{entry.address}/{registry.net.prefixlen}",
        "k": entry.secret,
    }
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return ACCESS_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def parse_access_code(code: str) -> AccessCode:
    """Read an access code; raises ``RegistryError`` with a message for the user."""
    code = "".join(code.split())  # tolerate line breaks picked up while copying
    if not code.startswith(ACCESS_PREFIX):
        raise RegistryError("no es un codigo de acceso de antilag (debe empezar con "
                            f"'{ACCESS_PREFIX}')")
    body = code[len(ACCESS_PREFIX):]
    try:
        raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        data = json.loads(raw)
        access = AccessCode(endpoint=str(data["e"]), client_id=int(data["i"]),
                            name=str(data["n"]), address=str(data["a"]),
                            secret=str(data["k"]))
        ipaddress.IPv4Interface(access.address)
    except (ValueError, KeyError, TypeError) as exc:
        raise RegistryError("el codigo de acceso esta incompleto o se copio mal") from exc
    if not 1 <= access.client_id <= MAX_CLIENT_ID or not access.secret:
        raise RegistryError("el codigo de acceso esta incompleto o se copio mal")
    host, _, port = access.endpoint.rpartition(":")
    if not host or not port.isdigit():
        raise RegistryError("el codigo de acceso tiene un servidor invalido")
    return access
