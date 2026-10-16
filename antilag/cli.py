"""antilag command line."""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
import os
import sys
import time
import urllib.parse
from datetime import datetime

from .net.connections import (
    ConnectionError_,
    find_game_servers,
    established,
)
from .monitor import (
    DEFAULT_OUTAGE_AFTER,
    DEFAULT_RECOVER_MS,
    DEFAULT_SPIKE_MS,
    EpisodeDetector,
    load_log,
    replay,
)
from .net.discovery import DiscoveryError, discover
from .net.probe import LinkMonitor, TcpConnectProbe
from .policy import Mode

_ANSI_CLEAR_LINE = "\x1b[2K"
_ANSI_UP = "\x1b[{}A"


def _parse_target(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(
            f"destino invalido {value!r}; se espera host:puerto"
        )
    return host, int(port)


def _resolve_links(args) -> list:
    links = discover()
    if args.link:
        wanted = {name.lower() for name in args.link}
        links = [link for link in links if link.name.lower() in wanted]
        missing = wanted - {link.name.lower() for link in links}
        if missing:
            raise SystemExit(
                f"no se encontraron estos enlaces: {', '.join(sorted(missing))}"
            )
    return links


def cmd_links(args) -> int:
    links = _resolve_links(args)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "name": link.name,
                        "kind": link.kind,
                        "address": link.address,
                        "gateway": link.gateway,
                        "index": link.index,
                        "metric": link.metric,
                        "os_state": link.os_state,
                    }
                    for link in links
                ],
                indent=2,
            )
        )
        return 0

    if not links:
        print("No se encontro ningun enlace con salida propia a internet.")
        return 1
    print(f"{len(links)} enlace(s) candidato(s):\n")
    for link in links:
        print(f"  {link.describe()}")
    print(
        "\nEl estado que reporta el sistema es solo una pista. "
        "Para saber cuales sirven de verdad:\n  antilag probe"
    )
    return 0


def _render(snapshot: dict, first: bool) -> int:
    # Redrawing with ANSI sequences only makes sense on a terminal; if the
    # output is redirected or piped, plain lines are written.
    interactive = sys.stdout.isatty()
    rows = snapshot["links"]
    header = (
        f"  {'enlace':<16} {'tipo':<9} {'rtt':>8} {'p95':>8} "
        f"{'jitter':>8} {'perdida':>8}  estado"
    )
    lines = [f"Sondeando {snapshot['target']}", header, "  " + "-" * 68]
    for row in rows:
        rtt = f"{row['rtt_ms']}ms" if row["rtt_ms"] is not None else "--"
        p95 = f"{row['p95_ms']}ms" if row["p95_ms"] is not None else "--"
        if not row["alive"]:
            state = f"CAIDO ({row['last_error'] or 'sin respuesta'})"
        elif not row["trustworthy"]:
            state = "ok (fijacion debil)"
        else:
            state = "ok"
        lines.append(
            f"  {row['name'][:16]:<16} {row['kind']:<9} {rtt:>8} {p95:>8} "
            f"{row['jitter_ms']:>7}ms {row['loss_pct']:>7}%  {state}"
        )

    if not interactive:
        # A single row per sample, with the target already in the header.
        for row in rows:
            rtt = f"{row['rtt_ms']}ms" if row["rtt_ms"] is not None else "--"
            sys.stdout.write(
                f"{row['name']}\t{rtt}\tjitter={row['jitter_ms']}ms"
                f"\tperdida={row['loss_pct']}%\n"
            )
        sys.stdout.flush()
        return 0

    if not first:
        sys.stdout.write(_ANSI_UP.format(len(lines)))
    for line in lines:
        sys.stdout.write(_ANSI_CLEAR_LINE + line + "\n")
    sys.stdout.flush()
    return len(lines)


