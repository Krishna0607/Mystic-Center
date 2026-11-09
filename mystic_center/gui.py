#!/usr/bin/env python3
"""Mystic Center — GTK4/libadwaita unified control app for MSI laptops.

Pages: Overview · Performance · Fans · Battery · Lighting · Settings.
Talks to the privileged mysticd daemon over a Unix socket for everything
that needs root; the Lighting page talks to the keyboard directly via
the user-accessible hidraw node.
"""

from __future__ import annotations

import os
import shutil
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import client as daemon_client
from . import theme
from . import wmi2
from .lighting import profiles as prof
from .lighting import effects
from .lighting.effects import gradient, hex_to_rgb, rgb_to_hex
from .lighting.hid import DeviceNotFound, find_hidraw
from .lighting.keyboard import NUM_ZONES, MysticKeyboard
from .lighting.keyboard_view import KeyboardView
from .lighting.profiles import GAMING_BASE, GAMING_RED, GAMING_RED_ZONES

APP_ID = "org.mysticcenter.Center"
APP_NAME = "Mystic Center"
REFRESH_MS = 1000

SWATCH_ROWS = [
    [("Amber", "FF7F20"), ("Coral", "FF5030"), ("Crimson", "C41E3A")],
    [("Magenta", "FF2AA0"), ("Violet", "8A40FF"), ("Navy", "1030C0")],
    [("Blue", "0070FF"), ("Cyan", "00C8FF"), ("Teal", "00A896")],
    [("Green", "00DD44"), ("Lime", "9FE600"), ("White", "F0F0F0")],
    [("Sunset", "FF9E00"), ("Matrix", "00FE55"), ("Dim", "2A2A2A")],
]

BASE_CSS = """.zone-btn { min-width: 46px; min-height: 34px; font-size: 9px;
    border-radius: 9px; border: 1px solid rgba(0,0,0,0.35);
    color: rgba(255,255,255,0.0); }
.zone-btn.sel { border: 2px solid #9cc7ff;
    box-shadow: 0 0 0 2px rgba(156,199,255,0.35); }
.swatch { min-width: 44px; min-height: 26px; border-radius: 6px;
    border: 1px solid rgba(0,0,0,0.4); font-size: 8px;
    color: rgba(255,255,255,0.0); }
.mode-btn { min-width: 92px; }
"""

AUTOSTART_DESKTOP = """[Desktop Entry]
Type=Application
Name=Mystic Center (lighting)
Comment=Re-apply Mystic Center keyboard lighting profile at login
Exec={exec_path} lighting apply
Terminal=false
Categories=Utility;
X-GNOME-Autostart-enabled=true
"""


class SegButtons(Gtk.Box):
    """Exclusive toggle-button group styled as a linked segmented control."""

    def __init__(self, options: list[tuple[str, str]], on_change):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.add_css_class("linked")
        self.buttons: dict[str, Gtk.ToggleButton] = {}
        self.on_change = on_change
        self.selected: str | None = None
        for key, label in options:
            btn = Gtk.ToggleButton(label=label)
            btn.connect("toggled", self._toggled, key)
            self.buttons[key] = btn
            self.append(btn)

    def _toggled(self, btn: Gtk.ToggleButton, key: str) -> None:
        if not btn.get_active() or self.selected == key:
            return
        self.selected = key
        for k, other in self.buttons.items():
            if k != key and other.get_active():
                other.handler_block_by_func(self._toggled)
                other.set_active(False)
                other.handler_unblock_by_func(self._toggled)
        self.on_change(key)

    def select(self, key: str) -> None:
        """Programmatic selection (no on_change fired)."""
        if self.selected == key:
            return
        self.selected = key
        for k, btn in self.buttons.items():
            btn.handler_block_by_func(self._toggled)
            btn.set_active(k == key)
            btn.handler_unblock_by_func(self._toggled)


def fmt(v, suffix="", nd=1):
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
        return f"{round(f, nd):g}{suffix}"
    except (TypeError, ValueError):
        return str(v)


SHIFT_LABELS = {
    "eco": "Battery Saver",
    "comfort": "Balanced",
    "turbo": "Performance",
}


def perf_mode_name(mode: str | None) -> str:
    if not mode:
        return "—"
    key = mode.split(" ")[0].lower()
    return SHIFT_LABELS.get(key, mode)


# =============================================================== generic page

class Page(Gtk.ScrolledWindow):
    """Base page: horizontally clamped content + daemon-reachability hook."""

    def __init__(self, window: "CenterWindow", icon: str,
                 title: str = "", subtitle: str = ""):
        super().__init__(vexpand=True)
        self.window = window
        self.icon_name = icon
        self.page_title = title
        self.page_subtitle = subtitle
        self.add_css_class("mc-content")
        self.clamp = Adw.Clamp(maximum_size=1180, tightening_threshold=1000)
        self.set_child(self.clamp)
        self.col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.col.set_margin_top(18)
        self.col.set_margin_bottom(30)
        self.col.set_margin_start(6)
        self.col.set_margin_end(6)
        self.clamp.set_child(self.col)
        if title:
            self.col.append(theme.section(title, subtitle or None))
            self.header = self.col.get_first_child()
        else:
            self.header = None

    def add(self, widget: Gtk.Widget) -> None:
        self.col.append(widget)

    def refresh(self, snap: dict) -> None:  # pragma: no cover - overridden
        ...

    def reachability(self, ok: bool, why: str) -> None:  # pragma: no cover
        ...


# ================================================================ overview

def _mk_row(title: str = "", subtitle: str = "") -> Adw.ActionRow:
    return Adw.ActionRow(title=title, subtitle=subtitle)


def _dmi(name: str) -> str:
    try:
        with open(f"/sys/class/dmi/id/{name}") as fh:
            return fh.read().strip()
    except OSError:
        return ""


MACHINE_NAME = _dmi("product_name") or "MSI Laptop"
MACHINE_BOARD = _dmi("board_name") or ""
MACHINE_BIOS = _dmi("bios_version") or ""


def _temp_tone(value) -> str | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v >= 88:
        return "err"
    if v >= 76:
        return "warn"
    return "ok"


