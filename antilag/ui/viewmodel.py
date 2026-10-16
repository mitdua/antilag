"""What the panel shows, computed from the client's snapshot.

Kept apart from Flet on purpose: all of this is tested without opening windows.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

#: the threshold that makes Tibia unplayable, and where yellow begins
BAD_MS = 300.0
WARN_MS = 180.0
#: seconds of history in each link's chart
HISTORY = 120

GOOD, WARN, BAD, IDLE, ACCENT = "good", "warn", "bad", "idle", "accent"


def rtt_tone(rtt_ms: float | None) -> str:
    if rtt_ms is None:
        return BAD
    if rtt_ms >= BAD_MS:
        return BAD
    if rtt_ms >= WARN_MS:
        return WARN
    return GOOD


@dataclass(frozen=True)
class LinkState:
    label: str
    tone: str
    detail: str


def link_state(row: dict, senders: list[str], suspect: list[str]) -> LinkState:
    """A link's status chip."""
    name = row["name"]
    if not row["alive"]:
        return LinkState("CAÍDO", BAD, row.get("last_error") or "sin respuesta")
    if name in suspect:
        return LinkState("SOSPECHA", WARN, "ping atrasado: juego duplicado de emergencia")
    if name in senders:
        detail = "lleva el juego" if len(senders) > 1 else "enlace primario"
        return LinkState("EMISOR", GOOD, detail)
    return LinkState("EN ESPERA", IDLE, "listo para tomar el relevo")


def overall_status(rows: list[dict], suspect: list[str]) -> tuple[str, str]:
    """The overall status pill in the header."""
    dead = [row for row in rows if not row["alive"]]
    if rows and len(dead) == len(rows):
        return "SIN ENLACES", BAD
    if dead:
        return f"{len(dead)} ENLACE CAÍDO" if len(dead) == 1 else f"{len(dead)} ENLACES CAÍDOS", WARN
    if suspect:
        return "PROTEGIENDO", WARN
    return "CONECTADO", GOOD


def win_shares(rows: list[dict]) -> dict[str, float]:
    """Fraction of the copies each link delivered first."""
    total = sum(row.get("first", 0) for row in rows)
    if not total:
        return {row["name"]: 0.0 for row in rows}
    return {row["name"]: row.get("first", 0) / total for row in rows}


def fmt_ms(value: float | None) -> str:
    if value is None:
        return "--"
    return f"{value:.0f}" if value >= 10 else f"{value:.1f}"


def fmt_count(value: int | None) -> str:
    if value is None:
        return "--"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 10_000:
        return f"{value / 1_000:.0f}k"
    return f"{value:,}".replace(",", ".")


class RttHistory:
    """One sample per second per link; None marks a second without a reply."""

    def __init__(self, size: int = HISTORY) -> None:
        self.size = size
        self.series: dict[str, deque[float | None]] = {}

    def push(self, rows: list[dict]) -> None:
        for row in rows:
            series = self.series.setdefault(row["name"], deque(maxlen=self.size))
            series.append(row["last_rtt_ms"] if row["alive"] else None)

    def bars(self, name: str, height: float, ceiling_ms: float = 600.0) -> list[tuple[float, str]]:
        """(height in px, tone) per sample, right-aligned.

        Fixed scale, not automatic: a 500ms spike must always look tall, not
        only when it is the largest in the window. A loss is drawn as a full
        red bar.
        """
        series = list(self.series.get(name, ()))
        out: list[tuple[float, str]] = [(0.0, IDLE)] * (self.size - len(series))
        for value in series:
            if value is None:
                out.append((height, BAD))
            else:
                ratio = min(1.0, value / ceiling_ms)
                out.append((max(2.0, ratio * height), rtt_tone(value)))
        return out


def event_lines(events: list[dict], now_mono: float | None = None,
                now_wall: float | None = None, limit: int = 12) -> list[tuple[str, str]]:
    """(time, reason) of the switches, most recent first.

    The policy stamps events with a monotonic clock; here they become local time.
    """
    now_mono = time.monotonic() if now_mono is None else now_mono
    now_wall = time.time() if now_wall is None else now_wall
    lines = []
    for event in reversed(events[-limit:]):
        wall = now_wall - (now_mono - event["at"])
        lines.append((time.strftime("%H:%M:%S", time.localtime(wall)), event["reason"]))
    return lines


MODE_LABELS = {
    "duplicate": "Duplicado",
    "adaptive": "Adaptativo",
    "single": "Fallback",
}

MODE_HELP = {
    "duplicate": "El juego sale siempre por todos los enlaces y gana la copia que llega "
                 "primero: ni los picos ni los cortes se notan.",
    "adaptive": "Un enlace mientras esté sano; en cuanto un ping se atrasa (~0,3 s) "
                "duplica el juego por los demás hasta que se estabilice.",
    "single": "Un enlace; cuando falla, el juego pasa al siguiente. Un corte brusco "
              "se nota como un tirón de menos de un segundo.",
}

MODE_COST = {
    "duplicate": "Recomendado · usa un poco de datos móviles (solo el juego, pocos kbps)",
    "adaptive": "Casi no usa datos móviles mientras el enlace principal esté sano",
    "single": "El que menos datos usa",
}

#: with fewer than this there is nowhere to switch to: the app makes no sense
MIN_LINKS = 2


def game_banner(game: dict | None, captured: list[str]) -> tuple[str, str, str]:
    """(title, detail, tone) of the notice about the game's connections."""
    if not game or not game.get("checked"):
        return ("Túnel listo: ya puedes entrar al mundo en Tibia",
                "Entra con un personaje del mismo mundo donde capturaste las IPs.", GOOD)
    if game.get("outside"):
        return ("Tibia se conectó por fuera del túnel",
                f"Conexión directa a {', '.join(game['outside'])}. Ese mundo no está en el "
                "túnel: un corte de enlace te desconectaría. Sal del mundo, desconecta y "
                "vuelve a capturar las IPs con el personaje que vas a usar.", BAD)
    if game.get("inside"):
        return ("Jugando a través del túnel",
                f"Tibia está conectado por el túnel a {', '.join(game['inside'])}.", GOOD)
    return ("Túnel listo: ya puedes entrar al mundo en Tibia",
            f"Entra con un personaje del mismo mundo donde capturaste las IPs "
            f"({', '.join(captured)}).", GOOD)