def _probe_target(args) -> tuple[str, int] | None:
    """The explicit target, or the game server currently connected."""
    if args.target:
        return args.target
    try:
        servers = find_game_servers()
    except ConnectionError_ as exc:
        print(f"no se pudieron leer las conexiones activas: {exc}", file=sys.stderr)
        return None
    if servers:
        server = servers[0]
        if len(servers) > 1:
            # Picking blindly is dangerous: the client keeps a connection to the
            # origin and another to an edge proxy at the same time, and
            # measuring against the edge gives a much better number than the
            # real one.
            print(
                f"Atencion: hay {len(servers)} destinos posibles y se eligio "
                f"{server.target}.\n"
                "  Si alguno es un proxy de borde (Cloudflare, por ejemplo), la\n"
                "  medicion contra el dara un rtt falsamente bueno, porque esos\n"
                "  proxys terminan el handshake TCP en su propio borde.\n"
                "  Para saber cual es cual:  antilag target --whois\n"
                "  Y fijalo a mano con:      --target IP:PUERTO\n",
                file=sys.stderr,
            )
            for candidate in servers:
                print(f"    candidato: {candidate.target}", file=sys.stderr)
            print(file=sys.stderr)
        else:
            print(
                f"Destino tomado del juego en marcha: {server.target} "
                f"(proceso {server.process or 'desconocido'})\n"
            )
        return server.remote_address, server.remote_port
    print(
        "No se pudo determinar contra que medir.\n\n"
        "Las IPs de los mundos no estan en el DNS: tibia.com apunta a AWS y\n"
        "www.tibia.com a Cloudflare, o sea al sitio web. Medir contra esos\n"
        "nombres mide un CDN, no el servidor donde esta tu personaje.\n\n"
        "Abre el juego, entra al mundo y vuelve a ejecutar esto; o pasa el\n"
        "destino a mano con --target IP:PUERTO (antilag target lo averigua).",
        file=sys.stderr,
    )
    return None


async def _probe(args) -> int:
    links = _resolve_links(args)
    if not links:
        print("No se encontro ningun enlace con salida propia a internet.")
        return 1

    target = _probe_target(args)
    if target is None:
        return 2
    host, port = target
    monitor = LinkMonitor(
        links, TcpConnectProbe(host, port, timeout=args.timeout), interval=args.interval
    )
    rounds = None if args.duration <= 0 else max(1, int(args.duration / args.interval))

    if args.json:
        await monitor.run(rounds=rounds)
        print(json.dumps(monitor.snapshot(), indent=2))
        return 0

    state = {"first": True}

    def on_tick(snapshot: dict) -> None:
        _render(snapshot, state["first"])
        state["first"] = False

    try:
        await monitor.run(rounds=rounds, on_tick=on_tick)
    except KeyboardInterrupt:
        monitor.stop()

    weak = [row for row in monitor.snapshot()["links"] if not row["trustworthy"]]
    if weak:
        print(
            "\nAviso: "
            + ", ".join(row["name"] for row in weak)
            + " quedaron fijados solo por direccion de origen. En Linux eso "
            "necesita root para ser firme, asi que esas medidas pueden venir "
            "de la interfaz por defecto."
        )
    return 0


def cmd_probe(args) -> int:
    try:
        return asyncio.run(_probe(args))
    except KeyboardInterrupt:
        return 130


def _asn_of(address: str) -> str:
    """Who announces that IP, as a hint of the region hosting the world."""
    import urllib.error
    import urllib.request

    url = (
        "https://stat.ripe.net/data/prefix-overview/data.json?resource="
        + urllib.parse.quote(address)
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError, OSError):
        return ""
    asns = payload.get("data", {}).get("asns", [])
    if not asns:
        return ""
    return f"AS{asns[0].get('asn')} {asns[0].get('holder', '')}".strip()


def cmd_target(args) -> int:
    try:
        servers = find_game_servers()
        everything = established() if args.all else []
    except ConnectionError_ as exc:
        print(f"error leyendo las conexiones activas: {exc}", file=sys.stderr)
        return 2

    if args.all:
        print("Conexiones establecidas hacia direcciones publicas:\n")
        for conn in everything:
            if conn.is_public:
                flag = "  <- parece el juego" if conn.looks_like_game else ""
                print(f"  {conn.process[:18]:20} {conn.target:24}{flag}")
        print()

    if not servers:
        print(
            "No se detecto ninguna conexion que parezca el servidor de juego.\n"
            "Abre el juego, entra a tu mundo y vuelve a ejecutar esto."
        )
        return 1

    print(f"{len(servers)} destino(s) que parecen servidor de juego:\n")
    for conn in servers:
        line = f"  {conn.target:24} proceso={conn.process or 'desconocido'}"
        if args.whois:
            holder = _asn_of(conn.remote_address)
            if holder:
                line += f"\n    alojado en: {holder}"
        print(line)

    if len(servers) > 1:
        print(
            "\nHay mas de un destino. El cliente de Tibia mantiene a la vez una\n"
            "conexion al origen del mundo y otra a un proxy de borde. Hay que\n"
            "medir contra el ORIGEN: un proxy anycast como Cloudflare Spectrum\n"
            "termina el handshake TCP en su borde, asi que medir contra el da un\n"
            "rtt mucho mejor que el que siente el jugador.\n"
            "Con --whois se ve quien es dueno de cada uno."
        )
    print(
        f"\nPara vigilar picos y cortes contra ese servidor:\n"
        f"  antilag watch --target {servers[0].target} --log antilag.jsonl"
    )
    return 0