class OverviewPage(Page):
    """Control-center dashboard: live telemetry, quick controls, GPU state."""

    def __init__(self, window):
        super().__init__(
            window, "view-grid-symbolic", "Dashboard",
            "Live telemetry and the controls you reach for most")

        # ---- alerts (hidden unless something needs attention) -------------
        self.alert_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,
                                 spacing=10)
        self.col.append(self.alert_box)

        self.gpu_alert = theme.banner(
            "Restart required", "A graphics-mode change is staged",
            "warn", "system-reboot-symbolic")
        review = Gtk.Button(label="Review")
        review.add_css_class("mc-ghost")
        review.set_valign(Gtk.Align.CENTER)
        review.connect("clicked", lambda *_: self.window.show_page("gpu"))
        self.gpu_alert.append(review)
        self.gpu_alert.set_visible(False)
        self.alert_box.append(self.gpu_alert)

        self.fail_alert = theme.banner(
            "The last graphics-mode switch did not apply",
            "Shut down completely, then power on again",
            "err", "dialog-warning-symbolic")
        fix = Gtk.Button(label="Fix")
        fix.add_css_class("mc-ghost")
        fix.set_valign(Gtk.Align.CENTER)
        fix.connect("clicked", lambda *_: self.window.show_page("gpu"))
        self.fail_alert.append(fix)
        self.fail_alert.set_visible(False)
        self.alert_box.append(self.fail_alert)

        # ---- hero ----------------------------------------------------------
        hero = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=28)
        hero.add_css_class("mc-hero")
        self.col.append(hero)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.set_hexpand(True)
        hero.append(left)

        left.append(theme.label("SYSTEM STATUS", "mc-hero-eyebrow"))
        left.append(theme.label(MACHINE_NAME, "mc-hero-title"))
        ident = " · ".join(x for x in (MACHINE_BOARD, MACHINE_BIOS) if x)
        left.append(theme.label(ident or "MSI laptop", "mc-hero-sub"))

        self.hero_badges = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                   spacing=8)
        self.hero_badges.set_margin_top(4)
        self.badge_perf = theme.Badge("—", "muted")
        self.badge_fan = theme.Badge("fan —", "muted")
        self.badge_gpu = theme.Badge("GPU —", "muted")
        self.badge_saver = theme.Badge("Battery saver", "ok")
        for b in (self.badge_perf, self.badge_fan, self.badge_gpu,
                  self.badge_saver):
            self.hero_badges.append(b)
        self.badge_saver.set_visible(False)
        left.append(self.hero_badges)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        actions.set_margin_top(10)
        perf_btn = Gtk.Button(label="Performance")
        perf_btn.add_css_class("mc-ghost")
        perf_btn.connect("clicked", lambda *_: self.window.show_page("perf"))
        gpu_btn = Gtk.Button(label="Graphics mode")
        gpu_btn.add_css_class("mc-primary")
        gpu_btn.connect("clicked", lambda *_: self.window.show_page("gpu"))
        actions.append(perf_btn)
        actions.append(gpu_btn)
        left.append(actions)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        right.set_size_request(230, -1)
        right.set_valign(Gtk.Align.CENTER)
        hero.append(right)
        right.append(theme.label("BATTERY", "mc-stat-label"))
        self.bat_big = theme.label("—", "mc-hero-big")
        right.append(self.bat_big)
        self.bat_meter = Gtk.LevelBar(min_value=0.0, max_value=1.0)
        self.bat_meter.add_css_class("mc-meter")
        self.bat_meter.set_size_request(-1, 8)
        right.append(self.bat_meter)
        self.bat_caption = theme.label("—", "mc-hero-sub")
        right.append(self.bat_caption)

        # ---- telemetry tiles ----------------------------------------------
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=14,
                        row_spacing=14)
        self.col.append(grid)
        self.tile_cpu = theme.StatTile("weather-clear-symbolic", "CPU")
        self.tile_gpu = theme.StatTile("video-display-symbolic", "GPU")
        self.tile_fan = theme.StatTile("weather-windy-symbolic", "Fans")
        self.tile_ram = theme.StatTile("drive-harddisk-symbolic", "Memory")
        for i, tile in enumerate((self.tile_cpu, self.tile_gpu,
                                  self.tile_fan, self.tile_ram)):
            grid.attach(tile, i, 0, 1, 1)

        # ---- quick controls ------------------------------------------------
        quick = theme.card(
            "Quick controls", "One tap, straight to the embedded controller",
            "preferences-system-symbolic")
        self.col.append(quick)
        body = quick.body

        perf_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        perf_row.append(theme.icon("power-profile-performance-symbolic", 18,
                                   "mc-card-icon"))
        perf_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        perf_text.append(theme.label("Platform profile", "mc-card-title"))
        perf_text.append(theme.label("Eco · Balanced · Performance",
                                     "mc-card-sub"))
        perf_row.append(perf_text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        perf_row.append(spacer)
        self.perf_btns = SegButtons(
            [("eco", "Eco"), ("comfort", "Balanced"), ("turbo", "Turbo")],
            self.on_perf)
        self.perf_btns.add_css_class("mc-seg")
        self.perf_btns.set_valign(Gtk.Align.CENTER)
        perf_row.append(self.perf_btns)
        body.append(perf_row)
        body.append(self._sep())

        self.sw_cooler = self._switch_row(
            body, "power-profile-performance-symbolic", "Cooler Boost",
            "Both fans to 100 % for the fastest heat dump", self.on_cooler)
        body.append(self._sep())
        self.sw_saver = self._switch_row(
            body, "night-light-symbolic", "Super Battery Saver",
            "Eco profile · silent fans · CPU capped at 2.0 GHz", self.on_saver)
        body.append(self._sep())
        self.sw_webcam = self._switch_row(
            body, "camera-web-symbolic", "Webcam",
            "Hardware kill switch for the camera", self.on_webcam)

        # ---- graphics card -------------------------------------------------
        gpu_card = theme.card(
            "Graphics", "Firmware MUX — MSHybrid, Discrete or Integrated",
            "video-display-symbolic")
        self.col.append(gpu_card)
        gbody = gpu_card.body
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.gpu_now = theme.label("—", "mc-card-title")
        row.append(self.gpu_now)
        self.gpu_badge = theme.Badge("—", "muted")
        row.append(self.gpu_badge)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        manage = Gtk.Button(label="Manage")
        manage.add_css_class("mc-ghost")
        manage.set_valign(Gtk.Align.CENTER)
        manage.connect("clicked", lambda *_: self.window.show_page("gpu"))
        row.append(manage)
        gbody.append(row)
        self.gpu_caption = theme.label("", "mc-card-sub", wrap=True)
        gbody.append(self.gpu_caption)

        # ---- system details ------------------------------------------------
        info = theme.card("System", "Firmware and machine identity",
                          "emblem-system-symbolic")
        self.col.append(info)
        self.kv = {}
        for key, val in (("Board", MACHINE_BOARD or "—"),
                         ("BIOS", MACHINE_BIOS or "—"),
                         ("EC firmware", "—"),
                         ("Linux kernel", os.uname().release)):
            row, value = theme.key_value(key, val)
            info.body.append(row)
            self.kv[key] = value

        self._snap = {}

    # ---- small helpers -----------------------------------------------------

    @staticmethod
    def _sep() -> Gtk.Box:
        sep = Gtk.Box()
        sep.add_css_class("mc-sep")
        sep.set_margin_top(2)
        sep.set_margin_bottom(2)
        return sep

    def _switch_row(self, body: Gtk.Box, icon_name: str, title: str,
                    subtitle: str, handler) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(theme.icon(icon_name, 18, "mc-card-icon"))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(theme.label(title, "mc-card-title"))
        text.append(theme.label(subtitle, "mc-card-sub", wrap=True))
        row.append(text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("notify::active", handler)
        row.append(sw)
        body.append(row)
        return sw

    # ---- actions -----------------------------------------------------------

    def on_perf(self, key: str) -> None:
        try:
            with daemon_client.Client() as c:
                c.request("set_shift_mode", mode=key)
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"profile switch failed: {e}")

    def on_cooler(self, sw, *_a):
        self._toggle(sw, "set_cooler_boost", "cooler boost")

    def on_saver(self, sw, *_a):
        self._toggle(sw, "set_super_saver", "super saver")

    def on_webcam(self, sw, *_a):
        self._toggle(sw, "set_webcam", "webcam")

    def _toggle(self, sw, op: str, what: str) -> None:
        if getattr(sw, "_mc_sync", False):
            return
        try:
            with daemon_client.Client() as c:
                c.request(op, on=sw.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"{what} failed: {e}")
            sw._mc_sync = True
            try:
                sw.set_active(not sw.get_active())
            finally:
                sw._mc_sync = False

    # ---- refresh -----------------------------------------------------------

    @staticmethod
    def _sync_switch(sw: Gtk.Switch, value: bool) -> None:
        if bool(value) == sw.get_active():
            return
        sw._mc_sync = True
        try:
            sw.set_active(bool(value))
        finally:
            sw._mc_sync = False

    def refresh(self, snap: dict) -> None:
        self._snap = snap
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        fans = snap.get("fans") or {}
        bat = snap.get("battery") or {}
        system = snap.get("system") or {}
        modes = snap.get("modes") or {}
        gmode = snap.get("gpu_mode") or {}

        shift = (modes.get("shift") or "").split(" ")[0].lower()
        perf_tone = {"eco": "ok", "comfort": "accent",
                     "turbo": "warn"}.get(shift, "muted")
        self.badge_perf.update(perf_mode_name(modes.get("shift")), perf_tone)
        self.badge_fan.update(f"fan {modes.get('fan') or 'auto'}", "muted")
        if gmode.get("available"):
            cur = gmode.get("current") or gmode.get("target")
            self.badge_gpu.update(GPU_MODE_INFO.get(cur, (cur,))[0],
                                  "warn" if gmode.get("restart_required")
                                  else "accent")
        else:
            self.badge_gpu.update("GPU —", "muted")
        self.badge_saver.set_visible(bool(modes.get("super_saver")))
        self.perf_btns.select(shift if shift in self.perf_btns.buttons
                              else "comfort")
        self._sync_switch(self.sw_cooler, bool(modes.get("cooler_boost")))
        self._sync_switch(self.sw_saver, bool(modes.get("super_saver")))
        self._sync_switch(self.sw_webcam, bool(modes.get("webcam")))

        percent = bat.get("percent")
        if percent is not None:
            self.bat_big.set_label(f"{percent}%")
            self.bat_meter.set_value(float(percent) / 100.0)
            status = bat.get("status") or ""
            pw = bat.get("power_w")
            self.bat_caption.set_label(
                f"{status or 'idle'}" + (f" · {fmt(pw, ' W')}"
                                         if pw is not None else ""))
        else:
            self.bat_big.set_label("—")
            self.bat_caption.set_label("no battery node")

        self.tile_cpu.update(
            fmt(cpu.get("temp"), "°C"),
            f"fan {fmt(cpu.get('fan_pct'), '%')}",
            None if cpu.get("temp") is None
            else (float(cpu["temp"]) - 30) / 70,
            _temp_tone(cpu.get("temp")))
        self.tile_gpu.update(
            fmt(gpu.get("temp"), "°C"),
            f"{fmt(gpu.get('util'), '%')} load · {fmt(gpu.get('power_w'), ' W')}",
            None if gpu.get("temp") is None
            else (float(gpu["temp"]) - 30) / 70,
            _temp_tone(gpu.get("temp")))
        rpm = fans.get("rpm")
        duty = fans.get("duty") or {}
        self.tile_fan.update(
            f"{rpm}" if isinstance(rpm, int) else "—",
            (f"{duty.get('cpu', '—')}% / {duty.get('gpu', '—')}% duty"
             if duty else "EC telemetry"),
            None if not isinstance(rpm, int) else min(1.0, rpm / 6000.0))
        ram = system.get("ram") or {}
        self.tile_ram.update(
            fmt(ram.get("pct"), "%"),
            f"{ram.get('used_gb', '—')} / {ram.get('total_gb', '—')} GB",
            None if ram.get("pct") is None else float(ram["pct"]) / 100.0,
            "warn" if (ram.get("pct") or 0) >= 85 else "ok")

        if gmode.get("available"):
            cur = gmode.get("current")
            tgt = gmode.get("target")
            self.gpu_now.set_label(
                GPU_MODE_INFO.get(cur, (cur,))[0] if cur else "Unknown")
            if gmode.get("apply_failed"):
                self.gpu_badge.update("switch failed", "err")
            elif gmode.get("restart_required"):
                self.gpu_badge.update("restart required", "warn")
            elif gmode.get("current_source") == "runtime":
                self.gpu_badge.update("live", "ok")
            else:
                self.gpu_badge.update("active", "ok")
            rt = (gmode.get("runtime") or {}).get("mode")
            parts = [GPU_MODE_INFO.get(cur, (cur,))[1] if cur else ""]
            if tgt and tgt != cur:
                parts.append(f"{GPU_MODE_INFO.get(tgt, (tgt,))[0]} staged for "
                             "next power-on")
            if rt:
                parts.append(
                    f"live topology: {GPU_MODE_INFO.get(rt, (rt,))[0]}")
            self.gpu_caption.set_label(" · ".join(p for p in parts if p))
        else:
            self.gpu_now.set_label("No switchable MUX")
            self.gpu_badge.update("unavailable", "muted")
            self.gpu_caption.set_label(
                "This machine does not expose a firmware graphics switch.")

        self.gpu_alert.set_visible(bool(gmode.get("restart_required")
                                        and not gmode.get("apply_failed")))
        if gmode.get("restart_required"):
            tgt = gmode.get("target")
            self.gpu_alert.title_label.set_label("Restart required")
            self.gpu_alert.subtitle_label.set_label(
                f"{GPU_MODE_INFO.get(tgt, (tgt,))[0]} is staged — restart to "
                "finish the graphics-mode switch")
        self.fail_alert.set_visible(bool(gmode.get("apply_failed")))
        if gmode.get("apply_failed"):
            req = gmode.get("requested")
            self.fail_alert.subtitle_label.set_label(
                f"Requested {GPU_MODE_INFO.get(req, (req,))[0]}, but the "
                "machine is still running the previous mode. A warm reboot "
                "does not re-run the MUX POST step — shut down fully, then "
                "power on.")

        if snap.get("fw"):
            self.kv["EC firmware"].set_label(fw(snap["fw"]))


# ============================================================== performance

PERF_INFO = {
    "eco": ("Battery Saver", "Lowest power draw — best for unplugged work",
            "battery-good-symbolic"),
    "comfort": ("Balanced", "Everyday profile — quiet with headroom", "power-profile-balanced-symbolic"),
    "turbo": ("Performance", "Maximum sustained clocks for gaming", "power-profile-performance-symbolic"),
}


class PerfPage(Page):
    def __init__(self, window):
        super().__init__(
            window, "power-profile-performance-symbolic", "Performance",
            "Platform profile and the power features behind it")

        self.col.append(theme.subsection(
            "Platform profile",
            "EC shift mode — changes clocks, fan curves and power limits together"))

        grid = Gtk.Grid(column_homogeneous=True, column_spacing=14)
        self.col.append(grid)
        self.mode_cards = {}
        for i, key in enumerate(("eco", "comfort", "turbo")):
            name, desc, ico = PERF_INFO[key]
            cardw = theme.ModeCard(key, name, desc, ico)
            cardw.connect("toggled", self._on_card, key)
            grid.attach(cardw, i, 0, 1, 1)
            self.mode_cards[key] = cardw

        card = theme.card("Power features",
                          "Toggle hardware directly on the embedded controller",
                          "preferences-system-symbolic")
        self.col.append(card)
        body = card.body

        self.cooler_row = self._switch_row(
            body, "power-profile-performance-symbolic", "Cooler Boost",
            "Both fans to maximum — instant heat dump", self.on_cooler)
        body.append(self._sep())
        self.sbat_row = self._switch_row(
            body, "battery-symbolic", "Super Battery (EC power plane)",
            "Limits one power plane for extra runtime", self.on_super_battery)
        body.append(self._sep())
        self.saver_row = self._switch_row(
            body, "night-light-symbolic", "Super Battery Saver",
            "Battery Saver profile · power-plane limit · silent fans · "
            "CPU capped at 2.0 GHz", self.on_saver)

        cal = theme.card("Calibration",
                         "Only needed once on boards that report turbo as an "
                         "unknown raw code", "emblem-system-symbolic")
        self.col.append(cal)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        cal_text = theme.label("Probe the raw EC shift-mode codes",
                               "mc-card-sub", wrap=True)
        cal_text.set_hexpand(True)
        row.append(cal_text)
        cal_btn = Gtk.Button(label="Run calibration")
        cal_btn.add_css_class("mc-ghost")
        cal_btn.connect("clicked", self.on_calibrate)
        row.append(cal_btn)
        cal.body.append(row)

        self._syncing = False

    @staticmethod
    def _sep() -> Gtk.Box:
        sep = Gtk.Box()
        sep.add_css_class("mc-sep")
        sep.set_margin_top(2)
        sep.set_margin_bottom(2)
        return sep

    def _switch_row(self, body: Gtk.Box, icon_name: str, title: str,
                    subtitle: str, handler) -> Gtk.Switch:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(theme.icon(icon_name, 18, "mc-card-icon"))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(theme.label(title, "mc-card-title"))
        text.append(theme.label(subtitle, "mc-card-sub", wrap=True))
        row.append(text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        sw = Gtk.Switch()
        sw.set_valign(Gtk.Align.CENTER)
        sw.connect("notify::active", handler)
        row.append(sw)
        body.append(row)
        return sw

    # ---- handlers ----------------------------------------------------------

    def _on_card(self, card, key: str) -> None:
        if self._syncing or not card.get_active():
            return
        self._syncing = True
        try:
            for k, other in self.mode_cards.items():
                if k != key and other.get_active():
                    other.set_active(False)
        finally:
            self._syncing = False
        try:
            with daemon_client.Client() as c:
                c.request("set_shift_mode", mode=key)
            self.window.toast(f"{PERF_INFO[key][0]} profile applied")
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"profile switch failed: {e}")

    def on_cooler(self, row, *_a):
        if getattr(row, "_sync", False):
            return
        try:
            with daemon_client.Client() as c:
                c.request("set_cooler_boost", on=row.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"cooler boost failed: {e}")
            self._flip(row, self.on_cooler)

    def on_super_battery(self, row, *_a):
        if getattr(row, "_sync", False):
            return
        try:
            with daemon_client.Client() as c:
                c.request("set_super_battery", on=row.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"super battery failed: {e}")
            self._flip(row, self.on_super_battery)

    def on_saver(self, row, *_a):
        if getattr(row, "_sync", False):
            return
        try:
            with daemon_client.Client() as c:
                res = c.request("set_super_saver", on=row.get_active())
            if res.get("note"):
                self.window.toast(res["note"])
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"super saver failed: {e}")
            self._flip(row, self.on_saver)

    @staticmethod
    def _flip(sw: Gtk.Switch, handler) -> None:
        sw._sync = True
        try:
            sw.set_active(not sw.get_active())
        finally:
            sw._sync = False

    def on_calibrate(self, *_a):
        try:
            with daemon_client.Client() as c:
                res = c.request("calibrate_shift_mode")
            self.window.toast(f"calibrated: {res}")
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"calibration failed: {e}")

    # ---- refresh -----------------------------------------------------------

    @staticmethod
    def _sync(sw: Gtk.Switch, value: bool) -> None:
        if bool(value) == sw.get_active():
            return
        sw._sync = True
        try:
            sw.set_active(bool(value))
        finally:
            sw._sync = False

    def refresh(self, snap: dict) -> None:
        modes = snap.get("modes") or {}
        shift = (modes.get("shift") or "").split(" ")[0].lower()
        self._syncing = True
        try:
            for key, card in self.mode_cards.items():
                card.set_active(key == shift)
                if key == shift:
                    card.set_status("ACTIVE", "ok")
                elif key == "turbo":
                    card.set_status("", "muted")
                else:
                    card.set_status("", "muted")
        finally:
            self._syncing = False
        self._sync(self.cooler_row, bool(modes.get("cooler_boost")))
        self._sync(self.sbat_row, bool(modes.get("super_battery")))
        self._sync(self.saver_row, bool(modes.get("super_saver")))

    def reachability(self, ok, why):
        pass


# ==================================================================== gpu

GPU_MODE_INFO = {
    "hybrid": ("MSHybrid",
               "Both GPUs active. The iGPU presents and the dGPU renders on "
               "demand (NVIDIA Optimus) — best battery life with full gaming "
               "performance.",
               "video-display-symbolic"),
    "discrete": ("Discrete",
                 "The dGPU drives the panel directly. Highest frame rates, "
                 "no copy overhead, noticeably more power draw.",
                 "media-playback-start-symbolic"),
    "integrated": ("Integrated",
                   "iGPU only. The dGPU is powered down and hidden — maximum "
                   "battery life and the quietest operation.",
                   "battery-good-symbolic"),
}


