"""antilag panel in Flet: a four-step wizard.

1. World: with Tibia inside the world, capture the game server's IPs. Each
   world has its own and they change (probably with the server save), so they
   are kept in memory only: they are forgotten when the app closes.
2. Server: the access code (VPS, player and secret) and the mode. This one is
   remembered.
3. Links: PING/PONG to the VPS over every internet connection. Two are
   needed: with a single one there is nowhere to switch to.
4. Game: the tunnel carries only the traffic to the world's IPs; Tibia's login
   and website go out with the home IP (CipSoft rejects datacenter IPs). Warns
   if Tibia connects outside the tunnel.

Closing the window brings the tunnel down before exiting: otherwise the routes
would be left pointing at an adapter that no longer exists.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import sys

import flet as ft

from .. import config as store
from ..session import SessionError, TunnelConfig, routes_for
from . import viewmodel as vm
from .backend import DemoBackend, RealBackend

C = {
    "bg": "#0E1014",
    "surface": "#161A21",
    "surface2": "#1D222B",
    "border": "#272D38",
    "text": "#E7EAF0",
    "muted": "#8A93A6",
    "accent": "#5B8CFF",
    "good": "#3FB950",
    "warn": "#D29922",
    "bad": "#F85149",
    "idle": "#3A404D",
}
TONE = {vm.GOOD: C["good"], vm.WARN: C["warn"], vm.BAD: C["bad"], vm.IDLE: C["idle"],
        vm.ACCENT: C["accent"]}

BAR_GAP = 1
SPARK_H = 56
AUTO = "__auto__"
STEPS = (("mundo", "Mundo"), ("servidor", "Servidor"), ("enlaces", "Enlaces"), ("juego", "Jugar"))


async def _maybe_await(value):
    if inspect.isawaitable(value):
        return await value
    return value


def card(content: ft.Control, padding: int = 16, expand=None) -> ft.Container:
    return ft.Container(
        content=content,
        padding=ft.Padding.all(padding),
        bgcolor=C["surface"],
        border=ft.Border.all(1, C["border"]),
        border_radius=ft.BorderRadius.all(12),
        expand=expand,
    )


def pill(text: str, tone: str) -> ft.Container:
    color = TONE[tone]
    return ft.Container(
        content=ft.Text(text, size=11, weight=ft.FontWeight.W_700, color=color),
        padding=ft.Padding.symmetric(horizontal=10, vertical=4),
        bgcolor=ft.Colors.with_opacity(0.14, color),
        border_radius=ft.BorderRadius.all(999),
    )


def label(text: str) -> ft.Text:
    return ft.Text(text.upper(), size=10, weight=ft.FontWeight.W_600, color=C["muted"])


class LinkCard:
    """A link's card; created once and updated in place."""

    def __init__(self, name: str, kind: str) -> None:
        self.name = name
        self.title = ft.Text(name, size=16, weight=ft.FontWeight.W_600, color=C["text"])
        self.kind = ft.Text(kind, size=12, color=C["muted"])
        self.chip = ft.Container(content=pill("...", vm.IDLE))
        self.rtt = ft.Text("--", size=34, weight=ft.FontWeight.W_700, color=C["text"])
        self.stats = ft.Text("", size=12, color=C["muted"])
        self.detail = ft.Text("", size=12, color=C["muted"])
        self.win = ft.Text("", size=12, color=C["muted"])
        # No fixed width: the bars share the card's width, so the full history
        # fits at any window size.
        self.bars = [
            ft.Container(expand=True, height=0, bgcolor=C["idle"],
                         border_radius=ft.BorderRadius.all(1))
            for _ in range(vm.HISTORY)
        ]
        threshold_y = SPARK_H * (1 - vm.BAD_MS / 600.0)
        spark = ft.Stack(
            controls=[
                ft.Row(self.bars, spacing=BAR_GAP, height=SPARK_H,
                       vertical_alignment=ft.CrossAxisAlignment.END),
                # The 300ms line: whatever is above it is unplayable.
                ft.Container(height=1, top=threshold_y, left=0, right=0,
                             bgcolor=ft.Colors.with_opacity(0.45, C["bad"])),
            ],
            height=SPARK_H,
        )
        self.control = card(
            ft.Column(
                [
                    ft.Row([ft.Column([self.title, self.kind], spacing=0), self.chip],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                           vertical_alignment=ft.CrossAxisAlignment.START),
                    ft.Row([self.rtt, ft.Text("ms", size=14, color=C["muted"])],
                           spacing=4, vertical_alignment=ft.CrossAxisAlignment.END),
                    self.stats,
                    spark,
                    ft.Row([self.detail, self.win],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                ],
                spacing=8,
            ),
        )
        self.control.col = {"xs": 12, "md": 6}

    def update(self, row: dict, state: vm.LinkState, share: float, history: vm.RttHistory) -> None:
        self.chip.content = pill(state.label, state.tone)
        rtt = row["last_rtt_ms"] if row["alive"] else None
        self.rtt.value = vm.fmt_ms(rtt)
        self.rtt.color = TONE[vm.rtt_tone(rtt)] if row["alive"] else C["bad"]
        self.stats.value = (
            f"media {vm.fmt_ms(row['rtt_ms'])} ms · p95 {vm.fmt_ms(row['p95_ms'])} ms · "
            f"jitter {vm.fmt_ms(row['jitter_ms'])} ms · pérdida {row['loss_pct']:.0f}%"
        )
        self.detail.value = state.detail
        self.win.value = f"llega primero {share * 100:.0f}%"
        for bar, (height, tone) in zip(self.bars, history.bars(self.name, SPARK_H)):
            bar.height = height
            bar.bgcolor = TONE[tone] if height else C["idle"]


def primary_button(text: str, icon, on_click, disabled: bool = False) -> ft.FilledButton:
    return ft.FilledButton(text, icon=icon, on_click=on_click, disabled=disabled)


def notice(title: str, detail: str, tone: str, icon) -> ft.Container:
    color = TONE[tone]
    return ft.Container(
        content=ft.Row(
            [ft.Icon(icon, color=color, size=22),
             ft.Column([ft.Text(title, size=14, weight=ft.FontWeight.W_600, color=C["text"]),
                        ft.Text(detail, size=13, color=C["muted"])],
                       spacing=2, expand=True)],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.START,
        ),
        padding=ft.Padding.all(14),
        bgcolor=ft.Colors.with_opacity(0.10, color),
        border=ft.Border.all(1, ft.Colors.with_opacity(0.35, color)),
        border_radius=ft.BorderRadius.all(10),
    )


def step_text(number: int, text: str) -> ft.Row:
    return ft.Row(
        [ft.Container(content=ft.Text(str(number), size=12, weight=ft.FontWeight.W_700,
                                      color=C["accent"]),
                      width=22, height=22, alignment=ft.Alignment.CENTER,
                      bgcolor=ft.Colors.with_opacity(0.15, C["accent"]),
                      border_radius=ft.BorderRadius.all(11)),
         ft.Text(text, size=14, color=C["text"], expand=True)],
        spacing=10, vertical_alignment=ft.CrossAxisAlignment.START,
    )


class Panel:
    def __init__(self, page: ft.Page, demo: bool = False) -> None:
        self.page = page
        self.backend = DemoBackend() if demo else RealBackend()
        self.demo = demo
        self.saved = store.load()
        #: the world's IPs: in memory only, forgotten when the app closes
        self.game_ips: list[str] = []
        self.session = None
        self.config: TunnelConfig | None = None
        self.checks = []
        self.step = "mundo"
        self.busy = False
        self.history = vm.RttHistory()
        self.cards: dict[str, LinkCard] = {}

    # -- scaffolding ------------------------------------------------------ #

    def build(self) -> None:
        page = self.page
        page.title = "antilag" + (" (demo)" if self.demo else "")
        page.theme_mode = ft.ThemeMode.DARK
        page.theme = ft.Theme(color_scheme_seed=C["accent"])
        page.dark_theme = ft.Theme(color_scheme_seed=C["accent"])
        page.bgcolor = C["bg"]
        page.padding = ft.Padding.all(20)
        page.scroll = ft.ScrollMode.AUTO
        page.window.width = 1000
        page.window.height = 860
        page.window.min_width = 600
        page.window.min_height = 560
        page.window.prevent_close = True
        page.window.on_event = self.on_window_event

        self.status = ft.Container(content=pill("DESCONECTADO", vm.IDLE))
        self.summary = ft.Text("", size=13, color=C["muted"])
        self.disconnect_btn = ft.FilledButton("Desconectar", icon=ft.Icons.STOP_ROUNDED,
                                              bgcolor=C["bad"], visible=False,
                                              on_click=self.on_disconnect)
        header = ft.Row(
            [ft.Text("antilag", size=24, weight=ft.FontWeight.W_700, color=C["text"]),
             self.status, ft.Container(expand=True), self.summary, self.disconnect_btn],
            spacing=12, vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.stepper = ft.Row(spacing=8, wrap=True)
        self.error = ft.Text("", color=C["bad"], size=13, visible=False, selectable=True)
        self.body = ft.Column(spacing=16, horizontal_alignment=ft.CrossAxisAlignment.STRETCH)
        page.add(ft.Column([header, self.stepper, self.error, self.body], spacing=16,
                           horizontal_alignment=ft.CrossAxisAlignment.STRETCH))
        self.show("mundo")

    def render_stepper(self) -> None:
        order = [key for key, _ in STEPS]
        current = order.index(self.step)
        items = []
        for i, (key, text) in enumerate(STEPS):
            done, active = i < current, i == current
            color = C["good"] if done else C["accent"] if active else C["muted"]
            items.append(ft.Container(
                content=ft.Row(
                    [ft.Icon(ft.Icons.CHECK_ROUNDED, size=14, color=color) if done
                     else ft.Text(str(i + 1), size=12, weight=ft.FontWeight.W_700, color=color),
                     ft.Text(text, size=13, color=C["text"] if active else C["muted"],
                             weight=ft.FontWeight.W_600 if active else None)],
                    spacing=6, tight=True),
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                bgcolor=ft.Colors.with_opacity(0.14, color) if active else None,
                border=ft.Border.all(1, ft.Colors.with_opacity(0.35, color)),
                border_radius=ft.BorderRadius.all(999),
            ))
            if i < len(STEPS) - 1:
                items.append(ft.Text("›", color=C["muted"]))
        self.stepper.controls = items

    def show(self, step: str) -> None:
        self.step = step
        self._show_error(None)
        self.render_stepper()
        builder = {"mundo": self.view_world, "servidor": self.view_server,
                   "enlaces": self.view_links, "juego": self.view_game}[step]
        self.body.controls = builder()
        self.page.update()

    def _show_error(self, message: str | None) -> None:
        self.error.value = message or ""
        self.error.visible = bool(message)

    # -- step 1: world ---------------------------------------------------- #

    def admin_gate(self) -> ft.Control:
        """Step 1 blocking notice: without administrator rights it cannot connect.

        It comes before the capture on purpose: the IPs live in memory, and
        reopening the panel elevated after capturing them would force entering
        the world again.
        """
        detail = ("antilag instala un adaptador de red y cambia rutas, y para eso Windows "
                  "exige permisos de administrador. Reábrelo así antes de capturar las IPs.")
        actions: list[ft.Control] = [ft.Container(expand=True)]
        if sys.platform == "win32":
            actions.append(primary_button("Reabrir como administrador",
                                          ft.Icons.ADMIN_PANEL_SETTINGS, self.on_elevate))
        else:
            detail = "Hace falta correr el panel como root (sudo) para crear el túnel."
        return card(ft.Column([
            notice("Hace falta abrir antilag como administrador", detail, vm.WARN,
                   ft.Icons.ADMIN_PANEL_SETTINGS),
            ft.Row(actions),
        ], spacing=14))

    def view_world(self) -> list[ft.Control]:
        if not self.backend.is_admin():
            return [self.admin_gate()]
        if self.game_ips:
            return [card(ft.Column([
                label("IPs del mundo"),
                ft.Row([pill(ip, vm.GOOD) for ip in self.game_ips], spacing=8, wrap=True),
                ft.Text("Estas IPs son del mundo donde estabas. Si vas a jugar con un personaje "
                        "de otro mundo, vuelve a capturar.", size=13, color=C["muted"]),
                notice("Antes de seguir, sal del mundo",
                       "Vuelve a la lista de personajes o cierra Tibia. La conexión abierta "
                       "no puede pasarse al túnel: hay que entrar de nuevo con el túnel arriba.",
                       vm.WARN, ft.Icons.LOGOUT_ROUNDED),
                ft.Row([ft.OutlinedButton("Volver a capturar", icon=ft.Icons.REFRESH_ROUNDED,
                                          on_click=self.on_capture),
                        ft.Container(expand=True),
                        primary_button("Siguiente", ft.Icons.ARROW_FORWARD_ROUNDED,
                                       self.on_world_next)]),
            ], spacing=14))]
        return [card(ft.Column([
            label("Capturar las IPs del mundo"),
            ft.Text("Cada mundo de Tibia tiene sus propias IPs y cambian con el tiempo. "
                    "antilag las toma de la conexión del juego, así que necesita que estés "
                    "dentro del mundo una vez.", size=13, color=C["muted"]),
            step_text(1, "Abre Tibia y entra al mundo con el personaje que vas a jugar."),
            step_text(2, "Con el personaje dentro, pulsa «Capturar IPs»."),
            step_text(3, "Sal del mundo y sigue con el asistente."),
            ft.Row([ft.Container(expand=True),
                    primary_button("Capturar IPs", ft.Icons.RADAR_ROUNDED, self.on_capture,
                                   disabled=self.busy)]),
        ], spacing=14))]

    async def on_capture(self, e=None) -> None:
        if self.busy:
            return
        self.busy = True
        self._show_error(None)
        self.body.controls = [card(ft.Row([ft.ProgressRing(width=18, height=18, stroke_width=2),
                                           ft.Text("Buscando las conexiones del juego...",
                                                   color=C["muted"])], spacing=12))]
        self.page.update()
        try:
            ips = await asyncio.to_thread(self.backend.capture)
        except SessionError as exc:
            ips = []
            self._show_error(str(exc))
        finally:
            self.busy = False
        if ips:
            self.game_ips = ips
        elif not self.error.visible:
            self._show_error("No se encontró Tibia conectado a un mundo. Entra con tu "
                             "personaje y vuelve a intentar.")
        message = self.error.value if self.error.visible else None
        self.show("mundo")
        if message:
            self._show_error(message)
            self.page.update()

    async def on_world_next(self, e=None) -> None:
        if await asyncio.to_thread(self.backend.in_world):
            self._show_error("Tibia sigue conectado al mundo. Sal a la lista de personajes "
                             "(o cierra Tibia) y pulsa Siguiente de nuevo.")
            self.page.update()
            return
        self.show("servidor")

    # -- step 2: server --------------------------------------------------- #

    def view_server(self) -> list[ft.Control]:
        saved = self.saved
        #: who to connect as: filled by the access code (or by manual entry)
        self.player = {
            "server": saved.server or ("demo:51888" if self.demo else ""),
            "secret": saved.secret or ("demo" if self.demo else ""),
            "client_id": saved.client_id,
            "address": saved.address,
            "name": saved.name or ("demo" if self.demo else ""),
        }
        self.code = ft.TextField(label="Código de acceso", hint_text="antilag1:...",
                                 password=True, can_reveal_password=True, dense=True,
                                 on_change=self.on_code)
        self.code_status = ft.Text("", size=12, color=C["muted"])
        self.player_summary = ft.Container()
        self.server = ft.TextField(label="Servidor (VPS)", hint_text="ip-del-vps:51888",
                                   value=self.player["server"], dense=True, expand=True,
                                   on_change=self.on_manual)
        self.secret = ft.TextField(label="Secreto", password=True, can_reveal_password=True,
                                   value=self.player["secret"], dense=True, expand=True,
                                   on_change=self.on_manual)
        self.manual = ft.Column([
            ft.Row([self.server, self.secret], spacing=12),
            ft.Text("Sin código de acceso se entra como el jugador 1 (el principal del VPS).",
                    size=12, color=C["muted"]),
        ], spacing=8, visible=False)
        manual_toggle = ft.TextButton("Configurar a mano", icon=ft.Icons.TUNE_ROUNDED,
                                      on_click=self.on_toggle_manual)
        self.mode = self.saved.mode if self.saved.mode in vm.MODE_LABELS else "duplicate"
        self.mode_cards = ft.ResponsiveRow(spacing=12, run_spacing=12)
        self.render_modes()
        links = []
        try:
            links = [link.name for link in self.backend.links()]
        except Exception:  # noqa: BLE001
            pass
        self.primary = ft.Dropdown(
            label="Enlace preferido", dense=True, width=280,
            value=self.saved.primary if self.saved.primary in links else AUTO,
            options=[ft.DropdownOption(key=AUTO, text="Automático")]
            + [ft.DropdownOption(key=name, text=name) for name in links],
        )
        self.server_next = primary_button("Siguiente", ft.Icons.ARROW_FORWARD_ROUNDED,
                                          self.on_server_next)
        self.refresh_player()
        return [
            card(ft.Column([label("Acceso al VPS"),
                            self.player_summary,
                            self.code, self.code_status,
                            ft.Text("Pega el código que te dio quien administra el VPS "
                                    "('antilag clients add'). Se guarda en este PC, cifrado con "
                                    "tu usuario de Windows.", size=12, color=C["muted"]),
                            manual_toggle, self.manual], spacing=10)),
            card(ft.Column([label("Tipo de conexión"), self.mode_cards,
                            ft.Row([self.primary,
                                    ft.Text("El preferido lleva lo que no se duplica. Conviene "
                                            "el que no gasta datos móviles.", size=12,
                                            color=C["muted"], expand=True)],
                                   spacing=12)], spacing=12)),
            ft.Row([ft.OutlinedButton("Atrás", icon=ft.Icons.ARROW_BACK_ROUNDED,
                                      on_click=lambda e: self.show("mundo")),
                    ft.Container(expand=True), self.server_next]),
        ]

    def render_modes(self) -> None:
        cards = []
        for key, title in vm.MODE_LABELS.items():
            selected = key == self.mode
            color = C["accent"] if selected else C["border"]
            box = ft.Container(
                content=ft.Column([
                    ft.Row([ft.Text(title, size=15, weight=ft.FontWeight.W_600, color=C["text"]),
                            ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=C["accent"], size=18,
                                    visible=selected)],
                           alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
                    ft.Text(vm.MODE_HELP[key], size=12, color=C["text"]),
                    ft.Text(vm.MODE_COST[key], size=11, color=C["muted"]),
                ], spacing=6),
                padding=ft.Padding.all(14),
                bgcolor=ft.Colors.with_opacity(0.10, C["accent"]) if selected else C["surface2"],
                border=ft.Border.all(2 if selected else 1, color),
                border_radius=ft.BorderRadius.all(10),
                on_click=lambda e, k=key: self.on_mode(k),
                ink=True,
            )
            box.col = {"xs": 12, "md": 4}
            cards.append(box)
        self.mode_cards.controls = cards

    def on_mode(self, key: str) -> None:
        self.mode = key
        self.render_modes()
        self.page.update()

    def refresh_player(self) -> None:
        player = self.player
        ready = bool(player["server"].strip()) and bool(player["secret"].strip())
        self.server_next.disabled = not ready
        if ready:
            who = player["name"] or f"jugador {player['client_id']}"
            ip = player["address"].split("/", 1)[0]
            self.player_summary.content = notice(
                f"Conectará como {who}",
                f"IP {ip} dentro del túnel, servidor {player['server']}.",
                vm.GOOD, ft.Icons.VERIFIED_USER_ROUNDED)
        else:
            self.player_summary.content = None

    def on_code(self, e=None) -> None:
        from ..registry import RegistryError, parse_access_code

        text = (self.code.value or "").strip()
        if not text:
            self.code_status.value = ""
        else:
            try:
                access = parse_access_code(text)
            except RegistryError as exc:
                self.code_status.value = str(exc)
                self.code_status.color = C["bad"]
            else:
                self.player = {"server": access.endpoint, "secret": access.secret,
                               "client_id": access.client_id, "address": access.address,
                               "name": access.name}
                self.server.value, self.secret.value = access.endpoint, access.secret
                self.code_status.value = "Código válido."
                self.code_status.color = C["good"]
        self.refresh_player()
        self.page.update()

    def on_manual(self, e=None) -> None:
        # Manual entry has no id or IP: it is player 1, as before access codes.
        self.player = {"server": (self.server.value or "").strip(),
                       "secret": (self.secret.value or "").strip(),
                       "client_id": 1, "address": "10.66.0.2/24", "name": ""}
        self.refresh_player()
        self.page.update()

    def on_toggle_manual(self, e=None) -> None:
        self.manual.visible = not self.manual.visible
        self.page.update()

    def _build_config(self) -> TunnelConfig:
        primary = self.primary.value if self.primary.value not in (None, AUTO) else None
        player = self.player
        return TunnelConfig(
            server=player["server"].strip(),
            secret=player["secret"].strip(),
            client_id=player["client_id"],
            address=player["address"],
            name=player["name"],
            mode=self.mode,
            primary=primary,
            routes=routes_for(self.game_ips),
        )

    async def on_server_next(self, e=None) -> None:
        self.config = self._build_config()
        self.saved = store.SavedConfig(server=self.config.server, mode=self.config.mode,
                                       primary=self.config.primary, secret=self.config.secret,
                                       client_id=self.config.client_id,
                                       address=self.config.address, name=self.config.name)
        if not self.demo:
            store.save(self.saved)
        self.show("enlaces")
        await self.run_probe()

    # -- step 3: links ---------------------------------------------------- #

    def view_links(self) -> list[ft.Control]:
        rows = []
        for check in self.checks:
            if check.ok:
                icon = ft.Icon(ft.Icons.CHECK_CIRCLE_ROUNDED, color=C["good"], size=20)
                detail = f"{vm.fmt_ms(check.rtt_ms)} ms hasta el VPS"
            else:
                icon = ft.Icon(ft.Icons.ERROR_OUTLINE_ROUNDED, color=C["bad"], size=20)
                detail = check.error
            rows.append(ft.Row([
                icon,
                ft.Column([ft.Text(check.link.name, size=14, weight=ft.FontWeight.W_600,
                                   color=C["text"]),
                           ft.Text(f"{check.link.kind} · {check.link.address}", size=12,
                                   color=C["muted"])], spacing=0, expand=True),
                ft.Text(detail, size=13, color=C["text"] if check.ok else C["bad"]),
            ], spacing=12))
        if self.busy:
            rows = [ft.Row([ft.ProgressRing(width=18, height=18, stroke_width=2),
                            ft.Text("Probando cada salida a internet contra el VPS...",
                                    color=C["muted"])], spacing=12)]

        good = sum(1 for check in self.checks if check.ok)
        admin = self.backend.is_admin()
        can_connect = not self.busy and good >= vm.MIN_LINKS and admin
        notes: list[ft.Control] = []
        if not self.busy and self.checks:
            if good == 0:
                notes.append(notice("Ningún enlace llegó al VPS",
                                    "Revisa la IP, el puerto y el secreto (Atrás), que el "
                                    "servicio esté corriendo y que el puerto UDP esté abierto "
                                    "en el firewall del proveedor.", vm.BAD,
                                    ft.Icons.ERROR_OUTLINE_ROUNDED))
            elif good < vm.MIN_LINKS:
                notes.append(notice(
                    f"Hace falta al menos {vm.MIN_LINKS} salidas a internet",
                    "antilag protege la partida pasando el juego por otra salida cuando una "
                    "falla; con una sola no hay a dónde pasarlo. Conecta la Wi-Fi o un teléfono "
                    "por USB y pulsa Reintentar.", vm.WARN, ft.Icons.WARNING_AMBER_ROUNDED))
            else:
                notes.append(notice(f"{good} salidas listas",
                                    "Si después conectas otra (otro teléfono por USB), antilag "
                                    "la suma sola.", vm.GOOD, ft.Icons.CHECK_CIRCLE_ROUNDED))
        if not admin:
            actions = [ft.Text("Conectar instala un adaptador de red y cambia rutas: hace falta "
                               "abrir antilag como administrador.", size=13, color=C["text"],
                               expand=True)]
            if sys.platform == "win32":
                actions.append(ft.Button("Reabrir como administrador",
                                         icon=ft.Icons.ADMIN_PANEL_SETTINGS,
                                         on_click=self.on_elevate))
            notes.append(ft.Container(
                content=ft.Row(actions, spacing=12,
                               vertical_alignment=ft.CrossAxisAlignment.CENTER),
                padding=ft.Padding.all(12),
                bgcolor=ft.Colors.with_opacity(0.12, C["warn"]),
                border=ft.Border.all(1, ft.Colors.with_opacity(0.4, C["warn"])),
                border_radius=ft.BorderRadius.all(10),
            ))
        return [
            card(ft.Column([label("Salidas a internet"), *rows], spacing=12)),
            *notes,
            ft.Row([ft.OutlinedButton("Atrás", icon=ft.Icons.ARROW_BACK_ROUNDED,
                                      on_click=lambda e: self.show("servidor"),
                                      disabled=self.busy),
                    ft.Container(expand=True),
                    ft.OutlinedButton("Reintentar", icon=ft.Icons.REFRESH_ROUNDED,
                                      on_click=self.run_probe, disabled=self.busy),
                    primary_button("Conectar", ft.Icons.PLAY_ARROW_ROUNDED, self.on_connect,
                                   disabled=not can_connect)]),
        ]

    async def run_probe(self, e=None) -> None:
        if self.busy or self.config is None:
            return
        self.busy = True
        self.show("enlaces")
        error = None
        try:
            self.checks = await asyncio.to_thread(self.backend.probe, self.config)
        except SessionError as exc:
            self.checks, error = [], str(exc)
        finally:
            self.busy = False
        self.show("enlaces")
        if error:
            self._show_error(error)
            self.page.update()

    # -- step 4: game ----------------------------------------------------- #

    async def on_connect(self, e=None) -> None:
        if self.busy or self.config is None:
            return
        self.busy = True
        self.status.content = pill("CONECTANDO", vm.ACCENT)
        self.page.update()
        session = self.backend.session(self.config)
        try:
            await asyncio.to_thread(session.start)
        except Exception as exc:  # noqa: BLE001 - shown, not swallowed
            self.busy = False
            self.status.content = pill("DESCONECTADO", vm.IDLE)
            self.show("enlaces")
            self._show_error(str(exc) if isinstance(exc, SessionError)
                             else f"{type(exc).__name__}: {exc}")
            self.page.update()
            return
        self.busy = False
        self.session = session
        self.history = vm.RttHistory()
        self.cards = {}
        self.status.content = pill("CONECTADO", vm.GOOD)
        who = f"{self.config.name} · " if self.config.name else ""
        self.summary.value = f"{who}{self.config.server} · {vm.MODE_LABELS[self.config.mode]}"
        self.disconnect_btn.visible = True
        self.show("juego")

    def view_game(self) -> list[ft.Control]:
        self.game_notice = ft.Container(content=notice(*vm.game_banner(None, self.game_ips)[:2],
                                                       vm.GOOD, ft.Icons.SPORTS_ESPORTS_ROUNDED))
        self.links_row = ft.ResponsiveRow(spacing=12, run_spacing=12)
        self.tiles: dict[str, ft.Text] = {}
        tiles = []
        for key, text in (("up", "Paquetes subida"), ("down", "Paquetes bajada"),
                          ("dup", "Juego duplicado"), ("discarded", "Copias descartadas"),
                          ("emergency", "Emergencias")):
            value = ft.Text("--", size=22, weight=ft.FontWeight.W_700, color=C["text"])
            self.tiles[key] = value
            tile = card(ft.Column([label(text), value], spacing=4), padding=14)
            tile.col = {"xs": 6, "sm": 4, "md": 12 / 5}
            tiles.append(tile)
        self.reason = ft.Text("", size=13, color=C["muted"])
        self.events = ft.Column(spacing=6)
        return [
            self.game_notice,
            self.links_row,
            ft.ResponsiveRow(tiles, spacing=12, run_spacing=12),
            card(ft.Column([label("Conmutaciones"), self.reason, self.events], spacing=10)),
        ]

    async def on_disconnect(self, e=None) -> None:
        if self.busy:
            return
        session, self.session = self.session, None
        self.busy = True
        self.status.content = pill("DESCONECTANDO", vm.ACCENT)
        self.disconnect_btn.disabled = True
        self.page.update()
        try:
            if session is not None:
                await asyncio.to_thread(session.stop)
        finally:
            self.busy = False
            self.status.content = pill("DESCONECTADO", vm.IDLE)
            self.summary.value = ""
            self.disconnect_btn.visible = False
            self.disconnect_btn.disabled = False
            self.show("mundo")

    async def on_elevate(self, e=None) -> None:
        import ctypes

        if getattr(sys, "frozen", False):
            # Packaged executable: it relaunches itself, without arguments.
            params, root = "", os.path.dirname(sys.executable)
        else:
            params = "-m antilag panel"
            root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        # ShellExecute with "runas" shows the UAC dialog; if accepted, this
        # unprivileged panel is no longer needed.
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable,
                                                     params, root, 1)
        if result > 32:
            await _maybe_await(self.page.window.destroy())

    async def on_window_event(self, e) -> None:
        if e.type == ft.WindowEventType.CLOSE:
            if self.session is not None:
                await asyncio.to_thread(self.session.stop)
                self.session = None
            await _maybe_await(self.page.window.destroy())

    # -- refresh ---------------------------------------------------------- #

    async def refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            session = self.session
            if self.step != "juego" or session is None or not session.running:
                continue
            snap = session.snapshot()
            if snap is None:
                continue
            self.render(snap)
            self.page.update()

    def render(self, snap: dict) -> None:
        title, detail, tone = vm.game_banner(snap.get("game"), self.game_ips)
        icon = ft.Icons.WARNING_AMBER_ROUNDED if tone == vm.BAD else ft.Icons.SPORTS_ESPORTS_ROUNDED
        self.game_notice.content = notice(title, detail, tone, icon)

        rows = snap["links"]
        self.history.push(rows)
        shares = vm.win_shares(rows)
        for row in rows:
            link_card = self.cards.get(row["name"])
            if link_card is None:
                link_card = self.cards[row["name"]] = LinkCard(
                    row["name"], f"{row['kind']} · {row['address']}")
                self.links_row.controls.append(link_card.control)
            link_card.kind.value = f"{row['kind']} · {row['address']}"
            state = vm.link_state(row, snap["senders"], snap.get("suspect", []))
            link_card.update(row, state, shares.get(row["name"], 0.0), self.history)

        text, tone = vm.overall_status(rows, snap.get("suspect", []))
        if (snap.get("game") or {}).get("outside"):
            text, tone = "FUERA DEL TÚNEL", vm.BAD
        self.status.content = pill(text, tone)

        self.tiles["up"].value = vm.fmt_count(snap["up_packets"])
        self.tiles["down"].value = vm.fmt_count(snap["down_packets"])
        self.tiles["dup"].value = vm.fmt_count(snap["up_duplicated"])
        self.tiles["discarded"].value = vm.fmt_count(snap["codec"]["duplicates"])
        self.tiles["emergency"].value = vm.fmt_count(snap.get("emergency_packets", 0))
        self.reason.value = f"Ahora: {snap['reason']}"
        self.events.controls = [
            ft.Row([ft.Text(at, size=12, color=C["muted"], width=64),
                    ft.Text(reason, size=12, color=C["text"], expand=True)], spacing=8)
            for at, reason in vm.event_lines(snap.get("events", []))
        ] or [ft.Text("Sin conmutaciones todavía.", size=12, color=C["muted"])]


def run(demo: bool = False, screenshot: str | None = None, after: float = 8.0) -> None:
    async def main(page: ft.Page) -> None:
        if screenshot:
            page.enable_screenshots = True
        panel = Panel(page, demo=demo)
        panel.build()
        page.update()
        asyncio.create_task(panel.refresh_loop())
        if screenshot:
            await _capture(page, panel, screenshot, after)

    ft.run(main)


async def _capture(page: ft.Page, panel: Panel, path: str, after: float) -> None:
    """Only for verifying the panel automatically: walks the wizard, saves one
    screenshot per step and ends the process, no matter what."""
    import traceback

    base, ext = os.path.splitext(path)

    async def shot(name: str) -> None:
        await asyncio.sleep(1.0)
        with open(f"{base}-{name}{ext}", "wb") as handle:
            handle.write(await page.take_screenshot() or b"")

    try:
        await shot("1-mundo")
        await panel.on_capture()
        await shot("1-mundo-capturado")
        if panel.game_ips:
            await panel.on_world_next()
            if panel.demo:
                # As if the friend pasted their access code.
                from ..registry import Registry, make_access_code

                registry = Registry(endpoint="demo:51888")
                registry.add("principal")
                panel.code.value = make_access_code(registry, registry.add("amigo1"))
                panel.on_code()
            await shot("2-servidor")
            if not panel.server_next.disabled:
                await panel.on_server_next()
                await shot("3-enlaces")
                if panel.session is None and panel.backend.is_admin():
                    await panel.on_connect()
                    await asyncio.sleep(after)
                    await shot("4-juego")
                    await panel.on_disconnect()
    except Exception:  # noqa: BLE001
        with open(path + ".error.txt", "w", encoding="utf-8") as handle:
            traceback.print_exc(file=handle)
    finally:
        try:
            if panel.session is not None:
                await asyncio.to_thread(panel.session.stop)
            await asyncio.wait_for(_maybe_await(page.window.destroy()), 3)
        finally:
            os._exit(0)
