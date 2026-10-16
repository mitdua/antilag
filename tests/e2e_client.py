"""End-to-end test: real clients, real server, spikes, outages and several players.

Runs inside ``unshare -rn --fork`` (root only inside a user namespace). Builds
this topology; each PC is a namespace with its own player:

    namespace "PC a"                              namespace "VPS" (this process)
    antilag connect (player 1)   cE ─veth─ vE0      antilag serve (TUN e2e0)
    TUN antilag 10.66.0.2        cW ─veth─ vW0      203.0.113.10 on lo
                                                    "game" 198.51.100.1:7171
    namespace "PC b" (dos-jugadores only)           "web"  198.51.100.1:8080
    antilag connect (player 2)   cE ─veth─ vE1
    TUN antilag 10.66.0.3        cW ─veth─ vW1

On each PC, a TCP conversation with the game port sends a message every 20ms
and measures its echo, while ``tc netem`` injects spikes and outages into the
links. The web port must stay blocked: the VPS only forwards game traffic.

Usage: e2e_client.py [duplicate|adaptive|single] [pico+corte|corte|redescubrimiento|dos-jugadores]
Prints a JSON with the per-phase latencies and exits with 1 if the expectations
are not met.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from antilag.registry import ClientEntry  # noqa: E402
from antilag.server import TunnelServer, serve  # noqa: E402
from antilag.tun.linux import TunDevice  # noqa: E402

SERVER = "203.0.113.10"
GAME = "198.51.100.1"
PLAYERS = [
    ClientEntry(id=1, name="ana", secret="e2e-ana", address="10.66.0.2"),
    ClientEntry(id=2, name="beto", secret="e2e-beto", address="10.66.0.3"),
]

#: (name, start second, action). An action is "PC:netem arguments",
#: "PC:limpio", "PC:@topology-change" or "PC:@reiniciar".
SCENARIOS = {
    "pico+corte": [
        ("sano", 0, None),
        ("pico_400ms_en_cE", 5, "a:delay 400ms"),
        ("recuperado", 10, "a:limpio"),
        # Well after the spike: let emergency duplication be released first so
        # the outage is measured on its own.
        ("corte_total_en_cE", 20, "a:loss 100%"),
        ("restaurado", 26, "a:limpio"),
    ],
    # Only the outage, on the pinned primary and with no previous spikes.
    "corte": [
        ("sano", 0, None),
        ("corte_total_en_cE", 6, "a:loss 100%"),
        ("restaurado", 12, "a:limpio"),
    ],
    # Links that change with the tunnel up: a third one appears (another
    # phone over USB), the Wi-Fi reconnects with another IP, and finally the
    # cable is cut. If rediscovery works, the game never notices.
    "redescubrimiento": [
        ("sano", 0, None),
        ("tercer_enlace_cX", 5, "a:@add-cX"),
        ("wifi_cambia_de_ip", 11, "a:@readdress-cW"),
        ("corte_total_en_cE", 18, "a:loss 100%"),
        ("restaurado", 24, "a:limpio"),
    ],
    # Two players at once: ana's cable is cut and beto restarts his client.
    # Neither of them should notice what happens to the other.
    "dos-jugadores": [
        ("sano", 0, None),
        ("corte_cable_ana", 5, "a:loss 100%"),
        ("beto_reinicia", 9, "b:@reiniciar"),
        ("restaurado", 15, "a:limpio"),
    ],
}
DURATIONS = {"pico+corte": 31, "corte": 17, "redescubrimiento": 28, "dos-jugadores": 20}

DRIVER = r"""
import json, socket, struct, sys, threading, time
total = float(sys.argv[1])
# Non-game traffic must not leave the VPS (even to the same IP).
try:
    socket.create_connection(("198.51.100.1", 8080), timeout=2).close()
    web_blocked = False
except OSError:
    web_blocked = True
s = socket.create_connection(("198.51.100.1", 7171), timeout=5)
s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
s.settimeout(None)
samples, sent = [], {}
def reader():
    buf = b""
    while True:
        try:
            chunk = s.recv(4096)
        except OSError:
            return
        if not chunk:
            return
        buf += chunk
        while len(buf) >= 8:
            (n,) = struct.unpack("!Q", buf[:8]); buf = buf[8:]
            t = sent.pop(n, None)
            if t is not None:
                samples.append((t, (time.monotonic() - t) * 1000))
threading.Thread(target=reader, daemon=True).start()
start, n = time.monotonic(), 0
while time.monotonic() - start < total:
    sent[n] = time.monotonic(); s.sendall(struct.pack("!Q", n)); n += 1
    time.sleep(0.02)
time.sleep(5)
print(json.dumps({"samples": samples, "unanswered": len(sent), "sent": n,
                  "web_blocked": web_blocked}))
