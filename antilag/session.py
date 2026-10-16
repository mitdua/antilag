"""Bring the tunnel up and down: adapter, routes, client and watchers.

Used the same way by the command line (``antilag connect``) and the panel. The
startup order matters and shutdown is the reverse: if something fails halfway,
``stop`` undoes only what was actually done.

Besides the tunnel, the session runs a watcher thread:

rediscovery
    Every ``rediscover_s`` it looks at the interfaces again. A new link
    (another phone over USB) joins live; one whose IP or gateway changed (the
    hotspot reconnecting) gets a new socket; one that disappeared and comes
    back does too, because Windows removes the IP when the link drops.
game outside the tunnel
    Checks the connections to the game ports. If any went out with the home IP
    instead of the tunnel's (CipSoft changed the world's IP, or the user
    entered another world) it reports it so the panel can warn.

The panel wizard's preliminary steps also live here: capturing the world's IPs
with the game open and checking each link against the VPS.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass, field

from .client import TunnelClient
from .net.binding import is_strong, make_udp_socket
from .net.connections import ConnectionError_, game_connections, host_of
from .net.discovery import DiscoveryError, Link, discover
from .net.routes import RouteError, RouteManager
from .policy import Mode, PolicyEngine
from .proto import DIR_UP, KIND_PING, KIND_PONG, Codec, ProtocolError, derive_key

log = logging.getLogger(__name__)

#: in duplicate mode, the game goes out over every live link there is
MAX_SENDERS = 16
DEFAULT_REDISCOVER_S = 5.0


class SessionError(Exception):
    """A problem the user can fix; the message is shown to them as is."""


@dataclass
class TunnelConfig:
    server: str  # host:port
    secret: str
    mode: str = Mode.DUPLICATE.value
    #: preferred link for what is not duplicated; None = chosen by measurement
    primary: str | None = None
    #: restrict to these links; empty = every detected one
    links: list[str] = field(default_factory=list)
    #: The world's IPs, as comma-separated /32 prefixes. Only that goes through
    #: the tunnel: this is a tool for the game, not a general VPN (and the
    #: server does not forward anything else either).
    routes: str = ""
    #: which VPS player we are; comes from the access code
    client_id: int = 1
    #: player name, for display only
    name: str = ""
    address: str = "10.66.0.2/24"
    mtu: int = 1400
    tun_name: str = "antilag"
    wintun_dll: str | None = None
    #: tests only: do not touch routes, only address the TUN
    configure_routes: bool = True
    #: seconds between rediscoveries; 0 = never
    rediscover_s: float = DEFAULT_REDISCOVER_S

    def server_addr(self) -> tuple[str, int]:
        host, _, port = self.server.strip().rpartition(":")
        if not host or not port.isdigit():
            raise SessionError(f"servidor invalido {self.server!r}; se espera host:puerto")
        try:
            infos = socket.getaddrinfo(host, int(port), socket.AF_INET, socket.SOCK_DGRAM)
        except socket.gaierror as exc:
            raise SessionError(f"no se pudo resolver {host}: {exc}") from exc
        return infos[0][4][0], int(port)

    @property
    def tunnel_ip(self) -> str:
        return self.address.split("/", 1)[0]

    def game_prefixes(self) -> tuple[str, ...]:
        """The requested routes, validated: single IPs only (/32)."""
        prefixes = []
        for item in (p.strip() for p in self.routes.split(",")):
            if not item:
                continue
            try:
                net = ipaddress.IPv4Network(item if "/" in item else f"{item}/32")
            except ValueError as exc:
                raise SessionError(f"ruta invalida {item!r}: {exc}") from exc
            if net.prefixlen != 32:
                raise SessionError(
                    f"{item}: solo se aceptan IPs del mundo (/32); antilag no es una VPN general")
            prefixes.append(str(net))
        if not prefixes:
            raise SessionError("faltan las IPs del mundo: capturalas con el juego abierto")
        return tuple(prefixes)

    @classmethod
    def from_access_code(cls, code: str, **kw) -> "TunnelConfig":
        from .registry import RegistryError, parse_access_code

        try:
            access = parse_access_code(code)
        except RegistryError as exc:
            raise SessionError(str(exc)) from exc
        return cls(server=access.endpoint, secret=access.secret, client_id=access.client_id,
                   address=access.address, name=access.name, **kw)


def is_admin() -> bool:
    if sys.platform == "win32":
        import ctypes

        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except OSError:
            return False
    return os.geteuid() == 0


def select_links(wanted: list[str], require: bool = True) -> list[Link]:
    links = discover()
    if wanted:
        lower = {name.lower() for name in wanted}
        links = [link for link in links if link.name.lower() in lower]
        missing = lower - {link.name.lower() for link in links}
        if missing and require:
            raise SessionError(f"no se encontraron estos enlaces: {', '.join(sorted(missing))}")
    if not links and require:
        raise SessionError("no hay ningun enlace con salida a internet")
    return links


# --------------------------------------------------------------------------- #
# Wizard preliminary steps
# --------------------------------------------------------------------------- #


def capture_game_ips() -> list[str]:
    """The world's IPs, taken from the open game's connections.

    Only the game ports count: the client's HTTPS connections to
    www.tibia.com must not enter the tunnel.
    """
    try:
        conns = game_connections()
    except ConnectionError_ as exc:
        raise SessionError(f"no se pudieron leer las conexiones: {exc}") from exc
    return sorted({c.remote_address for c in conns})


def routes_for(ips: list[str]) -> str:
    return ",".join(f"{ip}/32" for ip in ips)


@dataclass
class LinkCheck:
    link: Link
    rtt_ms: float | None
    error: str
    strategy: str

    @property
    def ok(self) -> bool:
        return self.rtt_ms is not None


def probe_links(
    server: tuple[str, int],
    secret: str,
    links: list[Link],
    client_id: int = 1,
    timeout: float = 1.5,
    rounds: int = 3,
    socket_factory=make_udp_socket,
) -> list[LinkCheck]:
    """PING/PONG to the VPS over every link, all at once.

    A valid reply proves at the same time that the link reaches the internet,
    that the VPS is up and that the secret matches: the server silently drops
    whatever does not authenticate. That is why a wrong secret looks the same
    as a VPS that is down.
    """
    codec = Codec(derive_key(secret.strip()), DIR_UP, client_id=client_id, max_skew_s=None)
    lock = threading.Lock()
    results: dict[str, LinkCheck] = {}

    def check(link: Link) -> None:
        try:
            sock, strategy = socket_factory(link)
        except OSError as exc:
            results[link.id] = LinkCheck(link, None, f"no se pudo abrir: {exc}", "")
            return
        best: float | None = None
        error = "sin respuesta del VPS"
        try:
            sock.setblocking(True)
            sock.connect(server)
            for _ in range(rounds):
                token = os.urandom(8)
                with lock:
                    _, datagram = codec.seal(token, KIND_PING)
                start = time.perf_counter()
                try:
                    sock.send(datagram)
                except OSError as exc:
                    error = f"no se pudo enviar: {exc}"
                    continue
                deadline = start + timeout
                while (remaining := deadline - time.perf_counter()) > 0:
                    sock.settimeout(remaining)
                    try:
                        data = sock.recv(2048)
                    except socket.timeout:
                        break
                    except OSError as exc:
                        error = f"{type(exc).__name__}: {exc}"
                        break
                    with lock:
                        try:
                            frame = codec.open(data)
                        except ProtocolError:
                            continue
                    if frame is not None and frame.kind == KIND_PONG and frame.payload == token:
                        rtt = (time.perf_counter() - start) * 1000.0
                        best = rtt if best is None else min(best, rtt)
                        break
        finally:
            sock.close()
        results[link.id] = LinkCheck(link, best, "" if best is not None else error, strategy)

    threads = [threading.Thread(target=check, args=(link,), daemon=True) for link in links]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout * rounds + 5)
    return [results.get(link.id, LinkCheck(link, None, "sin resultado", "")) for link in links]


# --------------------------------------------------------------------------- #
# Watchers
# --------------------------------------------------------------------------- #


def link_changed(old: Link, new: Link) -> bool:
    return (old.address, old.gateway, old.index) != (new.address, new.gateway, new.index)


def diff_links(known: dict[str, Link], absent: set[str], found: list[Link]):
    """What to do after a rediscovery.

    Returns (new, changed, gone). A link that had disappeared and comes back
    counts as changed: its old socket may have become useless even if the IP
    is the same.
    """
    added, changed = [], []
    seen = set()
    for link in found:
        seen.add(link.id)
        old = known.get(link.id)
        if old is None:
            added.append(link)
        elif link.id in absent or link_changed(old, link):
            changed.append(link)
    gone = [name for name in known if name not in seen and name not in absent]
    return added, changed, gone


@dataclass
class GameStatus:
    """Game connections seen in the last check."""

    inside: list[str] = field(default_factory=list)
    outside: list[str] = field(default_factory=list)
    checked: bool = False


def classify_game(conns, tunnel_ip: str) -> GameStatus:
    status = GameStatus(checked=True)
    for conn in conns:
        bucket = status.inside if host_of(conn.local_address) == tunnel_ip else status.outside
        if conn.target not in bucket:
            bucket.append(conn.target)
    return status


class TunnelSession:
    def __init__(self, config: TunnelConfig) -> None:
        self.config = config
        self.client: TunnelClient | None = None
        self.links: list[Link] = []
        self.game = GameStatus()
        self._tun = None
        self._routes: RouteManager | None = None
        self._server: tuple[str, int] | None = None
        self._known: dict[str, Link] = {}
        self._absent: set[str] = set()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.client is not None

    def start(self) -> None:
        """Blocks for a few seconds (discovery, adapter, netsh). Call it off the UI."""
        cfg = self.config
        if not cfg.secret.strip():
            raise SessionError("falta el secreto del tunel (el mismo que en el VPS)")
        server = self._server = cfg.server_addr()
        prefixes = cfg.game_prefixes() if cfg.configure_routes else ()
        self.links = select_links(cfg.links)
        if cfg.primary and cfg.primary not in {link.id for link in self.links}:
            raise SessionError(f"el enlace primario {cfg.primary!r} no esta disponible")
        if cfg.configure_routes and not is_admin():
            raise SessionError("hace falta correr como administrador: se instala un "
                               "adaptador y se cambian rutas")
        try:
            mode = Mode(cfg.mode)
        except ValueError as exc:
            raise SessionError(f"modo invalido {cfg.mode!r}") from exc

        try:
            self._open_tun()
            codec = Codec(derive_key(cfg.secret.strip()), DIR_UP, client_id=cfg.client_id,
                          max_skew_s=None)
            engine = PolicyEngine(mode=mode, primary=cfg.primary, max_senders=MAX_SENDERS)
            client = TunnelClient(codec, self._tun, self.links, server, engine)
            weak = [p.name for p in client.paths.values() if not is_strong(p.strategy)]
            if weak and cfg.configure_routes:
                # With a weak binding the tunnel's traffic could re-enter the
                # tunnel itself through the routes: a loop.
                client.stop()
                raise SessionError(
                    f"no se pudo fijar la interfaz de salida de: {', '.join(weak)}")
            self._routes = RouteManager()
            if cfg.configure_routes:
                self._routes.setup(self._tun.name, getattr(self._tun, "index", None),
                                   cfg.address, cfg.mtu, prefixes, server[0], self.links)
            else:
                self._routes.configure_tun(self._tun.name, getattr(self._tun, "index", None),
                                           cfg.address, cfg.mtu)
            client.start()
            self.client = client
        except RouteError as exc:
            self.stop()
            raise SessionError(f"error configurando la red: {exc}") from exc
        except BaseException:
            self.stop()
            raise

        self._known = {link.id: link for link in self.links}
        self._stop.clear()
        self._monitor = threading.Thread(target=self._watch, name="antilag-vigilancia",
                                         daemon=True)
        self._monitor.start()

    def _open_tun(self) -> None:
        if sys.platform == "win32":
            from .tun.wintun import WintunDevice, WintunError

            try:
                self._tun = WintunDevice(self.config.tun_name, dll_path=self.config.wintun_dll)
            except WintunError as exc:
                raise SessionError(f"no se pudo crear el adaptador: {exc}") from exc
        else:
            from .tun.linux import TunDevice

            try:
                self._tun = TunDevice(self.config.tun_name)
            except PermissionError as exc:
                raise SessionError("crear la interfaz TUN exige root o CAP_NET_ADMIN") from exc

    # -- watchers --------------------------------------------------------- #

    def _watch(self) -> None:
        interval = self.config.rediscover_s
        while not self._stop.wait(interval if interval > 0 else 5.0):
            if interval > 0:
                try:
                    self.rediscover()
                except Exception as exc:  # noqa: BLE001 - the watcher must not die
                    log.warning("redescubrimiento fallido: %s", exc)
            try:
                self.game = classify_game(game_connections(), self.config.tunnel_ip)
            except Exception as exc:  # noqa: BLE001
                log.debug("no se pudieron revisar las conexiones del juego: %s", exc)

    def rediscover(self) -> None:
        client, routes, server = self.client, self._routes, self._server
        if client is None or routes is None or server is None:
            return
        try:
            found = select_links(self.config.links, require=False)
        except (DiscoveryError, SessionError) as exc:
            log.debug("descubrimiento fallido: %s", exc)
            return
        added, changed, gone = diff_links(self._known, self._absent, found)
        for name in gone:
            log.info("enlace %s desaparecio", name)
            self._absent.add(name)
        for link in added + changed:
            old = self._known.get(link.id)
            try:
                client.add_link(link)
            except OSError as exc:
                log.warning("no se pudo usar el enlace %s: %s", link.id, exc)
                continue
            if self.config.configure_routes:
                if old is not None:
                    routes.drop_server_route_for(server[0], old)
                try:
                    routes.server_route_for(server[0], link, offset=len(self._known))
                except RouteError as exc:
                    log.warning("ruta al VPS por %s: %s", link.id, exc)
            self._known[link.id] = link
            self._absent.discard(link.id)
        if added or changed:
            self.links = list(self._known.values())

    # -- shutdown and status ---------------------------------------------- #

    def stop(self) -> None:
        """Idempotent; undoes, in reverse order, whatever was done."""
        self._stop.set()
        if self._monitor is not None:
            self._monitor.join(timeout=10)
            self._monitor = None
        if self.client is not None:
            self.client.stop()
            self.client = None
        if self._routes is not None:
            self._routes.close()
            self._routes = None
        if self._tun is not None:
            self._tun.close()
            self._tun = None

    def snapshot(self) -> dict | None:
        client = self.client
        if client is None:
            return None
        snap = client.snapshot()
        snap["events"] = client.engine.snapshot()["events"]
        snap["game"] = {"inside": list(self.game.inside), "outside": list(self.game.outside),
                        "checked": self.game.checked}
        return snap