def _print_summary(detector: EpisodeDetector, elapsed: float, links: list) -> None:
    print()
    print("=" * 72)
    print(f"  Resumen de {elapsed / 60:.1f} minutos de captura")
    print("=" * 72)
    for link in links:
        resumen = detector.summary(link.name)
        if not resumen["samples"]:
            continue
        print(f"\n  {link.name}  ({link.kind})")
        print(f"    muestras           : {resumen['samples']}")
        print(
            f"    picos              : {resumen['spikes']}"
            f"  ({resumen['spike_seconds']}s en total)"
        )
        if resumen["worst_spike_ms"] is not None:
            print(f"    peor pico          : {resumen['worst_spike_ms']}ms")
        print(
            f"    cortes             : {resumen['outages']}"
            f"  ({resumen['outage_seconds']}s en total)"
        )
        if resumen["outages"]:
            print(f"    corte mas largo    : {resumen['longest_outage_s']}s")
            print(
                "    Un corte de varios segundos es el caso grave: ahi es donde"
                " muere el personaje."
            )
        if elapsed > 0:
            malo = resumen["spike_seconds"] + resumen["outage_seconds"]
            print(f"    tiempo degradado   : {100.0 * malo / elapsed:.2f}%")

    episodios = [e for e in detector.episodes]
    if episodios:
        print(f"\n  Episodios en orden ({len(episodios)}):\n")
        for episode in episodios[-40:]:
            pico = (
                f"pico {episode.peak_rtt_ms:.0f}ms"
                if episode.peak_rtt_ms is not None
                else "sin respuesta"
            )
            print(
                f"    t+{episode.started_at:>7.1f}s  {episode.kind:<6}"
                f"  {episode.link:<14} {episode.duration_s():>6.1f}s  {pico}"
            )
        if len(episodios) > 40:
            print(f"    ... y {len(episodios) - 40} mas (estan todos en el log)")
    else:
        print("\n  Ningun pico ni corte en toda la captura.")


async def _watch(args) -> int:
    links = _resolve_links(args)
    if not links:
        print("No se encontro ningun enlace con salida propia a internet.")
        return 1
    target = _probe_target(args)
    if target is None:
        return 2
    host, port = target

    monitor = LinkMonitor(
        links, TcpConnectProbe(host, port, timeout=args.timeout), interval=args.interval
    )
    detector = EpisodeDetector(
        spike_ms=args.spike_ms,
        recover_ms=args.recover_ms,
        outage_after=args.outage_after,
    )
    rounds = None if args.duration <= 0 else max(1, int(args.duration / args.interval))

    log = None
    if args.log:
        log = open(args.log, "a", encoding="utf-8", buffering=1)

    started = time.time()
    interactive = sys.stdout.isatty()
    print(
        f"Vigilando {host}:{port} cada {args.interval}s. "
        f"Pico > {args.spike_ms}ms, corte tras {args.outage_after} perdidas seguidas."
    )
    if args.log:
        print(f"Log en {args.log}")
    print("Ctrl-C para terminar y ver el resumen.\n")

    state = {"lines": 0}

    def on_tick(_snapshot: dict) -> None:
        now = time.time() - started
        muestras: dict[str, float | None] = {}
        for health in monitor.health.values():
            sample = health.history[-1] if health.history else None
            muestras[health.link.name] = (
                round(sample, 1) if sample is not None else None
            )
            detector.observe(health.link.name, sample, now)

        if log is not None:
            log.write(
                json.dumps(
                    {
                        "t": round(now, 3),
                        "iso": datetime.now().isoformat(timespec="seconds"),
                        "links": muestras,
                    }
                )
                + "\n"
            )

        lineas = [
            f"  t+{now / 60:>6.1f} min   objetivo {host}:{port}",
            f"  {'enlace':<16} {'rtt':>9} {'picos':>7} {'cortes':>8}  estado",
            "  " + "-" * 62,
        ]
        for health in monitor.ranked():
            nombre = health.link.name
            sample = muestras.get(nombre)
            resumen = detector.summary(nombre)
            abierto = detector._open.get(nombre)
            if abierto is not None:
                estado = f"EN {abierto.kind.upper()} ({abierto.duration_s(now):.0f}s)"
            else:
                estado = "ok"
            rtt = f"{sample}ms" if sample is not None else "sin resp."
            lineas.append(
                f"  {nombre[:16]:<16} {rtt:>9} {resumen['spikes']:>7}"
                f" {resumen['outages']:>8}  {estado}"
            )

        if interactive:
            if state["lines"]:
                sys.stdout.write(_ANSI_UP.format(state["lines"]))
            for line in lineas:
                sys.stdout.write(_ANSI_CLEAR_LINE + line + "\n")
            state["lines"] = len(lineas)
        else:
            for nombre, sample in muestras.items():
                rtt = f"{sample}ms" if sample is not None else "sin-respuesta"
                sys.stdout.write(f"t+{now:.1f}\t{nombre}\t{rtt}\n")
        sys.stdout.flush()

    try:
        await monitor.run(rounds=rounds, on_tick=on_tick)
    except (KeyboardInterrupt, asyncio.CancelledError):
        monitor.stop()
    finally:
        elapsed = time.time() - started
        detector.close_all(elapsed)
        if log is not None:
            log.close()
        _print_summary(detector, elapsed, links)
    return 0