class GpuPage(Page):
    """Firmware MUX switch with real post-boot verification."""

    def __init__(self, window):
        super().__init__(
            window, "video-display-symbolic", "Graphics",
            "Switch GPUs at the firmware level — MSHybrid, Discrete or "
            "Integrated")

        self._picked: str | None = None
        self._last_target: str | None = None
        self._syncing = False
        self._state: dict = {}

        # ---- status banner -------------------------------------------------
        self.banner = theme.banner("Reading firmware…", "", "accent",
                                   "view-refresh-symbolic")
        self.banner_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                      spacing=8)
        self.banner_actions.set_valign(Gtk.Align.CENTER)
        self.restart_btn = Gtk.Button(label="Restart now")
        self.restart_btn.add_css_class("mc-primary")
        self.restart_btn.connect("clicked", self.on_restart)
        self.shutdown_btn = Gtk.Button(label="Shut down fully")
        self.shutdown_btn.add_css_class("mc-ghost")
        self.shutdown_btn.connect("clicked", self.on_shutdown)
        self.banner_actions.append(self.shutdown_btn)
        self.banner_actions.append(self.restart_btn)
        self.banner.append(self.banner_actions)
        self.col.append(self.banner)

        # ---- mode cards ----------------------------------------------------
        self.col.append(theme.subsection(
            "Choose a mode",
            "The target is written to the MsiDCVarData UEFI variable and "
            "armed with MSI's EC apply handshake."))
        grid = Gtk.Grid(column_homogeneous=True, column_spacing=14)
        self.col.append(grid)
        self.cards: dict[str, theme.ModeCard] = {}
        for i, key in enumerate(("hybrid", "discrete", "integrated")):
            name, desc, ico = GPU_MODE_INFO[key]
            card = theme.ModeCard(key, name, desc, ico)
            card.connect("toggled", self._on_card, key)
            grid.attach(card, i, 0, 1, 1)
            self.cards[key] = card

        # ---- apply row -----------------------------------------------------
        apply_card = theme.card()
        self.col.append(apply_card)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        self.apply_title = theme.label("Pick a mode", "mc-card-title")
        self.apply_sub = theme.label(
            "Nothing is written until you press Apply",
            "mc-card-sub", wrap=True)
        text.append(self.apply_title)
        text.append(self.apply_sub)
        row.append(text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        row.append(self.spinner)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("mc-primary")
        self.apply_btn.set_valign(Gtk.Align.CENTER)
        self.apply_btn.connect("clicked", self.on_apply)
        row.append(self.apply_btn)
        apply_card.body.append(row)

        # ---- live state ----------------------------------------------------
        state_card = theme.card("Live state", "Read from firmware and hardware",
                                "emblem-system-symbolic")
        self.col.append(state_card)
        self.kv = {}
        for key, hint in (("Running now", "firmware current mode"),
                          ("Live topology", "GPUs visible to the OS"),
                          ("Staged target", "applied on next power-on"),
                          ("Supported", "advertised by this firmware"),
                          ("EC MUX 0x2E", "PXCT panel routing"),
                          ("AC power", "required by the firmware")):
            row, value = theme.key_value(key, "—")
            state_card.body.append(row)
            self.kv[key] = value

        # ---- detected GPUs -------------------------------------------------
        gpu_card = theme.card("Detected GPUs", "Display controllers in this boot",
                              "video-display-symbolic")
        self.col.append(gpu_card)
        self.gpu_body = gpu_card.body

        # ---- safety --------------------------------------------------------
        safety = theme.card("How the switch works",
                            "Firmware-level — the app never reboots on its own",
                            "dialog-information-symbolic")
        self.col.append(safety)
        for step in (
            "1. The target mode is written to the MsiDCVarData UEFI variable.",
            "2. The EC apply trigger runs and the firmware acknowledges it.",
            "3. The MUX is re-routed during POST — so the next start applies it.",
            "4. If any step fails before the trigger, the previous target is "
            "restored and verified.",
            "If a warm restart does not change the mode, shut down completely "
            "and power on; this is what MSI Center's own restart prompt means.",
        ):
            safety.body.append(theme.label(step, "mc-card-sub", wrap=True))

        self._gpu_rows = 0

    # ---- selection ---------------------------------------------------------

    def _on_card(self, card: theme.ModeCard, key: str) -> None:
        if self._syncing or not card.get_active():
            return
        self._syncing = True
        try:
            for k, other in self.cards.items():
                if k != key and other.get_active():
                    other.set_active(False)
        finally:
            self._syncing = False
        self._picked = key
        label = GPU_MODE_INFO[key][0]
        self.apply_title.set_label(f"Stage {label}")
        self.apply_sub.set_label(
            "The firmware changes on the next start — press Apply to stage it")
        self.apply_btn.set_sensitive(
            key != (self._state.get("target") or self._state.get("current")))

    # ---- actions -----------------------------------------------------------

    def on_apply(self, *_a):
        key = self._picked
        if not key:
            self.window.toast("Pick a graphics mode first")
            return
        if key == self._state.get("target"):
            self.window.toast(
                f"{GPU_MODE_INFO[key][0]} is already staged — restart to apply")
            return
        label = GPU_MODE_INFO[key][0]
        dlg = Adw.MessageDialog(
            transient_for=self.window,
            heading=f"Stage {label}?",
            body="The target is written to UEFI and armed now; the GPU MUX "
                 "changes when the machine next starts.\n\n"
                 "Connect AC power first. Save your work, then restart.")
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("apply", f"Stage {label}")
        dlg.set_response_appearance("apply", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._apply_confirmed, key)
        dlg.present()

    def _apply_confirmed(self, dlg, response, key: str) -> None:
        dlg.destroy()
        if response != "apply":
            return
        self.apply_btn.set_sensitive(False)
        self.spinner.set_visible(True)
        self.spinner.start()

        def work() -> None:
            try:
                with daemon_client.Client(timeout=25.0) as c:
                    res = c.request("set_gpu_mode", mode=key)
                err = None
            except (daemon_client.DaemonDown, RuntimeError) as e:
                res, err = None, str(e)
            GLib.idle_add(self._apply_done, res, err, key)

        threading.Thread(target=work, daemon=True).start()

    def _apply_done(self, res, err, key: str) -> bool:
        self.spinner.stop()
        self.spinner.set_visible(False)
        self.apply_btn.set_sensitive(True)
        if err:
            self.window.toast(f"Graphics switch failed: {err}")
        elif res and res.get("already_staged"):
            self.window.toast(
                f"{GPU_MODE_INFO[key][0]} was already staged — restart to apply")
        else:
            self.window.toast(
                f"{GPU_MODE_INFO[key][0]} staged — restart to apply it")
        self._last_target = None
        self.window.request_snapshot()
        return False

    def _confirm_power(self, kind: str) -> None:
        if kind == "reboot":
            heading = "Restart now?"
            body = ("The graphics mode will be applied when the machine starts.\n\n"
                    "Save your work. If the mode is unchanged after the "
                    "restart, shut down completely and power on again.")
            btn = "Restart"
        else:
            heading = "Shut down now?"
            body = ("A full shutdown is the most reliable way to apply the "
                    "graphics-mode change — the MUX is re-routed during POST.\n\n"
                    "Save your work. Power the laptop back on to finish.")
            btn = "Shut down"
        dlg = Adw.MessageDialog(transient_for=self.window, heading=heading,
                                body=body)
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("go", btn)
        dlg.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")
        dlg.connect("response", self._power_confirmed, kind)
        dlg.present()

    def _power_confirmed(self, dlg, response, kind: str) -> None:
        dlg.destroy()
        if response != "go":
            return
        op = "gpu_restart" if kind == "reboot" else "gpu_shutdown"
        try:
            with daemon_client.Client(timeout=10.0) as c:
                c.request(op)
            self.window.toast("Shutting down…" if kind == "poweroff"
                              else "Restarting…")
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"Power action failed: {e}")

    def on_restart(self, *_a):
        self._confirm_power("reboot")

    def on_shutdown(self, *_a):
        self._confirm_power("poweroff")

    # ---- refresh -----------------------------------------------------------

    def refresh(self, snap: dict) -> None:
        st = snap.get("gpu_mode") or {}
        self._state = st
        self._refresh_gpus()

        if not st.get("available"):
            self.banner.set_visible(True)
            self.banner.title_label.set_label("No switchable GPU MUX")
            self.banner.subtitle_label.set_label(
                "This firmware does not expose MsiDCVarData — the graphics "
                "mode is fixed.")
            for cls in ("mc-banner-accent", "mc-banner-ok", "mc-banner-warn",
                        "mc-banner-err"):
                self.banner.remove_css_class(cls)
            self.banner.add_css_class("mc-banner-muted")
            self.banner_actions.set_visible(False)
            self.apply_btn.set_sensitive(False)
            for card in self.cards.values():
                card.set_sensitive(False)
                card.set_status("", "muted")
            for value in self.kv.values():
                value.set_label("—")
            return

        cur = st.get("current")
        tgt = st.get("target")
        sup = st.get("supported") or []
        pending = bool(st.get("restart_required"))
        failed = bool(st.get("apply_failed"))

        # banner
        for cls in ("mc-banner-accent", "mc-banner-ok", "mc-banner-warn",
                    "mc-banner-err", "mc-banner-muted"):
            self.banner.remove_css_class(cls)
        if failed:
            self.banner.add_css_class("mc-banner-err")
            req = st.get("requested")
            self.banner.title_label.set_label("The switch did not apply")
            self.banner.subtitle_label.set_label(
                f"Requested {GPU_MODE_INFO.get(req, (req,))[0]}, but the machine "
                f"is running {GPU_MODE_INFO.get(cur, (cur,))[0]}. A warm reboot "
                "skipped the MUX POST step — shut down completely, then power on.")
        elif pending:
            self.banner.add_css_class("mc-banner-warn")
            self.banner.title_label.set_label("Restart required")
            self.banner.subtitle_label.set_label(
                f"{GPU_MODE_INFO.get(tgt, (tgt,))[0]} is staged. Restart to "
                f"switch from {GPU_MODE_INFO.get(cur, (cur,))[0]}.")
        else:
            self.banner.add_css_class("mc-banner-ok")
            self.banner.title_label.set_label(
                f"Running in {GPU_MODE_INFO.get(cur, (cur,))[0]}")
            extra = (" · confirmed by live GPU topology"
                     if st.get("current_source") == "runtime" else "")
            self.banner.subtitle_label.set_label(
                "The firmware and the hardware agree" + extra + ".")
        self.banner_actions.set_visible(pending)

        # cards
        if self._picked is None:
            self._picked = tgt
        self._syncing = True
        try:
            for key, card in self.cards.items():
                card.set_sensitive(key in sup)
                if key == cur:
                    card.set_status("RUNNING", "ok")
                elif key == tgt and tgt != cur:
                    card.set_status("STAGED", "warn")
                elif key == self._picked:
                    card.set_status("SELECTED", "accent")
                else:
                    card.set_status("", "muted")
                card.set_active(key == self._picked)
        finally:
            self._syncing = False
        if self._picked not in sup and sup:
            self._picked = sup[0]

        # apply row
        if tgt and tgt == self._picked and tgt != cur:
            self.apply_title.set_label(
                f"{GPU_MODE_INFO.get(tgt, (tgt,))[0]} staged")
            self.apply_sub.set_label("Restart is required to apply it")
            self.apply_btn.set_sensitive(False)
        elif tgt == self._picked:
            self.apply_title.set_label(
                f"Running in {GPU_MODE_INFO.get(self._picked, (self._picked,))[0]}")
            self.apply_sub.set_label("This is already the active mode")
            self.apply_btn.set_sensitive(False)
        else:
            label = GPU_MODE_INFO.get(self._picked, (self._picked,))[0]
            self.apply_title.set_label(f"Stage {label}")
            self.apply_sub.set_label(
                "Firmware changes on the next start — press Apply to stage it")
            self.apply_btn.set_sensitive(self._picked in sup)

        # live state values
        rt = (st.get("runtime") or {}).get("mode")
        mux = st.get("mux_ec")
        self.kv["Running now"].set_label(
            GPU_MODE_INFO.get(cur, (cur,))[0] if cur else "unknown")
        self.kv["Live topology"].set_label(
            GPU_MODE_INFO.get(rt, (rt,))[0] if rt else "inconclusive")
        self.kv["Staged target"].set_label(
            GPU_MODE_INFO.get(tgt, (tgt,))[0] if tgt else "—")
        self.kv["Supported"].set_label(
            " · ".join(GPU_MODE_INFO.get(m, (m,))[0] for m in sup) or "—")
        self.kv["EC MUX 0x2E"].set_label(
            f"0x{mux:02X} · {'dGPU path' if mux & 0x40 else 'iGPU path'}"
            if mux is not None else "unavailable")
        self.kv["AC power"].set_label(
            "online" if st.get("ac_online") else "ON BATTERY — connect AC")

    def _refresh_gpus(self) -> None:
        if not self.get_mapped():
            return
        now = time.monotonic()
        if now - getattr(self, "_gpu_fetch_at", 0.0) < 4.0:
            return
        self._gpu_fetch_at = now
        try:
            with daemon_client.Client(timeout=4.0) as c:
                st = c.request("gpu_mode_state")
        except (daemon_client.DaemonDown, RuntimeError):
            return
        gpus = st.get("gpus") or []
        children = []
        child = self.gpu_body.get_first_child()
        while child is not None:
            children.append(child)
            child = child.get_next_sibling()
        if len(children) == len(gpus) and gpus:
            for child, gpu in zip(children, gpus):
                if hasattr(child, "mc_value"):
                    child.mc_value.set_label(self._gpu_text(gpu))
            return
        for child in children:
            self.gpu_body.remove(child)
        if not gpus:
            self.gpu_body.append(theme.label("No display controllers detected",
                                             "mc-card-sub"))
            return
        for gpu in gpus:
            row, value = theme.key_value(self._gpu_title(gpu),
                                         self._gpu_text(gpu))
            row.mc_value = value
            self.gpu_body.append(row)

    @staticmethod
    def _gpu_title(gpu: dict) -> str:
        vendor = {"0x10de": "NVIDIA", "0x8086": "Intel",
                  "0x1002": "AMD"}.get(gpu.get("vendor", ""),
                                       gpu.get("vendor", ""))
        return f"{vendor} · {gpu.get('pci', '')}"

    @staticmethod
    def _gpu_text(gpu: dict) -> str:
        driver = gpu.get("driver") or "none"
        return f"device {gpu.get('device', '—')} · driver {driver}"

    def reachability(self, ok, why):
        if not ok:
            self.banner.title_label.set_label("Daemon unreachable")
            self.banner.subtitle_label.set_label(why)


# ==================================================================== fans