"""


def sh(*argv: str, check: bool = True) -> None:
    subprocess.run(argv, check=check, capture_output=True)


def echo_server(host: str, port: int) -> None:
    listener = socket.create_server((host, port))

    def handle(conn: socket.socket) -> None:
        # Like a real game server: no Nagle, every echo goes out immediately.
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)

    while True:
        conn, _ = listener.accept()
        threading.Thread(target=handle, args=(conn,), daemon=True).start()


class Pc:
    """A simulated PC: its own namespace, two links to the VPS and its player."""

    def __init__(self, label: str, index: int, entry: ClientEntry) -> None:
        self.label, self.index, self.entry = label, index, entry
        self.proc = subprocess.Popen(["unshare", "-n", "sleep", "300"])
        self.pid = self.proc.pid
        self.client: subprocess.Popen | None = None
        self.client_outputs: list[tuple[str, str]] = []
        self.driver: subprocess.Popen | None = None
        time.sleep(0.3)
        self.vE, self.vW = f"vE{index}", f"vW{index}"
        base = 1 + 10 * index
        for ours, theirs, net in ((self.vE, "cE", f"172.16.{base}"),
                                  (self.vW, "cW", f"172.16.{base + 1}")):
            sh("ip", "link", "add", ours, "type", "veth", "peer", "name", theirs,
               "netns", str(self.pid))
            sh("ip", "addr", "add", f"{net}.1/24", "dev", ours)
            sh("ip", "link", "set", ours, "up")
            self.run("ip", "addr", "add", f"{net}.2/24", "dev", theirs)
            self.run("ip", "link", "set", theirs, "up")
        self.run("ip", "link", "set", "lo", "up")
        self.run("ip", "route", "add", "default", "via", f"172.16.{base}.1", "dev", "cE",
                 "metric", "10")
        self.run("ip", "route", "add", "default", "via", f"172.16.{base + 1}.1", "dev", "cW",
                 "metric", "20")

    def run(self, *argv: str, check: bool = True) -> None:
        sh("nsenter", "-t", str(self.pid), "-n", "--preserve-credentials", *argv, check=check)

    def popen(self, *argv: str, env=None) -> subprocess.Popen:
        return subprocess.Popen(
            ["nsenter", "-t", str(self.pid), "-n", "--preserve-credentials", *argv],
            env=env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def start_client(self, mode: str, seconds: float) -> None:
        env = dict(os.environ, ANTILAG_SECRET=self.entry.secret, PYTHONPATH=ROOT)
        self.client = self.popen(
            sys.executable, "-m", "antilag", "connect", "--server", f"{SERVER}:51888",
            "--client-id", str(self.entry.id), "--address", f"{self.entry.address}/24",
            "--routes", f"{GAME}/32", "--mode", mode, "--primary", "cE",
            "--duration", str(seconds), "--rediscover", "1", env=env)

    def stop_client(self) -> None:
        if self.client is None:
            return
        self.client.terminate()
        self.client_outputs.append(self.client.communicate(timeout=15))
        self.client = None

    def start_driver(self, seconds: float) -> None:
        self.driver = self.popen(sys.executable, "-c", DRIVER, str(seconds))

    def impair(self, action: str) -> None:
        if action == "@reiniciar":
            # Like closing and reopening the panel: new epoch, new adapter.
            self.stop_client()
            self.start_client(self.mode, self.remaining())
            return
        if action == "@add-cX":
            sh("ip", "link", "add", "vX", "type", "veth", "peer", "name", "cX",
               "netns", str(self.pid))
            sh("ip", "addr", "add", "172.16.3.1/24", "dev", "vX")
            sh("ip", "link", "set", "vX", "up")
            self.run("ip", "addr", "add", "172.16.3.2/24", "dev", "cX")
            self.run("ip", "link", "set", "cX", "up")
            self.run("ip", "route", "add", "default", "via", "172.16.3.1", "dev", "cX",
                     "metric", "30")
            return
        if action == "@readdress-cW":
            # Like a hotspot that reconnects and DHCP gives it another IP.
            self.run("ip", "addr", "del", "172.16.2.2/24", "dev", "cW")
            self.run("ip", "addr", "add", "172.16.2.7/24", "dev", "cW")
            self.run("ip", "route", "replace", "default", "via", "172.16.2.1", "dev", "cW",
                     "metric", "20")
            return
        clear = action == "limpio"
        for runner, dev in ((self.run, "cE"), (sh, self.vE)):
            argv = (["tc", "qdisc", "del", "dev", dev, "root"] if clear else
                    ["tc", "qdisc", "replace", "dev", dev, "root", "netem", *action.split()])
            runner(*argv, check=not clear)

    def remaining(self) -> float:
        return max(5.0, self.deadline - time.monotonic())

    def finish(self) -> tuple[dict, dict]:
        out, err = self.driver.communicate(timeout=60)
        if self.client is not None:
            self.client_outputs.append(self.client.communicate(timeout=30))
        self.proc.kill()
        if self.driver.returncode != 0:
            raise RuntimeError(f"driver de {self.label}: {err}")
        snapshots = [json.loads(line) for out_, _ in self.client_outputs
                     for line in out_.splitlines() if line.startswith("{")]
        return json.loads(out), (snapshots[-1] if snapshots else {})


def start_server() -> TunnelServer:
    tun = TunDevice("e2e0")
    tun.configure("10.66.0.1/24")
    server = TunnelServer(PLAYERS, tun)
    ready = threading.Event()

    def run() -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        event = asyncio.Event()

        async def main() -> None:
            task = asyncio.create_task(serve(server, tun, "0.0.0.0", 51888, ready=event))
            await event.wait()
            ready.set()
            await task

        loop.run_until_complete(main())

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return server


def phase_stats(samples, marks) -> dict:
    phases = {}
    for i, (name, begin) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else float("inf")
        rtts = sorted(r for t, r in samples if begin <= t < end)
        if rtts:
            phases[name] = {"n": len(rtts), "p50_ms": round(rtts[len(rtts) // 2], 1),
                            "max_ms": round(rtts[-1], 1)}
    return phases


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "duplicate"
    scenario = sys.argv[2] if len(sys.argv) > 2 else "pico+corte"
    phases_spec, total_s = SCENARIOS[scenario], DURATIONS[scenario]

    sh("ip", "link", "set", "lo", "up")
    sh("ip", "addr", "add", f"{SERVER}/32", "dev", "lo")
    sh("ip", "link", "add", "game0", "type", "dummy")
    sh("ip", "addr", "add", f"{GAME}/24", "dev", "game0")
    sh("ip", "link", "set", "game0", "up")
    labels = ["a", "b"] if scenario == "dos-jugadores" else ["a"]
    pcs = {label: Pc(label, i, PLAYERS[i]) for i, label in enumerate(labels)}
    server = start_server()
    for port in (7171, 8080):
        threading.Thread(target=echo_server, args=(GAME, port), daemon=True).start()

    for pc in pcs.values():
        pc.mode = mode
        pc.deadline = time.monotonic() + total_s + 11
        pc.start_client(mode, total_s + 8)
    time.sleep(3)  # adapters up and first probes
    for pc in pcs.values():
        if pc.client.poll() is not None:
            print(pc.client.stderr.read(), file=sys.stderr)
            return 1
        pc.start_driver(total_s)

    origin = time.monotonic()
    marks = []
    for name, at, action in phases_spec:
        delay = origin + at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        if action is not None:
            label, _, what = action.partition(":")
            pcs[label].impair(what)
        marks.append((name, time.monotonic()))

    results = {}
    for label, pc in pcs.items():
        result, last = pc.finish()
        results[label] = {
            "player": pc.entry.name,
            "phases": phase_stats(result["samples"], marks),
            "unanswered": result["unanswered"],
            "web_blocked": result["web_blocked"],
            "client": {k: last.get(k) for k in ("up_packets", "up_duplicated", "down_packets")},
            "links": [{k: row[k] for k in ("name", "address", "tx", "first", "late", "alive")}
                      for row in last.get("links", [])],
        }
    report = {"mode": mode, "scenario": scenario, "pcs": results,
              "server": {k: v for k, v in server.snapshot().items() if k != "clients"}}
    print(json.dumps(report, indent=2))

    failures = []
    for label, res in results.items():
        who = res["player"]
        if res["unanswered"]:
            failures.append(f"{who}: {res['unanswered']} mensajes sin eco")
        if not res["web_blocked"]:
            failures.append(f"{who}: el puerto web salio por el VPS (solo debe salir el juego)")
        if not (res["client"].get("up_packets") or 0):
            failures.append(f"{who}: el cliente no reporto trafico")
    a = results["a"]
    if scenario == "redescubrimiento":
        rows = {row["name"]: row for row in a["links"]}
        if "cX" not in rows or not rows["cX"]["alive"] or not rows["cX"]["first"]:
            failures.append("el tercer enlace cX no se sumo o no entrego nada")
        if "cW" in rows and (not rows["cW"]["alive"] or rows["cW"]["address"] != "172.16.2.7"):
            failures.append(f"la Wi-Fi no se recupero con su IP nueva: {rows.get('cW')}")
    if mode == "duplicate":
        # Ana must never notice anything: neither her own outage (duplicated) nor beto's.
        for name, stats in a["phases"].items():
            if stats["max_ms"] > 150:
                failures.append(f"ana, {name}: max {stats['max_ms']}ms con duplicacion")
    for failure in failures:
        print("FALLA:", failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