def cmd_watch(args) -> int:
    try:
        return asyncio.run(_watch(args))
    except KeyboardInterrupt:
        return 130


def cmd_report(args) -> int:
    try:
        rows = load_log(args.log)
    except OSError as exc:
        print(f"no se pudo leer el log: {exc}", file=sys.stderr)
        return 2
    if not rows:
        print("el log esta vacio")
        return 1

    detector = replay(
        rows,
        spike_ms=args.spike_ms,
        recover_ms=args.recover_ms,
        outage_after=args.outage_after,
    )
    elapsed = float(rows[-1].get("t", 0)) - float(rows[0].get("t", 0))
    nombres = sorted({name for row in rows for name in (row.get("links") or {})})

    class _Fake:
        def __init__(self, name):
            self.name = name
            self.kind = "-"

    print(
        f"Reanalizado {args.log}: {len(rows)} muestras, "
        f"pico > {args.spike_ms}ms, corte tras {args.outage_after} perdidas."
    )
    _print_summary(detector, elapsed, [_Fake(n) for n in nombres])
    return 0


def cmd_keygen(args) -> int:
    # Lazy import on purpose: 'cryptography' is only needed for the tunnel.
    # That way links, target, probe, watch and report run without installing
    # anything.
    from .proto import generate_secret

    secret = generate_secret()
    print(secret)
    if not args.quiet:
        print(
            "\nEl mismo secreto va en el cliente y en el servidor del VPS. "
            "Tratalo como una contrasena.",
            file=sys.stderr,
        )
    return 0


def _load_secret(path: str | None) -> str:
    """The secret comes from a file or from ANTILAG_SECRET, never from argv.

    In argv it would be visible to anyone listing processes.
    """
    if path:
        with open(path, encoding="utf-8") as handle:
            secret = handle.read().strip()
    else:
        secret = os.environ.get("ANTILAG_SECRET", "").strip()
    if not secret:
        raise SystemExit(
            "falta el secreto: --secret-file ARCHIVO o la variable ANTILAG_SECRET "
            "(generalo con 'antilag keygen')"
        )
    return secret


def cmd_serve(args) -> int:
    import logging
    import signal

    if not sys.platform.startswith("linux"):
        print("serve corre en el VPS, que tiene que ser Linux", file=sys.stderr)
        return 2

    from .registry import Registry, RegistryError
    from .server import TunnelServer, serve
    from .tun.linux import TunDevice

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log = logging.getLogger("antilag.serve")
    try:
        registry = Registry.load(args.clients)
    except RegistryError as exc:
        print(exc, file=sys.stderr)
        return 2
    try:
        tun = TunDevice(args.tun)
        if not args.no_configure:
            tun.configure(f"{registry.server_address}/{registry.net.prefixlen}", mtu=args.mtu)
    except PermissionError:
        print("crear la interfaz TUN exige root o CAP_NET_ADMIN", file=sys.stderr)
        return 2

    server = TunnelServer(registry.clients, tun)
    log.info("%d jugador(es): %s", len(registry.clients),
             ", ".join(c.name for c in registry.clients) or "-")

    def reload() -> None:
        # SIGHUP ('systemctl reload antilag'): players that did not change stay
        # connected; removed ones are out immediately.
        try:
            report = server.load(Registry.load(args.clients).clients)
        except RegistryError as exc:
            log.error("no se recargaron los jugadores: %s", exc)
            return
        log.info("jugadores recargados: %s", report)

    async def main() -> None:
        asyncio.get_running_loop().add_signal_handler(signal.SIGHUP, reload)
        await serve(server, tun, *args.listen, stats_every=args.stats,
                    status_path=args.status)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        tun.close()
    return 0


def _reload_server() -> None:
    """Tell the service to re-read the players, if it is running."""
    import shutil
    import subprocess

    if shutil.which("systemctl"):
        result = subprocess.run(["systemctl", "reload", "antilag"], capture_output=True)
        if result.returncode == 0:
            print("servicio recargado: el cambio ya esta activo")
            return
    print("recuerda recargar el servidor: systemctl reload antilag")