class CurveEditor(Gtk.Frame):
    """Six (temp °C → duty %) spin rows for one fan, like MSI Center's editor."""

    def __init__(self, fan_label: str, points=None) -> None:
        super().__init__()
        self._label = fan_label
        self._build()
        self.set_points(points or wmi2.DEFAULT_CPU_CURVE)

    def _build(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        lbl = Gtk.Label(label=self._label, xalign=0.0)
        lbl.add_css_class("heading")
        head.append(lbl)
        cols = Gtk.Label(label="temp °C   duty %", hexpand=True)
        cols.set_xalign(1.0)
        cols.add_css_class("caption")
        cols.add_css_class("dim-label")
        head.append(cols)
        box.append(head)

        self.grid = Gtk.Grid(column_spacing=10, row_spacing=2)
        self.spins: list[tuple[Gtk.SpinButton, Gtk.SpinButton]] = []
        box.append(self.grid)
        self.set_child(box)

    def _ensure_rows(self, n: int = 6) -> None:
        while len(self.spins) < n:
            i = len(self.spins)
            t = self._spin(30, 115, 5, 15)
            d = self._spin(0, 100, 5, 20)
            self.grid.attach(t, 0, i, 1, 1)
            self.grid.attach(d, 1, i, 1, 1)
            self.spins.append((t, d))

    @staticmethod
    def _spin(lo: int, hi: int, step: int, page: int) -> Gtk.SpinButton:
        s = Gtk.SpinButton()
        s.set_range(lo, hi)
        s.set_increments(step, page)
        s.set_digits(0)
        s.add_css_class("monospace")
        return s

    def set_points(self, points) -> None:
        self._ensure_rows()
        src = list(points) if points else wmi2.DEFAULT_CPU_CURVE
        for i in range(6):
            t, d = self.spins[i]
            pair = src[i] if i < len(src) else src[-1]
            t.set_value(float(pair[0]))
            d.set_value(float(pair[1]))

    def get_points(self) -> list[list[int]]:
        self._ensure_rows()
        return [[int(t.get_value()), int(d.get_value())]
                for t, d in self.spins]


class FansPage(Page):
    def __init__(self, window):
        super().__init__(window, "weather-windy-symbolic", "Fans",
                         "Cooling presets, custom six-point curves and the "
                         "thermal watchdog")

        g = Adw.PreferencesGroup(
            title="Fan mode",
            description="Managed by the embedded controller: 'Silent' caps fan "
                        "speed for quiet; Auto follows the thermal budget.")
        self.col.append(g)

        row = Adw.ActionRow(title="Mode")
        self.preset_btns = SegButtons([("auto", "Auto"), ("silent", "Silent")],
                                      self.on_fan_mode)
        self.preset_btns.select("auto")
        self.preset_btns.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self.preset_btns)
        g.add(row)

        self.boost_row = Adw.SwitchRow(
            title="Cooler Boost",
            subtitle="Both fans to max — the fastest way to dump heat")
        g.add(self.boost_row)
        self.boost_row.connect("notify::active", self.on_boost)

        # ---- custom curves (MSI-Center parity, via the WMI2 tables) ----
        self.curve_switch_row = Adw.SwitchRow(
            title="Custom fan curve",
            subtitle="Table-driven control (the interface MSI Center uses) — "
                     "firmware applies the curve in hardware")
        self.curve_switch_row.connect("notify::active", self.on_curve_toggle)
        g.add(self.curve_switch_row)

        self.manual_row = Adw.ActionRow()
        self.manual_row.set_title("Custom curve is off")
        self.manual_row.set_subtitle(
            "Flip it on to enable the six-point CPU/GPU tables below")
        g.add(self.manual_row)

        self.cpu_editor = CurveEditor("CPU fan")
        self.gpu_editor = CurveEditor("GPU fan")
        self._editor_holder = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                                      spacing=10)
        self._editor_holder.append(self.cpu_editor)
        self._editor_holder.append(self.gpu_editor)
        self.col.append(self._editor_holder)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.apply_btn = Gtk.Button(label="Apply curve")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply_curve)
        self.reset_btn = Gtk.Button(label="MSI defaults")
        self.reset_btn.connect("clicked", self.on_reset_curve)
        self.save_btn = Gtk.Button(label="Save profile…")
        self.save_btn.connect("clicked", self.on_save_profile)
        self.load_btn = Gtk.Button(label="Apply profile…")
        self.load_btn.connect("clicked", self.on_apply_profile)
        for b in (self.apply_btn, self.reset_btn, self.save_btn, self.load_btn):
            btns.append(b)
        btns.set_margin_top(4)
        self.col.append(btns)

        g2 = Adw.PreferencesGroup(title="Live")
        self.col.append(g2)

        self.status_row = Adw.ActionRow(title="Mode / duty")
        self.duty_label = Gtk.Label(label="—")
        self.duty_label.add_css_class("monospace")
        self.status_row.add_suffix(self.duty_label)
        g2.add(self.status_row)

        self.rpm_row = Adw.ActionRow(title="Fan speed")
        self.rpm_label = Gtk.Label(label="—")
        self.rpm_label.add_css_class("monospace")
        self.rpm_row.add_suffix(self.rpm_label)
        g2.add(self.rpm_row)

        self.watchdog_row = Adw.ActionRow(title="Watchdog")
        self.watchdog_note = Gtk.Label(label="armed while curve control is on")
        self.watchdog_note.add_css_class("caption")
        self.watchdog_note.add_css_class("dim-label")
        self.watchdog_row.add_suffix(self.watchdog_note)
        g2.add(self.watchdog_row)

        self.profile_names: list[str] = []
        self._curve_loaded_once = False
        self._last_wmi2_error: str | None = None
        self.connect("map", lambda *_a: self.prefetch_curves())

    # ---- daemon helpers -----------------------------------------------

    def _daemon(self, op: str, **args):
        with daemon_client.Client() as c:
            return c.request(op, **args)

    def _background(self, fn, *a, on_done=None):
        def work():
            try:
                out = ("ok", fn(*a))
            except Exception as e:  # surfaced on the GTK thread
                out = ("err", str(e))
            if on_done:
                GLib.idle_add(on_done, *out)
        threading.Thread(target=work, daemon=True).start()

    # ---- actions -------------------------------------------------------

    def on_fan_mode(self, name: str) -> None:
        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"fan preset failed: {payload}")
        self._background(
            lambda: self._daemon("set_fan_preset", preset=name),
            on_done=done)

    def on_boost(self, row, *_a):
        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"cooler boost failed: {payload}")
                row.set_active(not row.get_active())
        self._background(
            lambda: self._daemon("set_cooler_boost", on=row.get_active()),
            on_done=done)

    def on_curve_toggle(self, row, *_a):
        want = row.get_active()

        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"custom curve failed: {payload}")
                GLib.idle_add(self._set_switch_silent, not want)
                return
            state = payload or {}
            manual = bool(state.get("manual", want))
            GLib.idle_add(self._set_switch_silent, manual)
            self._refresh_curve_states_from(wait_manual=manual)

        self._background(lambda: self._daemon("fan_curve_manual", on=want),
                         on_done=done)

    def _set_switch_silent(self, active: bool) -> None:
        row = self.curve_switch_row
        if row.get_active() != active:
            row.handler_block_by_func(self.on_curve_toggle)
            row.set_active(active)
            row.handler_unblock_by_func(self.on_curve_toggle)

    def _refresh_curve_states_from(self, wait_manual: bool | None = None) -> None:
        def fetch():
            return self._daemon("fan_curve_get")

        def done(kind, payload):
            if kind == "err":
                self._last_wmi2_error = payload
                self.manual_row.set_title("Custom curve unavailable")
                self.manual_row.set_subtitle(payload)
                return
            self._last_wmi2_error = None
            snap = payload or {}
            manual = snap.get("manual", False)
            if wait_manual is not None:
                manual = wait_manual if snap.get("available") else False
            self._apply_curve_state(snap, manual)

        self._background(fetch, on_done=done)

    def _apply_curve_state(self, snap: dict, manual: bool) -> None:
        title = ("Custom curve is ON (watchdog active)"
                 if manual else "Custom curve is off")
        self.manual_row.set_title(title)
        self.manual_row.set_subtitle(
            "Six-point CPU/GPU tables written straight to the firmware, like "
            "MSI Center does on Windows"
            if manual else
            "Flip it on to enable the six-point CPU/GPU tables below")
        self._set_switch_silent(manual)
        for btn in (self.apply_btn, self.reset_btn,
                    self.save_btn, self.load_btn):
            btn.set_sensitive(manual)
        cpu = snap.get("cpu_curve")
        gpu = snap.get("gpu_curve")
        if manual:
            if cpu:
                self.cpu_editor.set_points(cpu)
            if gpu:
                self.gpu_editor.set_points(gpu)
        elif self._last_wmi2_error is None and cpu and gpu:
            # previews even when off (firmware stores the last tables)
            self.cpu_editor.set_points(cpu)
            self.gpu_editor.set_points(gpu)
        if snap.get("note"):
            self.watchdog_note.set_label(str(snap["note"]))

    def prefetch_curves(self) -> None:
        """Fetch current tables once the page is first shown."""
        if self._curve_loaded_once:
            return
        self._curve_loaded_once = True
        self._refresh_curve_states_from()

    def on_apply_curve(self, *_a) -> None:
        cpu = self.cpu_editor.get_points()
        gpu = self.gpu_editor.get_points()
        if not self.curve_switch_row.get_active():
            self.window.toast("enable 'Custom fan curve' first")
            return

        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"apply failed: {payload}")
            else:
                self.window.toast("curve written to firmware"
                                  " (see duty readbacks in a second)")
        self._background(lambda: self._daemon("fan_curve_set", cpu=cpu,
                                              gpu=gpu), on_done=done)

    def on_reset_curve(self, *_a) -> None:
        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"reset failed: {payload}")
            else:
                self.window.toast("MSI default curve restored")
                self.cpu_editor.set_points(wmi2.DEFAULT_CPU_CURVE)
                self.gpu_editor.set_points(wmi2.DEFAULT_GPU_CURVE)
        self._background(self._daemon("fan_curve_reset"), on_done=done)

    def on_save_profile(self, *_a) -> None:
        dlg = Adw.Dialog()
        dlg.set_title("Save fan curve profile")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_bottom(12)

        entry = Adw.EntryRow(title="Profile name")
        group = Adw.PreferencesGroup(
            title="Save the current CPU+GPU curve tables")
        group.add(entry)
        box.append(group)

        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        hb.append(cancel)
        hb.append(save)
        box.append(hb)

        toolbar.set_content(box)
        dlg.set_content_widget(toolbar)

        def do_save(*_a):
            name = entry.get_text().strip()
            if not name:
                entry.add_css_class("error")
                return
            try:
                self._daemon("fan_profile_save", name=name,
                             data={"cpu": self.cpu_editor.get_points(),
                                   "gpu": self.gpu_editor.get_points()})
                self.window.toast(f"saved fan curve '{name}'")
                dlg.force_close()
            except Exception as e:
                self.window.toast(f"save failed: {e}")

        save.connect("clicked", do_save)
        cancel.connect("clicked", lambda *_a: dlg.force_close())
        entry.connect("entry-activated", do_save)
        dlg.present(self.window)

    def on_apply_profile(self, *_a) -> None:
        def fetch():
            return self._daemon("fan_profile_list")
        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"profile listing failed: {payload}")
                return
            names = (payload or {}).get("profiles") or []
            if not names:
                self.window.toast("no saved fan profiles yet")
                return
            self._show_apply_dialog(names)
        self._background(fetch, on_done=done)

    def _show_apply_dialog(self, names: list[str]) -> None:
        dlg = Adw.Dialog()
        dlg.set_title("Apply fan curve profile")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12); box.set_margin_start(12)
        box.set_margin_end(12); box.set_margin_bottom(12)

        row = Adw.ComboRow(title="Profile")
        model = Gtk.StringList()
        for n in names:
            model.append(n)
        row.set_model(model)
        row.set_selected(0)
        group = Adw.PreferencesGroup()
        group.add(row)
        box.append(group)

        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        apply = Gtk.Button(label="Apply")
        apply.add_css_class("suggested-action")
        hb.append(cancel)
        hb.append(apply)
        box.append(hb)

        toolbar.set_content(box)
        dlg.set_content_widget(toolbar)

        def do_apply(*_a):
            idx = row.get_selected()
            if idx == Gtk.INVALID_LIST_POSITION:
                dlg.force_close()
                return
            name = names[idx]
            try:
                data = self._daemon("fan_profile_get", name=name)
                if not self.curve_switch_row.get_active():
                    self._background(lambda: self._daemon(
                        "fan_curve_manual", on=True),
                        on_done=self._manual_fetch_after(name))
                else:
                    self._apply_profile_curves(name, data)
                dlg.force_close()
            except Exception as e:
                self.window.toast(f"apply failed: {e}")

        def _manual_fetch_after(name):
            def done(kind, payload):
                if kind == "err":
                    self.window.toast(f"manual-enabling failed: {payload}")
                    return
                try:
                    data = self._daemon("fan_profile_get", name=name)
                    self._apply_profile_curves(name, data)
                except Exception as e:
                    self.window.toast(f"apply failed: {e}")
            return done

        apply.connect("clicked", do_apply)
        cancel.connect("clicked", lambda *_a: dlg.force_close())
        dlg.present(self.window)

    def _apply_profile_curves(self, name: str, data: dict) -> None:
        cpu = data.get("cpu")
        gpu = data.get("gpu")
        if cpu is None or gpu is None:
            self.window.toast(f"profile '{name}' is empty")
            return
        def done(kind, payload):
            if kind == "err":
                self.window.toast(f"apply failed: {payload}")
            else:
                self.window.toast(f"applied profile '{name}'")
                self.cpu_editor.set_points(cpu)
                self.gpu_editor.set_points(gpu)
        self._background(lambda: self._daemon("fan_curve_set", cpu=cpu,
                                              gpu=gpu), on_done=done)


    def refresh(self, snap: dict) -> None:
        modes = snap.get("modes") or {}
        fan_mode = (modes.get("fan") or "auto").lower()
        fans = snap.get("fans") or {}
        wmi2_state = snap.get("wmi2") or {}

        manual = bool(wmi2_state.get("manual"))
        self._set_switch_silent(manual)
        self.manual_row.set_title(
            "Custom curve is ON (watchdog active)"
            if manual else "Custom curve is off")
        for btn in (self.apply_btn, self.reset_btn, self.save_btn,
                    self.load_btn):
            btn.set_sensitive(manual)
        if wmi2_state.get("note"):
            self.watchdog_note.set_label(str(wmi2_state["note"]))
        if wmi2_state.get("error"):
            self.watchdog_note.set_label(f"interface: {wmi2_state['error']}")

        duty = fans.get("duty") or {}
        cpu = snap.get("cpu") or {}
        self.duty_label.set_label(
            f"{fan_mode} — cpu {fmt(duty.get('cpu'), '%')} / "
            f"gpu {fmt(duty.get('gpu'), '%')}")
        rpm = fans.get("rpm")
        if rpm is None:
            rpm = wmi2_state.get("rpm")
        self.rpm_label.set_label(
            ", ".join(str(r) for r in rpm) + " RPM"
            if isinstance(rpm, list) and rpm else "—")

        active_radio = fan_mode if fan_mode in self.preset_btns.buttons else "auto"
        self.preset_btns.select(active_radio)

        boost = self.boost_row
        if bool(modes.get("cooler_boost")) != boost.get_active():
            boost.handler_block_by_func(self.on_boost)
            boost.set_active(bool(modes.get("cooler_boost")))
            boost.handler_unblock_by_func(self.on_boost)

    def reachability(self, ok, why):
        pass


# ================================================================= battery

class BatteryPage(Page):
    def __init__(self, window):
        super().__init__(window, "battery-symbolic", "Battery",
                         "Charge limiting and long-term cell health")

        g = Adw.PreferencesGroup(
            title="Charge limit",
            description="Battery stops charging outside the window — the single "
                     "biggest lithium-aging saver. End 100 % = no limit.")
        self.col.append(g)

        self.start_row = Adw.SpinRow.new_with_range(0, 95, 1)
        self.start_row.set_title("Start charging below (%)")
        g.add(self.start_row)

        self.end_row = Adw.SpinRow.new_with_range(20, 100, 1)
        self.end_row.set_title("Stop charging at (%)")
        g.add(self.end_row)

        preset_row = Adw.ComboRow(
            title="Presets",
            model=Gtk.StringList.new(["60 / 80", "75 / 85", "90 / 95",
                                      "No limit"]))
        self.preset_row = preset_row
        g.add(preset_row)

        apply_btn = Gtk.Button(label="Apply charge limit")
        apply_btn.add_css_class("pill")
        apply_btn.add_css_class("suggested-action")
        apply_btn.connect("clicked", self.on_apply)
        act = Adw.ActionRow()
        act.add_suffix(apply_btn)
        g.add(act)

        self.state_row = Adw.ActionRow(title="Current values")
        self.state_row.set_subtitle("—")
        g.add(self.state_row)

        info = Gtk.Label(
            label="Charge state updates live on the Overview page.")
        g2 = Adw.PreferencesGroup()
        info.set_xalign(0.0)
        info.add_css_class("caption")
        info.add_css_class("dim-label")
        g2.add(info)
        self.col.append(g2)

    def on_apply(self, *_a):
        preset = self.preset_row.get_model()[self.preset_row.get_selected()]
        if preset == "No limit":
            end, start = 100, 0
        else:
            parts = (preset.split() or ["80", "90"])
            end = int(parts[-1]) if len(parts) > 1 else 80
            start = int(parts[0]) if parts else 60
        try:
            with daemon_client.Client() as c:
                res = c.request("set_battery_limit", end=end, start=start)
            self.start_row.set_value(res.get("start") or 0)
            self.end_row.set_value(res.get("end") or 80)
            self.window.toast(f"battery limit → {res.get('start')}/{res.get('end')}")
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"battery limit failed: {e}")

    def refresh(self, snap: dict) -> None:
        bat = snap.get("battery") or {}
        st = bat.get("start_threshold")
        end = bat.get("end_threshold")
        self.state_row.set_subtitle(
            f"start {st} % · end {end} %" if st is not None
            else "no charge thresholds exposed")
        if st is not None and not self._user_touched():
            self.start_row.set_value(st)
            self.end_row.set_value(end or 20)

    def _user_touched(self) -> bool:
        return False  # convenience: overview numbers always win until apply


# ================================================================= lighting

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "lighting", "icons")

# The physical keyboard reference (photo-matched deck geometry, 6 rows ×
# 4 zone-columns) lives in lighting/layout.py and is drawn by
# lighting/keyboard_view.py as one Cairo view with clickable zones.

# MSI Center's own effect set for this keyboard first, then this app's extras.
EFFECT_ORDER = ["off", "steady", "breath", "cycle", "wave", "dance", "wheel",
                "bouncy", "custom",
                "rainbow", "scycle", "spulse", "rbreath", "rain", "fire",
                "radar", "lightning", "gaming"]
EFFECT_NAMES = {
    # MSI Center effects (firmware-driven, byte-identical to MSI Center)
    "off": "Off",
    "steady": "Steady",
    "breath": "Breathing",
    "cycle": "Color Cycle",
    "wave": "Wave",
    "dance": "Dance",
    "wheel": "Wheel",
    "bouncy": "Bouncy Ball",
    "custom": "Customize",
    # extra looks rendered by this app
    "rainbow": "Rainbow Flow",
    "scycle": "Spectrum Cycle",
    "spulse": "Spectrum Pulse",
    "rbreath": "Rainbow Breath",
    "rain": "Rain",
    "fire": "Fire",
    "radar": "Radar",
    "lightning": "Lightning",
    "gaming": "Gaming",
}

# tuning rows shown per effect:
#   msi       — color mode radio (Rainbow | Customized)
#   colors    — MSI colour list editor + palette
#   msi_speed — speed radio (Slow | Medium | Fast)
#   wave_dir  — direction radio (Left | Right)
#   wheel_dir — direction radio (Clockwise | Counterclockwise)
#   speed / direction — software extras
#   custom    — the per-zone Customize editor
TUNING = {
    "off": (),
    "steady": ("msi", "colors"),
    "breath": ("msi", "colors", "msi_speed"),
    "cycle": ("msi", "colors", "msi_speed"),
    "wave": ("msi", "colors", "msi_speed", "wave_dir"),
    "dance": ("msi_speed",),
    "wheel": ("msi", "colors", "msi_speed", "wheel_dir"),
    "bouncy": ("msi", "colors"),
    "custom": ("custom",),
    "rainbow": ("speed", "direction"),
    "scycle": ("speed",),
    "spulse": ("speed",),
    "rbreath": ("speed",),
    "rain": ("speed",),
    "fire": ("speed",),
    "radar": ("speed", "direction"),
    "lightning": ("speed",),
    "gaming": (),
}

# effects offered inside the Customize zone editor (MSI's own palette)
CUSTOM_EFFECTS = ("off", "steady", "breath", "cycle", "wave", "dance")

SPEED_TITLES = {
    "rbreath": "Cycle length (seconds)",
    "rainbow": "Rainbow lap (seconds)",
    "scycle": "Cycle length (seconds)",
    "spulse": "Pulse period (seconds)",
    "rain": "Activity (6 = natural)",
    "fire": "Flicker speed (6 = natural)",
    "radar": "Sweep period (seconds)",
    "lightning": "Storm power (6 = natural)",
}

# effects rendered by the software 24-zone daemon (animate.py engines).
# "rainbow" is NOT here: it is firmware-driven (one burst of per-zone
# colour cycles; the MCU animates it itself, no daemon).
SOFTWARE = {"scycle", "spulse", "rbreath", "rain", "fire", "radar",
            "lightning"}

PAGE_CSS = \
    ".kb-card { background: rgba(127,127,127,0.08); border-radius: 14px;\n" \
    "  border: 1px solid rgba(127,127,127,0.16); }\n" \
    ".effect-tile { border-radius: 12px; padding-left: 10px; padding-right: 10px; }\n" \
    ".effect-tile.active { border: 2px solid #3584e4;\n" \
    "  background: rgba(53,132,228,0.14); }\n" \
    ".slot-active { background: rgba(53,132,228,0.16); border-radius: 8px; }"


def hex_to_rgba(hexstr: str) -> Gdk.RGBA:
    rgba = Gdk.RGBA()
    rgba.parse("#" + hexstr.strip("#"))
    return rgba


def rgba_to_ints(rgba: Gdk.RGBA) -> tuple[int, int, int]:
    clamp = lambda c: max(0, min(255, round(c * 255)))  # noqa: E731
    return clamp(rgba.red), clamp(rgba.green), clamp(rgba.blue)


class EffectTile(Gtk.Button):
    def __init__(self, key: str, page: "LightingPage"):
        super().__init__()
        self.effect_key = key
        self.add_css_class("effect-tile")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)
        icon = Gtk.Image.new_from_file(
            os.path.join(ICON_DIR, f"{key}.svg"))
        icon.set_pixel_size(34)
        box.append(icon)
        label = Gtk.Label(label=EFFECT_NAMES[key])
        label.add_css_class("caption")
        box.append(label)
        self.set_child(box)
        self.connect("clicked", page.on_effect_tile)