def cmd_clients(args) -> int:
    from .registry import Registry, RegistryError, make_access_code

    path = args.clients
    try:
        if args.action == "init":
            if os.path.exists(path):
                registry = Registry.load(path)
            else:
                registry = Registry()
            if args.endpoint:
                registry.endpoint = f"{args.endpoint[0]}:{args.endpoint[1]}"
            if args.secret_file and not registry.clients:
                # Migration: the previous secret becomes player 1's.
                with open(args.secret_file, encoding="utf-8") as handle:
                    secret = handle.read().strip()
                entry = registry.add(args.name, secret=secret)
                print(f"migrado: {entry.name} (id {entry.id}, {entry.address}) "
                      f"con el secreto de {args.secret_file}")
            registry.save(path)
            print(f"{path}: {len(registry.clients)} jugador(es), endpoint "
                  f"{registry.endpoint or '(sin definir)'}")
            return 0

        registry = Registry.load(path)
        if args.action == "endpoint":
            registry.endpoint = f"{args.value[0]}:{args.value[1]}"
            registry.save(path)
            print(f"endpoint: {registry.endpoint}. Los codigos nuevos lo usan; los ya "
                  "entregados apuntan al anterior.")
            return 0

        if args.action == "add":
            entry = registry.add(args.name)
            registry.save(path)
            print(f"agregado {entry.name}: id {entry.id}, IP {entry.address}\n")
            if registry.endpoint:
                print("Codigo de acceso (pegarlo en el paso 2 del panel; es una contrasena):\n")
                print(make_access_code(registry, entry))
                print()
            else:
                print("Falta el endpoint del VPS para generar su codigo: "
                      "'antilag clients endpoint IP:PUERTO' y despues "
                      f"'antilag clients code {entry.name}'\n")
            _reload_server()
            return 0

        if args.action == "code":
            entry = registry.find(args.name)
            if entry is None:
                raise RegistryError(f"no existe el jugador {args.name!r}")
            print(make_access_code(registry, entry))
            return 0

        if args.action == "remove":
            entry = registry.remove(args.name)
            registry.save(path)
            print(f"quitado {entry.name}; su codigo de acceso ya no sirve")
            _reload_server()
            return 0

        # list
        status = _read_status(args.status)
        online = {c["id"]: c for c in status.get("clients", [])}
        print(f"endpoint {registry.endpoint or '(sin definir)'}  red {registry.network}\n")
        print(f"  {'id':>3}  {'nombre':<16} {'ip':<14} estado")
        for entry in registry.clients:
            live = online.get(entry.id)
            idle = live.get("idle_s") if live else None
            if idle is not None and idle < 15:
                links = sum(1 for p in live["paths"] if p["idle_s"] < 15)
                state = f"en linea, {links} enlace(s)"
            elif idle is not None:
                state = f"visto hace {idle:.0f}s"
            else:
                state = "sin conectar desde que arranco el servidor"
            print(f"  {entry.id:>3}  {entry.name[:16]:<16} {entry.address:<14} {state}")
        return 0
    except RegistryError as exc:
        print(exc, file=sys.stderr)
        return 2


def _read_status(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError):
        return {}
    # A stale status (service down) says nothing about who is online.
    return status if time.time() - status.get("at", 0) < 30 else {}


def _render_tunnel(snapshot: dict, first: bool) -> int:
    lines = [
        f"Tunel a {snapshot['server']}  modo={snapshot['mode'] or '-'}  "
        f"emisores={','.join(snapshot['senders']) or '-'}",
        f"  {snapshot['reason']}",
        f"  subida={snapshot['up_packets']} (duplicados {snapshot['up_duplicated']})  "
        f"bajada={snapshot['down_packets']}  copias descartadas={snapshot['codec']['duplicates']}",
        f"  {'enlace':<16} {'rtt':>8} {'p95':>8} {'jitter':>8} {'perdida':>8} {'gana':>6}  estado",
    ]
    for row in snapshot["links"]:
        rtt = f"{row['rtt_ms']}ms" if row["rtt_ms"] is not None else "--"
        p95 = f"{row['p95_ms']}ms" if row["p95_ms"] is not None else "--"
        state = "ok" if row["alive"] else f"CAIDO ({row['last_error'] or 'sin respuesta'})"
        if row["alive"] and not row["trustworthy"]:
            state = "ok (fijacion debil)"
        lines.append(
            f"  {row['name'][:16]:<16} {rtt:>8} {p95:>8} {row['jitter_ms']:>7}ms "
            f"{row['loss_pct']:>7}% {row['first']:>6}  {state}"
        )
    if not sys.stdout.isatty():
        sys.stdout.write(json.dumps(snapshot) + "\n")
        sys.stdout.flush()
        return 0
    if not first:
        sys.stdout.write(_ANSI_UP.format(len(lines)))
    for line in lines:
        sys.stdout.write(_ANSI_CLEAR_LINE + line + "\n")
    sys.stdout.flush()
    return len(lines)