class ColorSlots(Gtk.Box):
    """Editable MSI colour list — one colour-dialog button per colour.

    Clicking a row makes it the *active slot*; the palette swatches then
    recolour that slot. `on_change` fires on add / remove / active change.
    """

    def __init__(self, max_colors: int = 5, on_change=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.max_colors = max_colors
        self._slots: list[tuple[Gtk.Box, Gtk.ColorDialogButton,
                                Gtk.Button]] = []
        self._active = 0
        self._on_change = on_change
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.append(self._list)
        self._add_btn = Gtk.Button(label="Add another colour")
        self._add_btn.add_css_class("flat")
        self._add_btn.connect("clicked", self._on_add)
        self.append(self._add_btn)

    # -- building ---------------------------------------------------------------

    def _append(self, hexstr: str) -> None:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        btn = Gtk.ColorDialogButton()
        btn.set_dialog(Gtk.ColorDialog())
        btn.set_rgba(hex_to_rgba(hexstr))
        btn.connect("notify::rgba", lambda *_a: self._changed())
        remove = Gtk.Button(icon_name="list-remove-symbolic")
        remove.add_css_class("flat")
        remove.set_tooltip_text("Remove this colour")
        remove.connect("clicked", lambda *_a, r=row: self._remove(r))
        click = Gtk.GestureClick()
        click.connect("pressed",
                      lambda _g, _n, _x, _y, r=row: self._select(r))
        row.add_controller(click)
        row.append(btn)
        row.append(remove)
        self._slots.append((row, btn, remove))
        self._list.append(row)
        self._refresh()

    def _remove(self, row: Gtk.Box) -> None:
        if len(self._slots) <= 1:
            return  # always keep at least one colour
        self._slots = [s for s in self._slots if s[0] is not row]
        self._list.remove(row)
        if self._active >= len(self._slots):
            self._active = len(self._slots) - 1
        self._refresh()
        self._changed()

    def _on_add(self, *_a) -> None:
        if len(self._slots) >= self.max_colors:
            return
        self._append("FFFFFF")
        self._active = len(self._slots) - 1
        self._refresh()
        self._changed()

    def _select(self, row: Gtk.Box) -> None:
        for i, (r, _b, _x) in enumerate(self._slots):
            if r is row:
                self._active = i
        self._refresh()

    def _changed(self) -> None:
        if self._on_change:
            self._on_change()

    def _refresh(self) -> None:
        self._add_btn.set_sensitive(len(self._slots) < self.max_colors)
        for i, (row, _b, remove) in enumerate(self._slots):
            row.remove_css_class("slot-active")
            if i == self._active:
                row.add_css_class("slot-active")
            remove.set_sensitive(len(self._slots) > 1)

    # -- interface ---------------------------------------------------------------

    def set_colors(self, colors) -> None:
        for row, _b, _x in list(self._slots):
            self._list.remove(row)
        self._slots = []
        for hx in (colors or []):
            self._append(hx)
        if not self._slots:
            self._append("00FFFF")
        self._active = min(self._active, len(self._slots) - 1)
        self._refresh()

    def get_colors(self) -> list[str]:
        out = []
        for _row, btn, _x in self._slots:
            r, g, b = rgba_to_ints(btn.get_rgba())
            out.append(rgb_to_hex(r, g, b))
        return out

    def set_active_color(self, hexstr: str) -> None:
        if not self._slots:
            return
        self._slots[self._active][1].set_rgba(hex_to_rgba(hexstr))
        self._changed()


class LightingPage(Page):
    """Premium lighting: the keyboard reference is fixed on top, and all
    functions (effect tiles, tuning, apply, profiles) scroll below."""

    def __init__(self, window):
        super().__init__(window, "preferences-color-symbolic", "Lighting",
                         "Per-key RGB: effects, colours and saved profiles")
        self.selected: set[int] = set()
        self.preview_colors: list[str] = ["16161C"] * NUM_ZONES
        self._preview_active = False
        self._preview_timer = None
        self._sweep_i = -1
        self._sweep_backup: list[str] | None = None
        self.state = prof.load_state()
        if self.state.effect not in EFFECT_ORDER:
            self.state.effect = "steady"

        self.build_ui()
        self.refresh_status()
        self.sync_preview_from_state()
        self.connect("map", lambda *_a: self.refresh_status())

    # ------------------------------------------------------------------ UI build

    def build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        main.set_margin_top(10)
        main.set_margin_start(12)
        main.set_margin_end(12)
        main.set_margin_bottom(10)
        self.set_child(main)  # replaces the base clamp: split layout below
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)

        # ---- top half: status + keyboard reference (fixed) -----------------
        main.append(theme.section(
            "Lighting", "Per-key RGB: effects, colours and saved profiles"))
        upper = Adw.Clamp(maximum_size=1240, tightening_threshold=980)
        upper_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        upper.set_child(upper_box)
        main.append(upper)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status_icon = Gtk.Label(label="●")
        self.status_label = Gtk.Label(label="checking device…")
        self.status_label.set_xalign(0.0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        status_row.append(self.status_icon)
        status_row.append(self.status_label)
        upper_box.append(status_row)

        card = Adw.Bin()
        card.add_css_class("kb-card")
        self.kbview = KeyboardView(on_zone_clicked=self.on_zone_clicked)
        kb_scroll = Gtk.ScrolledWindow()
        kb_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        kb_scroll.set_margin_top(10)
        kb_scroll.set_margin_bottom(10)
        kb_scroll.set_margin_start(10)
        kb_scroll.set_margin_end(10)
        kb_scroll.set_child(self.kbview)
        card.set_child(kb_scroll)
        upper_box.append(card)

        lg_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        lg_row.set_halign(Gtk.Align.CENTER)
        sel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in [("Select all", self.on_select_all),
                               ("Clear", self.on_clear_selection)]:
            b = Gtk.Button(label=label)
            b.add_css_class("flat")
            b.connect("clicked", handler)
            sel_row.append(b)
        lg_row.append(sel_row)
        self.sweep_btn = Gtk.Button(label="Zone scan (identify)")
        self.sweep_btn.set_tooltip_text(
            "Lights zones one by one so you can map LEDs to the table")
        self.sweep_btn.add_css_class("flat")
        self.sweep_btn.connect("clicked", self.on_sweep)
        lg_row.append(self.sweep_btn)
        note = Gtk.Label(
            label="zones approximate — run the scan to verify")
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        lg_row.append(note)
        upper_box.append(lg_row)

        # ---- bottom half: scrolling functions ------------------------------
        bottom = Gtk.ScrolledWindow()
        bottom.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        bottom.set_vexpand(True)
        bottom_clamp = Adw.Clamp(maximum_size=1240, tightening_threshold=980)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        col.set_margin_top(4)
        col.set_margin_bottom(24)
        col.set_margin_start(6)
        col.set_margin_end(6)
        bottom_clamp.set_child(col)
        bottom.set_child(bottom_clamp)
        main.append(bottom)

        # Effects ------------------------------------------------------------ 
        g_tiles = Adw.PreferencesGroup(
            title="Effects",
            description="Pick a look — its tuning appears underneath.")
        col.append(g_tiles)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(6)
        flow.set_max_children_per_line(6)
        flow.set_row_spacing(8)
        flow.set_column_spacing(8)
        flow.set_halign(Gtk.Align.CENTER)
        self.tiles: dict[str, EffectTile] = {}
        for key in EFFECT_ORDER:
            tile = EffectTile(key, self)
            self.tiles[key] = tile
            flow.append(tile)
        g_tiles.add(flow)

        # Tuning -------------------------------------------------------------
        self.tuning_group = Adw.PreferencesGroup(
            title=f"Tuning · {EFFECT_NAMES[self.state.effect]}")
        col.append(self.tuning_group)

        self.color_mode_row = Adw.ComboRow(title="Color mode")
        self.color_mode_row.set_model(
            Gtk.StringList.new(["Rainbow", "Customized"]))
        self.color_mode_row.connect("notify::selected",
                                    self.on_color_mode_changed)
        self.tuning_group.add(self.color_mode_row)

        self.colors_row = Adw.ActionRow(title="Colours")
        self.colors_row.set_subtitle(
            "The effect cycles / blends through these (max 5)")
        self.colors_editor = ColorSlots(on_change=self.on_colors_changed)
        self.colors_row.add_suffix(self.colors_editor)
        self.tuning_group.add(self.colors_row)

        palette = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for row_ in SWATCH_ROWS:
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for name, hexstr in row_:
                b = Gtk.Button()
                b.add_css_class("swatch")
                b.add_css_class(f"swatch-{hexstr.lower()}")
                b.set_tooltip_text(f"#{hexstr}  {name}")
                b.connect("clicked", self.on_swatch, hexstr)
                h.append(b)
            palette.append(h)
        self.tuning_group.add(palette)
        self.palette_box = palette

        self.msi_speed_row = Adw.ComboRow(title="Speed")
        self.msi_speed_row.set_model(
            Gtk.StringList.new(list(effects.SPEED_LABELS)))
        self.tuning_group.add(self.msi_speed_row)

        self.msi_dir_row = Adw.ComboRow(title="Direction")
        self.tuning_group.add(self.msi_dir_row)

        self.speed_row = Adw.SpinRow.new_with_range(3.0, 12.0, 0.5)
        self.speed_row.set_title(SPEED_TITLES.get(self.state.effect,
                                                  "Cycle length (seconds)"))
        self.speed_row.set_value(self.state.speed)
        self.tuning_group.add(self.speed_row)

        self.dir_row = Adw.SwitchRow(title="Direction: left → right",
                                     subtitle="Off = right → left")
        self.dir_row.set_active(self.state.direction == 1)
        self.tuning_group.add(self.dir_row)

        self.apply_btn_row = Adw.SwitchRow(
            title="Apply to whole keyboard",
            subtitle="Off = only the selected zones change (Steady only)")
        self.apply_btn_row.set_active(True)
        self.tuning_group.add(self.apply_btn_row)

        # Customize (MSI per-zone styles) ------------------------------------
        self.custom_group = Adw.PreferencesGroup(
            title="Customize · per-zone styles",
            description="Select zones on the keyboard, choose a look, then "
                        "Assign. Each zone keeps its own effect — press "
                        "Apply to send the map.")
        col.append(self.custom_group)

        self.custom_effect_row = Adw.ComboRow(title="Zone effect")
        self.custom_effect_row.set_model(Gtk.StringList.new(
            [EFFECT_NAMES[k] for k in CUSTOM_EFFECTS]))
        self.custom_effect_row.set_selected(CUSTOM_EFFECTS.index("steady"))
        self.custom_effect_row.connect("notify::selected",
                                       self.on_custom_tuning)
        self.custom_group.add(self.custom_effect_row)

        self.custom_colors_row = Adw.ActionRow(title="Zone colours")
        self.custom_colors = ColorSlots()
        self.custom_group.add(self.custom_colors_row)
        self.custom_colors_row.add_suffix(self.custom_colors)

        self.custom_speed_row = Adw.ComboRow(title="Zone speed")
        self.custom_speed_row.set_model(
            Gtk.StringList.new(list(effects.SPEED_LABELS)))
        self.custom_speed_row.set_selected(1)
        self.custom_group.add(self.custom_speed_row)

        self.custom_dir_row = Adw.ComboRow(title="Wave direction")
        self.custom_dir_row.set_model(Gtk.StringList.new(["Left", "Right"]))
        self.custom_dir_row.set_selected(1)
        self.custom_group.add(self.custom_dir_row)

        cacts = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        cacts.set_halign(Gtk.Align.CENTER)
        for label, handler in [("Assign to selected zones",
                                self.on_custom_assign),
                               ("Off · selected zones", self.on_custom_off)]:
            b = Gtk.Button(label=label)
            b.add_css_class("pill")
            b.connect("clicked", handler)
            cacts.append(b)
        self.custom_group.add(cacts)

        # Apply --------------------------------------------------------------
        g_apply = Adw.PreferencesGroup(title="Apply")
        col.append(g_apply)
        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        actions.set_halign(Gtk.Align.CENTER)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("pill")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        off_btn = Gtk.Button(label="Turn Off")
        off_btn.add_css_class("pill")
        off_btn.connect("clicked", self.on_off)
        save_btn = Gtk.Button(label="Save to keyboard")
        save_btn.add_css_class("pill")
        save_btn.set_tooltip_text(
            "Persist the current lighting into the keyboard's memory "
            "(MSI Center's Save — the look survives a restart)")
        save_btn.connect("clicked", self.on_mcu_save)
        restore_btn = Gtk.Button(label="Restore saved")
        restore_btn.add_css_class("pill")
        restore_btn.set_tooltip_text(
            "Reload the lighting stored in the keyboard's memory "
            "(MSI Center's Cancel)")
        restore_btn.connect("clicked", self.on_mcu_restore)
        for b in (self.apply_btn, off_btn, save_btn, restore_btn):
            actions.append(b)
        g_apply.add(actions)

        self.autostart_row = Adw.SwitchRow(
            title="Apply this look at login",
            subtitle="Installs an autostart entry that re-runs "
                     "`mystic-center lighting apply`",
        )
        self.autostart_row.set_active(self.autostart_installed())
        self.autostart_row.connect("notify::active", self.on_autostart_toggled)
        g_apply.add(self.autostart_row)

        # Profiles -----------------------------------------------------------
        group3 = Adw.PreferencesGroup(
            title="Profiles",
            description="Your saved looks — nothing is predefined. "
                        "Use 'Save current as…' to create one.")
        col.append(group3)

        self.profile_names: list[str] = []
        self.profile_row = Adw.ComboRow(title="Profile")
        group3.add(self.profile_row)
        self.refresh_profiles()

        prof_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        prof_btns.set_halign(Gtk.Align.CENTER)
        for label, handler in [("Apply selected", self.on_apply_profile),
                               ("Save current as…", self.on_save_profile),
                               ("Delete", self.on_delete_profile)]:
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            prof_btns.append(b)
        group3.add(prof_btns)

        self.setup_css()
        self._sync_effect_ui()

    # ------------------------------------------------------------------- helpers

    @staticmethod
    def _rgba(hexstr: str) -> Gdk.RGBA:
        return hex_to_rgba(hexstr)

    @staticmethod
    def _rgba_ints(rgba: Gdk.RGBA) -> tuple[int, int, int]:
        return rgba_to_ints(rgba)

    def setup_css(self) -> None:
        self.provider = Gtk.CssProvider()
        self.style_full()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def style_full(self) -> None:
        css = [BASE_CSS, PAGE_CSS]
        for row in SWATCH_ROWS:
            for _, hx in row:
                r, g, b = hex_to_rgb(hx)
                css.append(
                    f".swatch-{hx.lower()} {{ background-color: rgb({r},{g},{b}); }}")
        self.provider.load_from_string("\n".join(css))
        if getattr(self, "kbview", None) is not None:
            self.kbview.set_colors(self.preview_colors)

    def _sync_effect_ui(self) -> None:
        """Selected-tile ring, dynamic titles, and relevant tuning rows."""
        self._syncing = True
        try:
            eff = self.state.effect
            show = set(TUNING.get(eff, ()))
            msi = "msi" in show
            customized = self.state.color_mode == "customized"
            if not self.state.colors:
                self.state.colors = [self.state.color.lstrip("#").upper()]
            self.color_mode_row.set_selected(
                0 if self.state.color_mode == "rainbow" else 1)
            self.color_mode_row.set_visible(msi)
            self.colors_row.set_visible(msi and customized)
            self.palette_box.set_visible(msi and customized)
            self.colors_editor.set_colors(self.state.colors)
            self.msi_speed_row.set_visible("msi_speed" in show)
            self.msi_speed_row.set_selected(self.state.speed_index)
            if "wave_dir" in show:
                self.msi_dir_row.set_title("Wave direction")
                self.msi_dir_row.set_model(
                    Gtk.StringList.new(["Left", "Right"]))
                self.msi_dir_row.set_selected(
                    0 if self.state.direction == effects.DIR_LEFT else 1)
                self.msi_dir_row.set_visible(True)
            elif "wheel_dir" in show:
                self.msi_dir_row.set_title("Direction")
                self.msi_dir_row.set_model(
                    Gtk.StringList.new(["Clockwise", "Counterclockwise"]))
                self.msi_dir_row.set_selected(
                    0 if self.state.direction == effects.DIR_CW else 1)
                self.msi_dir_row.set_visible(True)
            else:
                self.msi_dir_row.set_visible(False)
            self.speed_row.set_visible("speed" in show)
            self.speed_row.set_title(SPEED_TITLES.get(
                eff, "Cycle length (seconds)"))
            self.speed_row.set_value(self.state.speed)
            self.dir_row.set_visible("direction" in show)
            self.dir_row.set_active(self.state.direction != 0)
            self.apply_btn_row.set_visible(eff == "steady")
            self.custom_group.set_visible(eff == "custom")
            if eff == "custom":
                self._sync_custom_ui()
            self.tuning_group.set_title(f"Tuning · {EFFECT_NAMES[eff]}")
            for key, tile in self.tiles.items():
                if key == eff:
                    tile.add_css_class("active")
                else:
                    tile.remove_css_class("active")
        finally:
            self._syncing = False

    def _sync_custom_ui(self) -> None:
        e = CUSTOM_EFFECTS[self.custom_effect_row.get_selected()]
        self.custom_dir_row.set_visible(e == "wave")
        self.custom_speed_row.set_visible(
            e in ("breath", "cycle", "wave", "dance"))
        self.custom_colors_row.set_visible(e != "off")

    def refresh_profiles(self) -> None:
        self.user_profiles = prof.load_profiles()
        self.profile_names = sorted(self.user_profiles)
        self.profile_row.set_model(Gtk.StringList.new(self.profile_names))

    def refresh_status(self) -> None:
        node = find_hidraw()
        for c in ("error", "warning", "success"):
            self.status_icon.remove_css_class(c)
        if node is None:
            self.status_icon.add_css_class("error")
            self.status_label.set_label(
                "MSI keyboard not found (check lsusb shows 1462:1603 MysticLight)")
            return
        try:
            with open(node):
                pass
            self.status_icon.add_css_class("success")
            self.status_label.set_label(f"attached — {node}")
        except PermissionError:
            self.status_icon.add_css_class("warning")
            self.status_label.set_label(
                f"{node} found, but not accessible to this user. "
                "Run `sudo ./install.sh --system` once, then reopen the app.")
        except OSError as e:
            self.status_icon.add_css_class("error")
            self.status_label.set_label(f"{node}: {e}")

    def _sample_preview_colors(self) -> list[str]:
        """Representative frame for the current effect (static snapshot)."""
        st = self.state
        eff = st.effect
        if eff == "off":
            return ["16161C"] * NUM_ZONES
        if eff == "steady":
            if st.color_mode == "rainbow":
                return list(effects.zone_rainbow_colors())
            return [st.zone_colors.get(str(z), st.effective_colors()[0])
                    .lstrip("#") for z in range(NUM_ZONES)]
        if eff == "breath":
            if st.color_mode == "rainbow":
                return [effects.dim_hex(c, 0.45)
                        for c in effects.zone_rainbow_colors()]
            return [effects.dim_hex(st.effective_colors()[0], 0.45)] * NUM_ZONES
        if eff in ("wave", "cycle", "wheel", "bouncy"):
            return self._msi_band_preview(eff)
        if eff == "dance":
            from .lighting import animate
            try:
                eng = animate.get_engine("scycle", speed=st.speed,
                                         direction=1, colors=None)
                return [c.lstrip("#") for c in eng.colors(
                    max(0.25, st.speed * 0.31))]
            except KeyError:
                return [effects.dim_hex(c, 0.5)
                        for c in effects.zone_rainbow_colors()]
        if eff == "custom":
            return [self._custom_zone_preview(z) for z in range(NUM_ZONES)]
        if eff == "gaming":
            base = [GAMING_BASE.lstrip("#")] * NUM_ZONES
            for z in GAMING_RED_ZONES:
                base[z] = GAMING_RED.lstrip("#")
            return base
        # software engine sample at a mid-animation instant
        from .lighting import animate
        try:
            eng = animate.get_engine(eff, speed=st.speed,
                                     direction=st.direction,
                                     colors=getattr(st, "band_colors", None))
        except KeyError:
            # legacy/unknown effect name — fall back to the steady preview
            return [st.zone_colors.get(str(z), st.color).lstrip("#")
                    for z in range(NUM_ZONES)]
        if isinstance(eng, (animate.Rain, animate.Lightning)):
            t = 0.0
            for _ in range(7):  # let the state build up a few events
                eng.colors(t)
                t += 0.22
            return [c.lstrip("#") for c in eng.colors(t)]
        return [c.lstrip("#") for c in eng.colors(max(0.25, st.speed * 0.31))]

    def _msi_band_preview(self, eff: str) -> list[str]:
        """4-column snapshot for the MSI band effects (wave/cycle/wheel/…)."""
        st = self.state
        mode = st.color_mode
        colors = st.effective_colors()
        if eff == "bouncy":
            if mode == "rainbow":
                ball = effects.sample_balls(effects.RAINBOW_STREAM, 50)
            else:
                ball = effects.sample_balls(effects.balls_bouncy(colors), 50)
            return [effects.dim_hex(ball, 0.6)] * NUM_ZONES
        balls = effects.balls_for_effect(eff, mode, colors)
        cols = [effects.sample_balls(balls, (c + 0.5) * 25) for c in range(4)]
        if eff == "wave" and st.direction == effects.DIR_LEFT:
            cols.reverse()
        if eff == "wheel":
            cols = [effects.dim_hex(c, 0.65) for c in cols]
        return [cols[z % 4] for z in range(NUM_ZONES)]

    def _custom_zone_preview(self, z: int) -> str:
        st = self.state
        style = st.zone_styles.get(str(z))
        if not style:
            return "16161C"
        e = style.get("effect", "steady")
        if e == "off":
            return "16161C"
        cols = [c.lstrip("#") for c in style.get("colors") or []]
        if not cols:
            cols = st.effective_colors()
        c = cols[0]
        if e == "breath":
            return effects.dim_hex(c, 0.45)
        if e in ("cycle", "wave"):
            return effects.sample_balls(effects.balls_default(cols),
                                        (z % 4 + 0.5) * 25)
        if e == "dance":
            return "FF00FF"
        return c

    def sync_preview_from_state(self) -> None:
        self.preview_colors = self._sample_preview_colors()
        self.kbview.set_selected(self.selected)
        self.style_full()

    # -------------------------------------------------------------------- events

    def on_zone_clicked(self, zone: int) -> None:
        if zone in self.selected:
            self.selected.discard(zone)
        else:
            self.selected.add(zone)
        self.kbview.set_zone_selected(zone, zone in self.selected)

    def on_effect_tile(self, tile: EffectTile) -> None:
        self.state.effect = tile.effect_key
        self._sync_effect_ui()

    def on_select_all(self, *_a) -> None:
        self.selected = set(range(NUM_ZONES))
        self.kbview.set_selected(self.selected)

    def on_clear_selection(self, *_a) -> None:
        self.selected.clear()
        self.kbview.set_selected(set())

    def on_swatch(self, _btn, hexstr: str) -> None:
        """Palette click — recolour the active slot of the visible editor."""
        if self.state.effect == "custom":
            self.custom_colors.set_active_color(hexstr)
        elif self.colors_row.get_visible():
            self.colors_editor.set_active_color(hexstr)

    def on_color_mode_changed(self, *_a) -> None:
        if getattr(self, "_syncing", False):
            return
        self.state.color_mode = (
            "rainbow" if self.color_mode_row.get_selected() == 0
            else "customized")
        self._sync_effect_ui()
        self.sync_preview_from_state()

    def on_colors_changed(self) -> None:
        if getattr(self, "_syncing", False):
            return
        cs = [c.upper().lstrip("#") for c in self.colors_editor.get_colors()]
        if cs:
            self.state.colors = cs
            self.state.color = "#" + cs[0]
        self.sync_preview_from_state()

    def on_custom_tuning(self, *_a) -> None:
        if getattr(self, "_syncing", False):
            return
        self._sync_custom_ui()

    def on_custom_assign(self, *_a) -> None:
        st = self.state
        st.effect = "custom"
        sel = sorted(self.selected) or list(range(NUM_ZONES))
        e = CUSTOM_EFFECTS[self.custom_effect_row.get_selected()]
        style: dict = {
            "effect": e,
            "speed_index": self.custom_speed_row.get_selected(),
            "colors": [c.upper().lstrip("#")
                       for c in self.custom_colors.get_colors()],
        }
        if e == "wave":
            style["direction"] = (
                effects.DIR_LEFT if self.custom_dir_row.get_selected() == 0
                else effects.DIR_RIGHT)
        for z in sel:
            st.zone_styles[str(z)] = dict(style)
        self._sync_effect_ui()
        self.sync_preview_from_state()
        self.window.toast(
            f"{EFFECT_NAMES[e]} assigned to {len(sel)} zone(s) — press Apply")

    def on_custom_off(self, *_a) -> None:
        st = self.state
        st.effect = "custom"
        sel = sorted(self.selected) or list(range(NUM_ZONES))
        for z in sel:
            st.zone_styles[str(z)] = {"effect": "off"}
        self._sync_effect_ui()
        self.sync_preview_from_state()
        self.window.toast(
            f"zones {','.join(map(str, sel))} → off — press Apply")

    def on_mcu_save(self, *_a) -> None:
        kb = self._current_machine()
        if kb is None:
            return
        try:
            kb.mcu_save()
        except Exception as e:
            self.window.toast(f"save failed: {e}")
            return
        self.window.toast("lighting saved into the keyboard's memory")

    def on_mcu_restore(self, *_a) -> None:
        kb = self._current_machine()
        if kb is None:
            return
        try:
            kb.mcu_cancel()
        except Exception as e:
            self.window.toast(f"restore failed: {e}")
            return
        self.window.toast("keyboard lighting restored from its memory")

    def _current_machine(self) -> MysticKeyboard | None:
        try:
            kb = MysticKeyboard()
            kb.hid.ensure_open()
            return kb
        except Exception as e:
            self.window.toast(str(e))
            return None

    def _collect_state(self) -> None:
        st = self.state
        show = set(TUNING.get(st.effect, ()))
        if "colors" in show:
            cs = [c.upper().lstrip("#")
                  for c in self.colors_editor.get_colors()]
            if cs:
                st.colors = cs
                st.color = "#" + cs[0]
        if "msi" in show:
            st.color_mode = (
                "rainbow" if self.color_mode_row.get_selected() == 0
                else "customized")
        if "msi_speed" in show:
            st.speed_index = self.msi_speed_row.get_selected()
        if "wave_dir" in show:
            st.direction = (
                effects.DIR_LEFT if self.msi_dir_row.get_selected() == 0
                else effects.DIR_RIGHT)
        elif "wheel_dir" in show:
            st.direction = (
                effects.DIR_CW if self.msi_dir_row.get_selected() == 0
                else effects.DIR_CCW)
        if "speed" in show:
            st.speed = self.speed_row.get_value()
        if "direction" in show:
            st.direction = 1 if self.dir_row.get_active() else 0

    def on_apply(self, *_a) -> None:
        self._collect_state()
        state = self.state
        from .lighting import daemon as daemon_ctl

        if state.effect == "rainbow":
            # Firmware rainbow conveyor: one burst of per-zone colour cycles,
            # phase-offset by band slot. Up to 5 random colours march across
            # the deck, one per zone-column; a colour only enters when the
            # leading colour has fully exited. Fresh randomness every Apply.
            kb = self._current_machine()
            if kb is None:
                return
            daemon_ctl.stop()
            self._stop_preview_timer()
            try:
                state.band_colors = effects.random_band_colors()
                kb.rainbow(state.band_colors,
                           speed=int(round(state.speed * 100)),
                           direction=state.direction)
            except Exception as e:
                self.window.toast(f"apply failed: {e}")
                return
            prof.save_state(state)
            self._preview_engine = None
            try:
                from .lighting.animate import get_engine
                self._preview_engine = get_engine(
                    "rainbow", speed=state.speed, direction=state.direction,
                    colors=state.band_colors)
            except KeyError:
                pass
            self._preview_t0 = GLib.get_monotonic_time() / 1e6
            self._start_preview_timer()
            self.sync_preview_from_state()
            self.window.toast("applied — Rainbow (runs on the keyboard itself)")
            return

        if state.effect in SOFTWARE:
            daemon_ctl.stop()
            try:
                daemon_ctl.start_detached(state.effect, state.speed,
                                          state.direction)
            except Exception as e:
                self.window.toast(f"renderer failed: {e}")
                return
            prof.save_state(state)
            self._stop_preview_timer()
            self._preview_engine = None
            try:
                from .lighting.animate import get_engine
                self._preview_engine = get_engine(
                    state.effect, speed=state.speed, direction=state.direction)
            except KeyError:
                pass
            self._preview_t0 = GLib.get_monotonic_time() / 1e6
            self._start_preview_timer()
            self.sync_preview_from_state()
            self.window.toast(f"animating — {EFFECT_NAMES[state.effect]}")
            return

        kb = self._current_machine()
        if kb is None:
            return
        daemon_ctl.stop()
        self._stop_preview_timer()
        try:
            if state.effect == "steady":
                whole = self.apply_btn_row.get_active()
                target = list(range(NUM_ZONES)) if (whole or not self.selected) \
                    else sorted(self.selected)
                if self.selected and not whole:
                    for z in target:
                        state.zone_colors[str(z)] = state.color.lstrip("#")
                state.apply_to(kb, zone_target=target)
            else:
                state.apply_to(kb)  # breath/cycle hardware, gaming static, off
        except Exception as e:
            self.window.toast(f"apply failed: {e}")
            return
        prof.save_state(state)
        self.sync_preview_from_state()
        self.window.toast(
            f"applied — {EFFECT_NAMES[state.effect]}"
            + ((" · zones " + ",".join(map(str, sorted(self.selected))))
               if (state.effect == "steady" and self.selected
                   and not self.apply_btn_row.get_active()) else ""))

    def on_off(self, *_a) -> None:
        from .lighting import daemon as daemon_ctl
        daemon_ctl.stop()
        self._stop_preview_timer()
        kb = self._current_machine()
        if kb is None:
            return
        try:
            kb.off()
        except Exception as e:
            self.window.toast(f"off failed: {e}")
            return
        self._collect_state()
        self.state.effect = "off"
        self.state.zone_colors = {str(z): "#000000" for z in range(NUM_ZONES)}
        prof.save_state(self.state)
        self.sync_preview_from_state()
        self._sync_effect_ui()
        self.window.toast("lights off")

    # ---- local preview animation -----------------------------------------------

    def _start_preview_timer(self) -> None:
        self._preview_active = True
        self._preview_t0 = getattr(self, "_preview_t0",
                                   GLib.get_monotonic_time() / 1e6)
        if not getattr(self, "_preview_timer", None):
            self._preview_timer = GLib.timeout_add(120, self._preview_tick)

    def _stop_preview_timer(self) -> None:
        self._preview_active = False
        t = getattr(self, "_preview_timer", None)
        if t:
            GLib.source_remove(t)
            self._preview_timer = None

    def _preview_tick(self) -> bool:
        if not getattr(self, "_preview_active", False):
            return False
        eng = getattr(self, "_preview_engine", None)
        if eng is None:
            return False
        t = GLib.get_monotonic_time() / 1e6 - self._preview_t0
        self.preview_colors = [c.lstrip("#") for c in eng.colors(t)]
        self.style_full()
        return True

    def on_sweep(self, *_a) -> None:
        kb = self._current_machine()
        if kb is None:
            return
        from .lighting import daemon as daemon_ctl
        daemon_ctl.stop()
        self._stop_preview_timer()
        if self._sweep_i >= 0:
            return  # already sweeping
        self._sweep_backup = list(self.preview_colors)
        self._sweep_kb = kb
        self._sweep_i = 0
        self.window.toast("sweep running — zones 0..23 light one by one")
        GLib.timeout_add(900, self._sweep_step)

    def _sweep_step(self) -> bool:
        if self._sweep_i >= NUM_ZONES:
            self._sweep_i = -1
            assert self._sweep_backup is not None
            self.preview_colors = self._sweep_backup
            self._sweep_backup = None
            try:
                prof.load_state().apply_to(self._sweep_kb)
            except Exception:
                pass
            self.style_full()
            self.window.toast("sweep done — previous look restored")
            return False
        z = self._sweep_i
        self._sweep_i += 1
        try:
            self._sweep_kb.steady("#FFFFFF", zones=[z])
        except Exception as e:
            self.window.toast(f"sweep failed: {e}")
            self._sweep_i = -1
            return False
        self.preview_colors = ["16161C"] * NUM_ZONES
        self.preview_colors[z] = "FFFFFF"
        self.style_full()
        return True

    # ---- profiles ----------------------------------------------------------------

    def on_apply_profile(self, *_a) -> None:
        idx = self.profile_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or not self.profile_names:
            self.window.toast("no profiles yet — save one first")
            return
        name = self.profile_names[idx]
        st = prof.get_profile(name)
        if st is None:
            self.window.toast(f"profile '{name}' is broken — delete it")
            return
        if st.effect in SOFTWARE:
            st.apply_to(None)  # daemon path — the child opens the device itself
            self._stop_preview_timer()
            self._preview_engine = None
            try:
                from .lighting.animate import get_engine
                self._preview_engine = get_engine(
                    st.effect, speed=st.speed, direction=st.direction)
            except KeyError:
                pass
            self._preview_t0 = GLib.get_monotonic_time() / 1e6
            self._start_preview_timer()
        else:
            kb = self._current_machine()
            if kb is None:
                return
            st.apply_to(kb)
            self._stop_preview_timer()
        prof.save_state(st)
        self.state = st
        self.sync_preview_from_state()
        self._sync_effect_ui()
        self.window.toast(f"profile applied — {name}")

    def on_save_profile(self, *_a) -> None:
        self._collect_state()
        self._show_save_dialog()

    def _show_save_dialog(self) -> None:
        dlg = Adw.Dialog()
        dlg.set_title("Save profile")
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar(show_title=False))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(12)
        box.set_margin_start(12)
        box.set_margin_end(12)
        box.set_margin_bottom(12)

        entry = Adw.EntryRow(title="Profile name")
        group = Adw.PreferencesGroup(title="Save current lighting as a profile")
        group.add(entry)
        box.append(group)

        hb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        hb.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        hb.append(cancel)
        hb.append(save)
        box.append(hb)

        toolbar.set_content(box)
        dlg.set_content_widget(toolbar)

        def do_save(*_a):
            name = entry.get_text().strip()
            if not name:
                entry.add_css_class("error")
                return
            prof.save_profile(name, self.state)
            self.refresh_profiles()
            if name in self.profile_names:
                self.profile_row.set_selected(self.profile_names.index(name))
            self.window.toast(f"saved '{name}'")
            dlg.force_close()

        save.connect("clicked", do_save)
        cancel.connect("clicked", lambda *_a: dlg.force_close())
        entry.connect("entry-activated", do_save)
        dlg.present(self.window)

    def on_delete_profile(self, *_a) -> None:
        idx = self.profile_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or not self.profile_names:
            self.window.toast("pick a profile first")
            return
        name = self.profile_names[idx]
        prof.delete_profile(name)
        self.refresh_profiles()
        self.window.toast(f"deleted '{name}'")

    # ---- autostart -------------------------------------------------------------

    @staticmethod
    def autostart_path() -> str:
        return os.path.expanduser("~/.config/autostart/mystic-center-light.desktop")

    @classmethod
    def autostart_installed(cls) -> bool:
        return os.path.exists(cls.autostart_path())

    def on_autostart_toggled(self, row: Adw.SwitchRow, *_a) -> None:
        path = self.autostart_path()
        if row.get_active():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            exec_path = shutil.which("mystic-center") or os.path.join(
                os.path.expanduser("~/.local/bin"), "mystic-center"
            )
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(AUTOSTART_DESKTOP.format(exec_path=exec_path))
            self.window.toast("autostart entry installed")
        else:
            try:
                os.unlink(path)
                self.window.toast("autostart entry removed")
            except FileNotFoundError:
                pass

    def reachability(self, ok, why) -> None:
        pass

# ================================================================== settings

class SettingsPage(Page):
    def __init__(self, window):
        super().__init__(window, "emblem-system-symbolic", "Settings",
                         "Service, appearance and diagnostics")

        # ---- appearance ----------------------------------------------------
        look = theme.card("Appearance", "Premium dark by default, light on demand",
                          "applications-graphics-symbolic")
        self.col.append(look)
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(theme.icon("weather-clear-night-symbolic", 18,
                              "mc-card-icon"))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(theme.label("Dark interface", "mc-card-title"))
        text.append(theme.label("Switch to the light palette",
                                "mc-card-sub"))
        row.append(text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        self.dark_switch = Gtk.Switch()
        self.dark_switch.set_valign(Gtk.Align.CENTER)
        self.dark_switch.set_active(theme.is_dark())
        self.dark_switch.connect("notify::active", self.on_theme)
        row.append(self.dark_switch)
        look.body.append(row)

        # ---- daemon --------------------------------------------------------
        svc = theme.card("Privileged service",
                         "mystic-center.service owns EC, UEFI and WMI access",
                         "emblem-system-symbolic")
        self.col.append(svc)
        self.svc_dot = theme.label("●", "mc-dot-ok")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        row.append(self.svc_dot)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(theme.label("Service status", "mc-card-title"))
        self.svc_state = theme.label("checking…", "mc-card-sub")
        text.append(self.svc_state)
        row.append(text)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        row.append(spacer)
        check = Gtk.Button(label="Check now")
        check.add_css_class("mc-ghost")
        check.connect("clicked", self.on_check)
        row.append(check)
        self.restart_svc = Gtk.Button(label="Restart")
        self.restart_svc.add_css_class("mc-ghost")
        self.restart_svc.connect("clicked", self.on_restart)
        row.append(self.restart_svc)
        svc.body.append(row)

        # ---- about ---------------------------------------------------------
        about = theme.card("About", None, "help-about-symbolic")
        self.col.append(about)
        self.about_kv = {}
        for key, val in (("Application", APP_NAME),
                         ("Interface", "GTK4 · libadwaita"),
                         ("Machine", MACHINE_NAME),
                         ("Board", MACHINE_BOARD or "—"),
                         ("BIOS", MACHINE_BIOS or "—"),
                         ("Kernel", os.uname().release)):
            row, value = theme.key_value(key, val)
            about.body.append(row)
            self.about_kv[key] = value

        link = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        about_label = theme.label(
            "Documentation and source live in the install directory "
            "(README.md, docs/)", "mc-card-sub", wrap=True)
        about_label.set_hexpand(True)
        link.append(about_label)
        diag = Gtk.Button(label="Copy diagnostics")
        diag.add_css_class("mc-ghost")
        diag.connect("clicked", self.on_diag)
        link.append(diag)
        about.body.append(link)

    def on_theme(self, sw, *_a):
        theme.set_scheme(sw.get_active())
        self.window.toast("Light theme" if not sw.get_active()
                          else "Dark theme")

    def on_check(self, *_a):
        out = _sysctl("is-active")
        self.svc_state.set_label(f"systemctl: {out or 'unknown'}")
        self.window.toast(f"service is {out or 'unknown'}")

    def on_restart(self, *_a):
        import subprocess
        try:
            subprocess.Popen(["pkexec", "systemctl", "restart", "mystic-center"],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            self.window.toast("restart requested — password dialog may appear")
        except OSError as e:
            self.window.toast(f"could not spawn pkexec: {e}")

    def on_diag(self, *_a):
        import json
        snap = getattr(self.window, "_last_snap", {}) or {}
        text = json.dumps(snap, indent=2, default=str)
        self.get_clipboard().set(text)
        self.window.toast("diagnostics copied to the clipboard")

    def refresh(self, snap: dict) -> None:
        pass

    def reachability(self, ok, why) -> None:
        self.svc_state.set_label(f"service: {'active' if ok else 'unreachable — ' + why}")
        self.svc_dot.remove_css_class("mc-dot-ok")
        self.svc_dot.remove_css_class("mc-dot-err")
        self.svc_dot.add_css_class("mc-dot-ok" if ok else "mc-dot-err")


def _sysctl(*args) -> str:
    from .util import run_cmd
    out = run_cmd(["systemctl", "is-active", "mystic-center"], timeout_s=3.0)
    return (out or "").strip() or _raw_is_active()


def _raw_is_active() -> str:
    from .util import run_cmd
    out = run_cmd(["systemctl", "is-active", "mystic-center"], timeout_s=3.0)
    return (out or "").strip()


# ===================================================================== window

NAV_ITEMS = [
    ("control", "CONTROL CENTER", [
        ("dashboard", "Dashboard", "view-grid-symbolic"),
        ("perf", "Performance", "power-profile-performance-symbolic"),
        ("fans", "Fans", "weather-windy-symbolic"),
        ("battery", "Battery", "battery-symbolic"),
        ("gpu", "Graphics", "video-display-symbolic"),
        ("lighting", "Lighting", "preferences-color-symbolic"),
    ]),
    ("system", "SYSTEM", [
        ("settings", "Settings", "emblem-system-symbolic"),
    ]),
]


class NavButton(Gtk.ToggleButton):
    def __init__(self, icon_name: str, title: str) -> None:
        super().__init__()
        self.add_css_class("mc-nav")
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        box.append(theme.icon(icon_name, 17))
        box.append(theme.label(title))
        self.set_child(box)


def _app_icon_widget(size: int = 26) -> Gtk.Widget:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    svg = os.path.join(here, "data", "mystic-center-icon.svg")
    if os.path.exists(svg):
        img = Gtk.Image.new_from_file(svg)
        img.set_pixel_size(size)
        return img
    return theme.icon("applications-system-symbolic", size, "mc-card-icon")


class CenterWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1320, 860)
        self.set_size_request(1040, 720)
        self.add_css_class("mc-window")

        app_obj = self.get_application()
        self.install_action("win.toggle-fullscreen", None,
                            self._toggle_fullscreen)
        if app_obj is not None:
            app_obj.set_accels_for_action("win.toggle-fullscreen", ["F11"])

        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        toolbar.set_content(self.toast_overlay)
        self.set_content(toolbar)

        # ---- header bar ----------------------------------------------------
        header = Adw.HeaderBar()
        header.add_css_class("mc-header")
        header.set_title_widget(Gtk.Box())
        toolbar.add_top_bar(header)

        brand = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        brand.set_margin_start(6)
        brand.append(_app_icon_widget(24))
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        names.append(theme.label(APP_NAME, "mc-header-title"))
        names.append(theme.label(MACHINE_NAME, "mc-brand-sub"))
        brand.append(names)
        header.pack_start(brand)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=7)
        chips.set_margin_end(4)
        self.chip_cpu = self._chip("weather-clear-symbolic", "CPU", "—")
        self.chip_gpu = self._chip("video-display-symbolic", "GPU", "—")
        self.chip_bat = self._chip("battery-symbolic", "", "—")
        for chip in (self.chip_cpu, self.chip_gpu, self.chip_bat):
            chips.append(chip)
        header.pack_end(chips)

        # ---- body ----------------------------------------------------------
        body = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        sidebar.set_size_request(236, -1)
        sidebar.set_margin_top(10)
        sidebar.set_margin_bottom(0)
        sidebar.set_margin_start(10)
        sidebar.set_margin_end(0)

        self.nav: dict[str, NavButton] = {}
        self.stack = Adw.ViewStack()

        for _key, group_title, items in NAV_ITEMS:
            sidebar.append(theme.label(group_title, "mc-nav-section"))
            for name, title, ico in items:
                btn = NavButton(ico, title)
                btn.connect("toggled", self._on_nav, name)
                sidebar.append(btn)
                self.nav[name] = btn

        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        sidebar.append(spacer)

        footer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9)
        footer.add_css_class("mc-sidebar-footer")
        self.svc_dot = theme.label("●", "mc-dot-ok")
        footer.append(self.svc_dot)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.svc_label = theme.label("Connecting…", "mc-brand-sub")
        text.append(self.svc_label)
        footer.append(text)
        sidebar.append(footer)

        # left inset so the nav highlight has breathing room
        side_wrap = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        side_wrap.add_css_class("mc-sidebar")
        side_wrap.set_size_request(236, -1)
        side_wrap.append(sidebar)
        body.append(side_wrap)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_hexpand(True)

        self.banner = Adw.Banner()
        self.banner.button_label = "Retry"
        self.banner.connect("button-clicked", self._retry_now)
        content.append(self.banner)
        content.append(self.stack)
        body.append(content)

        self.toast_overlay.set_child(body)

        # ---- pages ---------------------------------------------------------
        self.pages = [
            OverviewPage(self), PerfPage(self), FansPage(self),
            BatteryPage(self), GpuPage(self), LightingPage(self),
            SettingsPage(self),
        ]
        names = ["dashboard", "perf", "fans", "battery", "gpu", "lighting",
                 "settings"]
        for name, page in zip(names, self.pages):
            self.stack.add_named(page, name)
        self.show_page(os.environ.get("MYSTIC_CENTER_PAGE", "dashboard"))

        # ---- snapshot stream -------------------------------------------------
        self._snap_lock = threading.Lock()
        self._last_snap: dict = {}
        self._thread = threading.Thread(
            target=self._snap_loop, name="snapshot", daemon=True)
        self._thread.start()
        self.connect("close-request", self._on_close)

    @staticmethod
    def _chip(icon_name: str, prefix: str, value: str) -> Gtk.Box:
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chip.add_css_class("mc-chip")
        chip.append(theme.icon(icon_name, 14))
        if prefix:
            chip.append(theme.label(prefix, "mc-chip-label"))
        val = theme.label(value, "mc-chip-value")
        chip.append(val)
        chip.value_label = val  # type: ignore[attr-defined]
        return chip

    # ---- navigation -----------------------------------------------------------

    def _on_nav(self, btn: NavButton, name: str) -> None:
        if not btn.get_active():
            return
        for key, other in self.nav.items():
            if key != name and other.get_active():
                other.handler_block_by_func(self._on_nav)
                other.set_active(False)
                other.handler_unblock_by_func(self._on_nav)
        self.stack.set_visible_child_name(name)

    def show_page(self, name: str) -> None:
        btn = self.nav.get(name)
        if btn is None:
            return
        if not btn.get_active():
            btn.set_active(True)
        self.stack.set_visible_child_name(name)

    # ---- snapshot thread ------------------------------------------------------

    def _snap_loop(self) -> None:
        while True:
            snap = None
            why = ""
            try:
                with daemon_client.Client(timeout=2.5) as c:
                    snap = c.snapshot()
            except Exception as e:
                why = str(e)
                snap = None
            GLib.idle_add(self._apply_snap, snap, why)
            if self._closed:
                return
            import time as _t
            _t.sleep(REFRESH_MS / 1000.0)

    def _apply_snap(self, snap: dict | None, why: str) -> bool:
        self.banner.set_revealed(snap is None)
        if snap is None:
            self.banner.set_title(f"daemon unreachable — {why}")
            self.svc_dot.remove_css_class("mc-dot-ok")
            self.svc_dot.add_css_class("mc-dot-err")
            self.svc_label.set_label("Daemon offline")
            for p in self.pages:
                p.reachability(False, why)
            return False
        self._last_snap = snap
        self.svc_dot.remove_css_class("mc-dot-err")
        self.svc_dot.add_css_class("mc-dot-ok")
        self.svc_label.set_label("Daemon online")

        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        bat = snap.get("battery") or {}
        self.chip_cpu.value_label.set_label(fmt(cpu.get("temp"), "°"))
        self.chip_gpu.value_label.set_label(fmt(gpu.get("temp"), "°"))
        percent = bat.get("percent")
        self.chip_bat.value_label.set_label(
            f"{percent}%" if percent is not None else "—")
        for p in self.pages:
            try:
                p.refresh(snap)
                p.reachability(True, "")
            except Exception as e:  # one page's failure never kills the loop
                print(f"[gui] page refresh error: {e}")
        return False

    def _retry_now(self, *_a) -> None:
        self.banner.set_revealed(False)
        self._apply_snap(None, "manual retry pending")

    def request_snapshot(self) -> None:
        """Force an immediate snapshot refresh (after a slow daemon op)."""
        def _run() -> None:
            try:
                with daemon_client.Client(timeout=5.0) as c:
                    snap = c.snapshot()
            except Exception as e:
                GLib.idle_add(self._apply_snap, None, str(e))
                return
            GLib.idle_add(self._apply_snap, snap, "")
        threading.Thread(target=_run, daemon=True).start()

    def _toggle_fullscreen(self, *_a) -> None:
        if self.is_fullscreen():
            self.unfullscreen()
        else:
            self.fullscreen()

    def _on_close(self, *_a) -> bool:
        self._closed = True
        return False  # allow the close

    def toast(self, text: str) -> None:
        t = Adw.Toast.new(text)
        t.set_timeout(3)
        self.toast_overlay.add_toast(t)

    _closed = False


# ======================================================================= app

class MysticCenterApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )
        self.win: CenterWindow | None = None

    def do_activate(self):
        theme.install(dark=theme.is_dark())
        self.win = CenterWindow(self)
        self.win.present()


def main() -> int:
    app = MysticCenterApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