def cmd_connect(args) -> int:
    import logging

    from .session import SessionError, TunnelConfig, TunnelSession

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    common = dict(
        mode=args.mode,
        primary=args.primary,
        links=args.link,
        routes=args.routes,
        mtu=args.mtu,
        tun_name=args.tun,
        wintun_dll=args.wintun_dll,
        configure_routes=not args.no_routes,
        rediscover_s=args.rediscover,
    )
    try:
        if args.code:
            config = TunnelConfig.from_access_code(args.code, **common)
        elif args.server:
            config = TunnelConfig(server=f"{args.server[0]}:{args.server[1]}",
                                  secret=_load_secret(args.secret_file),
                                  client_id=args.client_id, address=args.address, **common)
        else:
            print("falta --code (o --server con el secreto)", file=sys.stderr)
            return 2
    except SessionError as exc:
        print(exc, file=sys.stderr)
        return 2
    session = TunnelSession(config)
    try:
        session.start()
        lines = 0
        started = time.monotonic()
        while args.duration <= 0 or time.monotonic() - started < args.duration:
            time.sleep(1)
            lines = _render_tunnel(session.snapshot(), lines == 0) or lines
    except SessionError as exc:
        print(exc, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
    return 0


def cmd_panel(args) -> int:
    try:
        from .ui.app import run
    except ImportError:
        print("el panel necesita Flet: python -m pip install \"flet>=1.0\"", file=sys.stderr)
        return 2
    run(demo=args.demo, screenshot=args.screenshot, after=args.after)
    return 0


def cmd_fetch_wintun(args) -> int:
    from .tun.wintun import WINTUN_URL, fetch_dll

    print(f"bajando {WINTUN_URL} ...")
    path = fetch_dll(args.dest)
    print(f"hash verificado; wintun.dll en {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="antilag",
        description=(
            "Failover multi-enlace que mantiene viva la sesion del juego: la IP "
            "publica no cambia porque el trafico sale siempre por tu VPS."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    links = sub.add_parser("links", help="listar los enlaces candidatos detectados")
    links.add_argument("--json", action="store_true", help="salida legible por maquina")
    links.add_argument(
        "--link", action="append", default=[], metavar="NOMBRE",
        help="limitar a este enlace (repetible)",
    )
    links.set_defaults(func=cmd_links)

    probe = sub.add_parser(
        "probe",
        help="medir rtt, jitter y perdida por enlace",
        description=(
            "Mide cada enlace por separado con handshakes TCP, sin privilegios. "
            "Usalo para elegir la region del VPS y para comprobar si el enlace "
            "secundario es de verdad mejor durante un pico."
        ),
    )
    probe.add_argument(
        "--target", type=_parse_target, default=None, metavar="HOST:PUERTO",
        help=(
            "destino a medir. Si se omite, se toma del servidor de juego que "
            "este conectado en este momento"
        ),
    )
    probe.add_argument("--interval", type=float, default=1.0, metavar="SEG")
    probe.add_argument(
        "--duration", type=float, default=0, metavar="SEG",
        help="segundos a medir; 0 = hasta Ctrl-C",
    )
    probe.add_argument("--timeout", type=float, default=2.0, metavar="SEG")
    probe.add_argument("--json", action="store_true")
    probe.add_argument(
        "--link", action="append", default=[], metavar="NOMBRE",
        help="limitar a este enlace (repetible)",
    )
    probe.set_defaults(func=cmd_probe)

    target = sub.add_parser(
        "target",
        help="averiguar la ip del servidor de juego desde las conexiones activas",
        description=(
            "Las IPs de los mundos de Tibia no estan en el DNS. Este comando las "
            "saca de las conexiones TCP establecidas del cliente, que es la "
            "unica fuente fiable, y requiere tener el juego abierto."
        ),
    )
    target.add_argument(
        "--all", action="store_true",
        help="listar todas las conexiones publicas, no solo las que parecen juego",
    )
    target.add_argument(
        "--whois", action="store_true",
        help="consultar quien anuncia la ip, como pista de la region de alojamiento",
    )
    target.set_defaults(func=cmd_target)

    def _umbrales(parser_) -> None:
        parser_.add_argument(
            "--spike-ms", type=float, default=DEFAULT_SPIKE_MS, metavar="MS",
            help=f"rtt a partir del cual cuenta como pico (por defecto {DEFAULT_SPIKE_MS:.0f})",
        )
        parser_.add_argument(
            "--recover-ms", type=float, default=DEFAULT_RECOVER_MS, metavar="MS",
            help="rtt al que un pico se considera terminado (histeresis)",
        )
        parser_.add_argument(
            "--outage-after", type=int, default=DEFAULT_OUTAGE_AFTER, metavar="N",
            help="perdidas seguidas para declarar un corte",
        )

    watch = sub.add_parser(
        "watch",
        help="vigilancia larga que captura picos y cortes",
        description=(
            "Para este problema un promedio no sirve: un enlace que esta el 99% "
            "del tiempo en 100ms y el 1% en 500ms promedia 104ms y parece sano, "
            "pero ese 1% es donde muere el personaje. Esto captura la forma de "
            "los episodios: cuantos, cuanto duran y hasta donde llegan."
        ),
    )
    watch.add_argument(
        "--target", type=_parse_target, default=None, metavar="HOST:PUERTO",
        help="destino a vigilar; si se omite se toma del juego en marcha",
    )
    watch.add_argument("--interval", type=float, default=1.0, metavar="SEG")
    watch.add_argument(
        "--duration", type=float, default=0, metavar="SEG",
        help="segundos a vigilar; 0 = hasta Ctrl-C",
    )
    watch.add_argument("--timeout", type=float, default=2.0, metavar="SEG")
    watch.add_argument(
        "--log", metavar="ARCHIVO",
        help="guardar cada muestra en jsonl, para reanalizar despues",
    )
    watch.add_argument(
        "--link", action="append", default=[], metavar="NOMBRE",
        help="limitar a este enlace (repetible)",
    )
    _umbrales(watch)
    watch.set_defaults(func=cmd_watch)

    report = sub.add_parser(
        "report",
        help="reanalizar un log de watch, con otros umbrales si hace falta",
    )
    report.add_argument("log", metavar="ARCHIVO")
    _umbrales(report)
    report.set_defaults(func=cmd_report)

    keygen = sub.add_parser("keygen", help="generar el secreto compartido del tunel")
    keygen.add_argument("--quiet", action="store_true")
    keygen.set_defaults(func=cmd_keygen)

    serve = sub.add_parser(
        "serve",
        help="servidor del tunel, en el VPS (Linux, root)",
        description=(
            "Una sesion por jugador: recibe las copias de cada enlace, se queda con "
            "la primera, saca el trafico del juego por NAT con la IP publica del VPS "
            "y replica la bajada por los enlaces de cada jugador. Los jugadores "
            "salen de --clients; 'systemctl reload antilag' los relee sin cortar a "
            "nadie. El NAT y el forwarding los deja listos deploy/setup-vps.sh."
        ),
    )
    serve.add_argument(
        "--listen", type=_parse_target, default=("0.0.0.0", 51888), metavar="IP:PUERTO",
        help="direccion udp de escucha (por defecto 0.0.0.0:51888)",
    )
    serve.add_argument("--clients", default="/etc/antilag/clients.json", metavar="ARCHIVO")
    serve.add_argument("--status", default="/run/antilag/status.json", metavar="ARCHIVO",
                       help="donde dejar el estado de los jugadores (lo lee 'clients list')")
    serve.add_argument("--tun", default="antilag0", metavar="NOMBRE")
    serve.add_argument("--mtu", type=int, default=1400)
    serve.add_argument(
        "--no-configure", action="store_true",
        help="no tocar la interfaz: direccion y mtu ya los puso otro",
    )
    serve.add_argument(
        "--stats", type=float, default=60, metavar="SEG",
        help="cada cuanto registrar el estado en el log; 0 = nunca",
    )
    serve.add_argument("--verbose", action="store_true")
    serve.set_defaults(func=cmd_serve)

    clients = sub.add_parser(
        "clients",
        help="jugadores del VPS: agregar, listar, quitar",
        description=(
            "Cada jugador tiene su propio secreto y su IP dentro del tunel. 'add' "
            "imprime el codigo de acceso que el jugador pega en su panel."
        ),
    )
    clients.add_argument("--clients", default="/etc/antilag/clients.json", metavar="ARCHIVO")
    clients.add_argument("--status", default="/run/antilag/status.json", help=argparse.SUPPRESS)
    clients_sub = clients.add_subparsers(dest="action", required=True)
    init = clients_sub.add_parser("init", help="crear el archivo (o migrar el secreto viejo)")
    init.add_argument("--endpoint", type=_parse_target, metavar="IP:PUERTO",
                      help="como llegan los jugadores al VPS (va en cada codigo)")
    init.add_argument("--secret-file", metavar="ARCHIVO",
                      help="secreto de la version anterior: pasa a ser el del jugador 1")
    init.add_argument("--name", default="principal", help=argparse.SUPPRESS)
    endpoint = clients_sub.add_parser("endpoint", help="cambiar la IP:puerto publica del VPS")
    endpoint.add_argument("value", type=_parse_target, metavar="IP:PUERTO")
    add = clients_sub.add_parser("add", help="agregar un jugador e imprimir su codigo")
    add.add_argument("name", metavar="NOMBRE")
    code = clients_sub.add_parser("code", help="volver a imprimir el codigo de un jugador")
    code.add_argument("name", metavar="NOMBRE")
    remove = clients_sub.add_parser("remove", help="quitar un jugador (deja de conectar)")
    remove.add_argument("name", metavar="NOMBRE")
    clients_sub.add_parser("list", help="jugadores y quien esta en linea")
    clients.set_defaults(func=cmd_clients)

    connect = sub.add_parser(
        "connect",
        help="levantar el tunel en este PC (administrador)",
        description=(
            "Crea el adaptador del tunel y manda por el VPS el trafico hacia las IPs "
            "del mundo, asi la IP que ve el juego no cambia aunque cambie el enlace. "
            "Las IPs del mundo las muestra 'antilag target' con el personaje dentro. "
            "Lo normal es usar el panel, que hace todo esto con un asistente."
        ),
    )
    connect.add_argument(
        "--code", metavar="CODIGO",
        help="codigo de acceso de 'antilag clients add' (servidor, jugador y secreto)",
    )
    connect.add_argument("--server", type=_parse_target, metavar="HOST:PUERTO",
                         help="sin --code: el VPS")
    connect.add_argument(
        "--secret-file", metavar="ARCHIVO",
        help="sin --code: archivo con el secreto; si se omite se lee ANTILAG_SECRET",
    )
    connect.add_argument("--client-id", type=int, default=1, help="sin --code: id del jugador")
    connect.add_argument(
        "--link", action="append", default=[], metavar="NOMBRE",
        help="usar solo este enlace (repetible); por defecto, todos los detectados",
    )
    connect.add_argument(
        "--mode", choices=[m.value for m in Mode], default=Mode.DUPLICATE.value,
        help=(
            "duplicate (por defecto): el juego sale siempre por los dos enlaces y "
            "un pico en uno no se nota. adaptive: duplica solo tras 2s de "
            "degradacion. single: un enlace, failover al degradarse"
        ),
    )
    connect.add_argument(
        "--primary", metavar="NOMBRE",
        help="enlace preferido para el trafico que no se duplica (p. ej. el que no gasta datos)",
    )
    connect.add_argument(
        "--routes", required=True, metavar="IP,IP,...",
        help="IPs del mundo (las de 'antilag target'); solo eso va por el tunel",
    )
    connect.add_argument("--no-routes", action="store_true", help=argparse.SUPPRESS)
    connect.add_argument("--rediscover", type=float, default=5.0, help=argparse.SUPPRESS)
    connect.add_argument("--tun", default="antilag", metavar="NOMBRE")
    connect.add_argument("--address", default="10.66.0.2/24", metavar="IP/PREFIJO",
                         help="sin --code: IP del jugador dentro del tunel")
    connect.add_argument("--mtu", type=int, default=1400)
    connect.add_argument("--wintun-dll", metavar="RUTA")
    connect.add_argument(
        "--duration", type=float, default=0, metavar="SEG",
        help="segundos con el tunel arriba; 0 = hasta Ctrl-C",
    )
    connect.add_argument("--verbose", action="store_true")
    connect.set_defaults(func=cmd_connect)

    panel = sub.add_parser(
        "panel",
        help="abrir el panel grafico (administrador para conectar)",
    )
    panel.add_argument(
        "--demo", action="store_true",
        help="enlaces simulados con picos y cortes: ver el panel sin VPS ni administrador",
    )
    panel.add_argument("--screenshot", metavar="PNG", help=argparse.SUPPRESS)
    panel.add_argument("--after", type=float, default=8.0, help=argparse.SUPPRESS)
    panel.set_defaults(func=cmd_panel)

    fetch = sub.add_parser(
        "fetch-wintun",
        help="bajar wintun.dll (driver del adaptador en Windows) verificando su hash",
    )
    fetch.add_argument("--dest", metavar="RUTA", help="donde dejarla; por defecto junto al paquete")
    fetch.set_defaults(func=cmd_fetch_wintun)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except DiscoveryError as exc:
        print(f"error consultando la red del sistema: {exc}", file=sys.stderr)
        return 2
    except ConnectionError_ as exc:
        print(f"error leyendo las conexiones activas: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
