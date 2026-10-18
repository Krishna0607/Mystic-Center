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

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from . import client as daemon_client
from .lighting import profiles as prof
from .lighting.effects import gradient, hex_to_rgb, rgb_to_hex
from .lighting.hid import DeviceNotFound, find_hidraw
from .lighting.keyboard import NUM_ZONES, MysticKeyboard

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

ZONE_CSS_TEMPLATE = ".zb{idx} {{ background-color: rgb({r},{g},{b}); }}"

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

    def __init__(self, window: "CenterWindow", icon: str):
        super().__init__(vexpand=True)
        self.window = window
        self.icon_name = icon
        self.clamp = Adw.Clamp(maximum_size=960, tightening_threshold=720)
        self.set_child(self.clamp)
        self.col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.col.set_margin_top(12)
        self.col.set_margin_bottom(24)
        self.clamp.set_child(self.col)

    def refresh(self, snap: dict) -> None:  # pragma: no cover - overridden
        ...

    def reachability(self, ok: bool, why: str) -> None:  # pragma: no cover
        ...


# ================================================================ overview

def _mk_row(title: str = "", subtitle: str = "") -> Adw.ActionRow:
    return Adw.ActionRow(title=title, subtitle=subtitle)


class OverviewPage(Page):
    def __init__(self, window):
        super().__init__(window, "computer-symbolic")

        self.bars: dict[str, tuple[Gtk.LevelBar, Gtk.Label, Gtk.Label, float, float]] = {}

        def add_temp_row(title: str, group: Adw.PreferencesGroup, lo=30.0, hi=95.0):
            row = Adw.ActionRow(title=title)
            value = Gtk.Label(label="—")
            value.add_css_class("monospace")
            value.add_css_class("heading")
            value.set_xalign(1.0)
            row.add_suffix(value)
            bar = Gtk.LevelBar(min_value=lo, max_value=hi)
            bar.set_size_request(110, 6)
            bar.set_valign(Gtk.Align.CENTER)
            row.add_suffix(bar)
            group.add(row)
            self.bars[title] = (bar, value, row, lo, hi)

        g_temp = Adw.PreferencesGroup(title="Temperatures")
        self.col.append(g_temp)
        for t in ("CPU", "GPU", "NVMe"):
            add_temp_row(t, g_temp)

        g_fan = Adw.PreferencesGroup(title="Fans")
        self.col.append(g_fan)
        self.rpm_row = _mk_row("Fan speed", "read via msi_wmi_platform hwmon")
        self.rpm_label = Gtk.Label(label="—")
        self.rpm_label.add_css_class("monospace")
        self.rpm_row.add_suffix(self.rpm_label)
        g_fan.add(self.rpm_row)

        self.duty_row = _mk_row("Fan duty (CPU / GPU)", "feedback from EC")
        self.duty_label = Gtk.Label(label="—")
        self.duty_label.add_css_class("monospace")
        self.duty_row.add_suffix(self.duty_label)
        g_fan.add(self.duty_row)

        g_mem = Adw.PreferencesGroup(title="Memory and storage")
        self.col.append(g_mem)

        def add_meter_row(title: str, group: Adw.PreferencesGroup):
            row = Adw.ActionRow(title=title)
            value = Gtk.Label(label="—")
            value.add_css_class("monospace")
            value.add_css_class("heading")
            value.set_xalign(1.0)
            row.add_suffix(value)
            bar = Gtk.LevelBar(min_value=0, max_value=100)
            bar.set_size_request(110, 6)
            bar.set_valign(Gtk.Align.CENTER)
            row.add_suffix(bar)
            group.add(row)
            return bar, value

        self.ram_bar, self.ram_value = add_meter_row("RAM", g_mem)
        self.disk_bar, self.disk_value = add_meter_row("Storage", g_mem)

        g_bat = Adw.PreferencesGroup(title="Battery")
        self.col.append(g_bat)
        self.bat_row = _mk_row("Charge")
        self.bat_label = Gtk.Label(label="—")
        self.bat_label.add_css_class("monospace")
        self.bat_row.add_suffix(self.bat_label)
        g_bat.add(self.bat_row)

        self.power_row = _mk_row("Now / voltage", "battery current draw")
        self.power_label = Gtk.Label(label="—")
        self.power_label.add_css_class("monospace")
        self.power_row.add_suffix(self.power_label)
        g_bat.add(self.power_row)

        self.limit_row = _mk_row("Charge limit (start / end)")
        self.limit_label = Gtk.Label(label="—")
        self.limit_label.add_css_class("monospace")
        self.limit_row.add_suffix(self.limit_label)
        g_bat.add(self.limit_row)

        g_mode = Adw.PreferencesGroup(title="Mode")
        self.mode_label = Gtk.Label(label="—")
        self.mode_label.add_css_class("monospace")
        mode_row = _mk_row("System / fan mode")
        mode_row.add_suffix(self.mode_label)
        g_mode.add(mode_row)

        self.fw_row = _mk_row("EC firmware")
        fw_label = Gtk.Label(label="—")
        fw_label.add_css_class("monospace")
        self.fw_row.add_suffix(fw_label)
        g_mode.add(self.fw_row)

    def refresh(self, snap: dict) -> None:
        cpu = snap.get("cpu") or {}
        gpu = snap.get("gpu") or {}
        fans = snap.get("fans") or {}
        bat = snap.get("battery") or {}
        temps = snap.get("system_temps") or {}
        system = snap.get("system") or {}

        def setbar(title, value, fmt_suffix="°C"):
            bar, value_label, _row, lo, hi = self.bars[title]
            if value is None:
                value_label.set_label("—")
                bar.set_value(0)
                return
            value_label.set_label(fmt(value, fmt_suffix))
            bar.set_value(float(value) - lo)

        setbar("CPU", cpu.get("temp"))
        setbar("GPU", gpu.get("temp"))
        setbar("NVMe", temps.get("nvme"))

        def meter(bar, label, data):
            if not data:
                label.set_label("—")
                bar.set_value(0)
                return
            label.set_label(
                f"{data.get('used_gb', '—')} / {data.get('total_gb', '—')} GB "
                f"({fmt(data.get('pct'), '%')})")
            bar.set_value(float(data.get("pct") or 0))

        meter(self.ram_bar, self.ram_value, system.get("ram"))
        meter(self.disk_bar, self.disk_value, system.get("storage"))

        rpm = fans.get("rpm")
        self.rpm_label.set_label(
            f"{rpm} RPM" if isinstance(rpm, int) else "—")
        duty = fans.get("duty") or {}
        if duty:
            self.duty_label.set_label(
                f"{duty.get('cpu', '—')}% / {duty.get('gpu', '—')}%")
        else:
            cpu_pct = cpu.get("fan_pct")
            self.duty_label.set_label(
                f"{fmt(cpu_pct, '%')}" if cpu_pct is not None else "—")

        percent = bat.get("percent")
        status = bat.get("status", "")
        glyph = {"Charging": "▲", "Discharging": "▼", "Not charging": "■",
                 "Full": "●"}.get(status, "")
        self.bat_label.set_label(
            f"{glyph} {percent}%  {status}".strip() if percent is not None else "—")
        pw = bat.get("power_w")
        volt = bat.get("voltage")
        cur = bat.get("current")
        self.power_label.set_label(
            f"{fmt(pw, ' W')}  {fmt(volt, ' V', 2)}  {fmt(cur, ' A', 2)}"
            if pw is not None else "—")
        st, end = bat.get("start_threshold"), bat.get("end_threshold")
        self.limit_label.set_label(
            f"{st} / {end}" if st is not None and end is not None else "—")

        modes = snap.get("modes") or {}
        saver = " · 🌙 super saver" if modes.get("super_saver") else ""
        self.mode_label.set_label(
            f"{perf_mode_name(modes.get('shift'))} · fan {modes.get('fan', '—')}"
            + (" · cooler boost" if modes.get("cooler_boost") else "") + saver)
        self.fw_row.set_subtitle(fw(snap.get("fw") or {}))

    def reachability(self, ok, why):
        if not ok:
            self.rpm_label.set_label("daemon down")


def fw(fw_dict) -> str:
    return f"{fw_dict.get('version', '—')} ({fw_dict.get('released', '')})"


# ============================================================== performance

class PerfPage(Page):
    def __init__(self, window):
        super().__init__(window, "power-profile-performance-symbolic")

        g = Adw.PreferencesGroup(
            title="Platform profile (EC shift mode)")
        self.col.append(g)
        row = Adw.ActionRow(title="Mode",
                            subtitle="Eco saves power · Comfort balanced · Turbo peaks")
        self.mode_btns = SegButtons(
            [("eco", "Battery Saver"), ("comfort", "Balanced"),
             ("turbo", "Performance")],
            self.on_mode)
        self.mode_btns.select("comfort")  # placeholder until first snapshot
        self.mode_btns.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self.mode_btns)
        g.add(row)

        self.cooler_row = Adw.SwitchRow(
            title="Cooler Boost",
            subtitle="Max fan rotation momentarily (fans at 100 %)")
        g.add(self.cooler_row)
        self.cooler_row.connect("notify::active", self.on_cooler)

        self.sbat_row = Adw.SwitchRow(
            title="Super Battery (EC power plane)",
            subtitle="Limits one power plane for extra runtime")
        g.add(self.sbat_row)
        self.sbat_row.connect("notify::active", self.on_super_battery)

        self.saver_row = Adw.SwitchRow(
            title="🌙  Super Battery Saver",
            subtitle="Battery Saver mode · power-plane limit · silent fans · "
                     "CPU capped at 2.0 GHz — maximum runtime, reduced power")
        g.add(self.saver_row)
        self.saver_row.connect("notify::active", self.on_saver)

        self.cal_row = Adw.ActionRow(
            title="Turbo code calibration",
            subtitle="Writes each mode and reads back raw EC values "
                     "(needed once on boards reporting turbo as 0xC0)")
        cal_btn = Gtk.Button(label="Run")
        cal_btn.set_valign(Gtk.Align.CENTER)
        cal_btn.connect("clicked", self.on_calibrate)
        cal_btn_row = Adw.ActionRow(title="")
        cal_btn_row.add_suffix(cal_btn)
        g.add(cal_btn_row)

    # ---- handlers are synchronous; daemon ops are fast ------------------------

    def on_mode(self, name: str) -> None:
        try:
            with daemon_client.Client() as c:
                c.request("set_shift_mode", mode=name)
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"mode switch failed: {e}")

    def on_cooler(self, row, *_a):
        try:
            with daemon_client.Client() as c:
                c.request("set_cooler_boost", on=row.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"cooler boost failed: {e}")
            row.set_active(not row.get_active())

    def on_super_battery(self, row, *_a):
        try:
            with daemon_client.Client() as c:
                c.request("set_super_battery", on=row.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"super battery failed: {e}")
            row.set_active(not row.get_active())

    def on_saver(self, row, *_a):
        try:
            with daemon_client.Client() as c:
                res = c.request("set_super_saver", on=row.get_active())
            if res.get("note"):
                self.window.toast(res["note"])
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"super saver failed: {e}")
            row.set_active(not row.get_active())

    def on_calibrate(self, *_a):
        try:
            with daemon_client.Client() as c:
                res = c.request("calibrate_shift_mode")
            self.window.toast(f"calibrated: {res}")
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"calibration failed: {e}")

    def refresh(self, snap: dict) -> None:
        modes = snap.get("modes") or {}
        shift = (modes.get("shift") or "").split(" ")[0].lower()
        if shift in self.mode_btns.buttons:
            self.mode_btns.select(shift)
        cb = self.cooler_row
        if bool(modes.get("cooler_boost")) != cb.get_active():
            cb.handler_block_by_func(self.on_cooler)
            cb.set_active(bool(modes.get("cooler_boost")))
            cb.handler_unblock_by_func(self.on_cooler)
        sb = self.sbat_row
        if bool(modes.get("super_battery")) != sb.get_active():
            sb.handler_block_by_func(self.on_super_battery)
            sb.set_active(bool(modes.get("super_battery")))
            sb.handler_unblock_by_func(self.on_super_battery)
        sv = self.saver_row
        if bool(modes.get("super_saver")) != sv.get_active():
            sv.handler_block_by_func(self.on_saver)
            sv.set_active(bool(modes.get("super_saver")))
            sv.handler_unblock_by_func(self.on_saver)

    def reachability(self, ok, why):
        pass


# ==================================================================== fans

class FansPage(Page):
    def __init__(self, window):
        super().__init__(window, "weather-wind-symbolic")

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

        note = Gtk.Label(label=(
            "Note: this EC firmware (2024 platform interface) does not accept "
            "user fan tables from Linux tools — MSI Center's custom curves on "
            "Windows go through its WMI interface. Auto/Silent + Cooler Boost "
            "cover the practical range here."))
        note.set_xalign(0.0)
        note.set_wrap(True)
        note.add_css_class("caption")
        note.add_css_class("dim-label")
        g2.add(note)

    def on_fan_mode(self, name: str) -> None:
        try:
            with daemon_client.Client() as c:
                c.request("set_fan_preset", preset=name)
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"fan preset failed: {e}")

    def on_boost(self, row, *_a):
        try:
            with daemon_client.Client() as c:
                c.request("set_cooler_boost", on=row.get_active())
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"cooler boost failed: {e}")
            row.set_active(not row.get_active())

    def refresh(self, snap: dict) -> None:
        modes = snap.get("modes") or {}
        fan_mode = (modes.get("fan") or "auto").lower()
        active_radio = fan_mode if fan_mode in self.preset_btns.buttons else "auto"
        self.preset_btns.select(active_radio)

        boost = self.boost_row
        if bool(modes.get("cooler_boost")) != boost.get_active():
            boost.handler_block_by_func(self.on_boost)
            boost.set_active(bool(modes.get("cooler_boost")))
            boost.handler_unblock_by_func(self.on_boost)

        fans = snap.get("fans") or {}
        duty = fans.get("duty") or {}
        rpm = fans.get("rpm")
        self.duty_label.set_label(
            f"{fan_mode} — cpu {fmt(duty.get('cpu'), '%')} / "
            f"gpu {fmt(duty.get('gpu'), '%')}")
        self.rpm_label.set_label(f"{rpm} RPM" if isinstance(rpm, int) else "—")

    def reachability(self, ok, why):
        pass


# ================================================================= battery

class BatteryPage(Page):
    def __init__(self, window):
        super().__init__(window, "battery-symbolic")

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

# Physical-key grouping per zone: 4 zone-columns x 6 zone-rows on the
# Crosshair 16 HX deck (approximate quarter boundaries; the numpad maps into
# the right-most column, row-major zone ordering hardware-verified).
KB_ROWS: list[list[list[str]]] = [
    [["Esc", "F1", "F2", "F3"], ["F4", "F5", "F6", "F7"],
     ["F8", "F9", "F10", "F11"], ["F12", "Del", "PgUp", "PgDn"]],
    [["`", "1", "2", "3", "4"], ["5", "6", "7", "8"],
     ["9", "0", "-", "="], ["Bksp", "Nlk", "/", "*"]],
    [["Tab", "Q", "W", "E", "R"], ["T", "Y", "U", "I"],
     ["O", "P", "[", "]"], ["\\", "7", "8", "-"]],
    [["Caps", "A", "S", "D", "F"], ["G", "H", "J", "K"],
     ["L", ";", "'"], ["⏎", "4", "5", "6"]],
    [["⇧", "Z", "X", "C", "V"], ["B", "N", "M", ","],
     [".", "/", "⇧"], ["1", "2", "3", "+"]],
    [["Ctrl", "Fn", "⊞", "Alt"], ["Space", "Alt"],
     ["Ctrl", "←"], ["↓", "→", "0", "."]],
]

EFFECT_ORDER = ["steady", "breath", "cycle", "wave", "swave", "scycle", "off"]
EFFECT_NAMES = {
    "steady": "Steady",
    "breath": "Breathing",
    "cycle": "Colorful Cycle",
    "wave": "Wave",
    "swave": "Rainbow Wave 24",
    "scycle": "Colorful Wave 24",
    "off": "Off",
}

# tuning rows shown per effect
TUNING = {
    "steady": ("color",),
    "breath": ("color", "speed", "level"),
    "cycle": ("speed", "level"),
    "wave": ("speed", "direction", "level"),
    "swave": ("speed", "direction"),
    "scycle": ("speed",),
    "off": (),
}

LIGHT_KEY_CSS = ".light-key { font-size: 7px; padding: 1px 2px; " \
    "min-width: 0; min-height: 24px; border-radius: 4px;\n" \
    "  border: 1px solid rgba(0,0,0,0.45); }\n" \
    ".light-key.sel { border: 2px solid #9cc7ff; }\n" \
    ".effect-tile { font-size: 9px; }"


class KeyboardLayout(Gtk.Box):
    """24-zone reference drawn as a physical keyboard; keys are clickable
    zone selectors and preview colors live in the .zbN css rules."""

    def __init__(self, page: "LightingPage"):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        self.page = page
        self.key_buttons: dict[int, list[Gtk.Button]] = {}
        for row_idx, row_zones in enumerate(KB_ROWS):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
            row.set_halign(Gtk.Align.CENTER)
            for col_idx, keys in enumerate(row_zones):
                zone = row_idx * 4 + col_idx
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=1)
                box.set_halign(Gtk.Align.CENTER)
                for key in keys:
                    btn = Gtk.Button(label=key)
                    btn.add_css_class("light-key")
                    btn.add_css_class(f"zb{zone}")
                    btn.set_tooltip_text(f"Zone {zone}")
                    btn.connect("clicked", page.on_zone_clicked, zone)
                    self.key_buttons.setdefault(zone, []).append(btn)
                    box.append(btn)
                row.append(box)
            self.append(row)

    def set_zone_selected(self, zone: int, selected: bool) -> None:
        for btn in self.key_buttons.get(zone, []):
            if selected:
                btn.add_css_class("sel")
            else:
                btn.remove_css_class("sel")


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


class LightingPage(Page):
    """Lighting: MSI-Center-style tiles + physical 24-zone keyboard layout."""

    def __init__(self, window):
        super().__init__(window, "preferences-color-symbolic")
        self.clamp.set_maximum_size(1160)

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
        if self.state.effect in ("swave", "scycle", "spulse"):
            from .lighting import daemon as daemon_ctl
            if daemon_ctl.running():
                from .lighting.animate import get_engine
                self._preview_engine = get_engine(
                    self.state.effect, speed=self.state.speed,
                    direction=self.state.direction)
                self._preview_t0 = GLib.get_monotonic_time() / 1e6
                self._start_preview_timer()

    # ------------------------------------------------------------------ UI build

    def build_ui(self) -> None:
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.clamp.set_child(main)

        # left column — keyboard reference + status ------------------------------
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        left.set_size_request(470, -1)
        main.append(left)

        status_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status_icon = Gtk.Label(label="●")
        self.status_label = Gtk.Label(label="checking device…")
        self.status_label.set_xalign(0.0)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("dim-label")
        status_row.append(self.status_icon)
        status_row.append(self.status_label)
        left.append(status_row)

        hint = Gtk.Label(
            label="Physical keyboard — click keys to pick zones, "
                  "colors preview live")
        hint.set_xalign(0.0)
        hint.add_css_class("heading")
        left.append(hint)

        self.kb_layout = KeyboardLayout(self)
        left.append(self.kb_layout)

        sel_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for label, handler in [("Select all", self.on_select_all),
                               ("Clear", self.on_clear_selection)]:
            b = Gtk.Button(label=label)
            b.connect("clicked", handler)
            sel_row.append(b)
        sel_sub = Gtk.Label(label="(approximate column boundaries)")
        sel_sub.add_css_class("caption")
        sel_sub.add_css_class("dim-label")
        sel_row.append(sel_sub)
        left.append(sel_row)

        scan_l = Gtk.Label(label="Zone scan (identify):")
        scan_l.add_css_class("caption")
        scan_l.add_css_class("dim-label")
        self.sweep_btn = Gtk.Button(label="Run")
        self.sweep_btn.set_tooltip_text(
            "Lights zones one by one so you can map LEDs to the table")
        self.sweep_btn.connect("clicked", self.on_sweep)
        scan_l.set_valign(Gtk.Align.END)
        scan_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        scan_row.append(scan_l)
        scan_row.append(self.sweep_btn)
        left.append(scan_row)

        # right column — effect tiles + tuning -----------------------------------
        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        right.set_hexpand(True)
        main.append(right)

        g_tiles = Adw.PreferencesGroup(title="Effect")
        right.append(g_tiles)
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_min_children_per_line(4)
        flow.set_max_children_per_line(4)
        flow.set_row_spacing(6)
        flow.set_column_spacing(6)
        self.tiles: dict[str, EffectTile] = {}
        for key in EFFECT_ORDER:
            tile = EffectTile(key, self)
            self.tiles[key] = tile
            flow.append(tile)
        g_tiles.add(flow)

        group1 = Adw.PreferencesGroup(title="Tuning")
        right.append(group1)

        color_row = Adw.ActionRow(title="Color")
        self.color_btn = Gtk.ColorDialogButton()
        self.color_btn.set_dialog(Gtk.ColorDialog())
        self.color_btn.set_rgba(self._rgba(self.state.color))
        color_row.add_prefix(self.color_btn)
        group1.add(color_row)
        self.color_row = color_row

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
        group1.add(palette)
        self.palette_box = palette

        self.speed_row = Adw.SpinRow.new_with_range(3.0, 12.0, 0.5)
        self.speed_row.set_title("Cycle length (seconds)")
        self.speed_row.set_value(self.state.speed)
        group1.add(self.speed_row)

        self.dir_row = Adw.SwitchRow(title="Wave direction: left → right",
                                     subtitle="Off = right → left")
        self.dir_row.set_active(self.state.direction == 1)
        group1.add(self.dir_row)

        self.level_row = Adw.SpinRow.new_with_range(0, 100, 5)
        self.level_row.set_title("Brightness (level byte)")
        self.level_row.set_value(self.state.level)
        group1.add(self.level_row)

        self.apply_btn_row = Adw.SwitchRow(
            title="Apply to whole keyboard",
            subtitle="Off = only the selected zones change (Steady only)")
        self.apply_btn_row.set_active(True)
        group1.add(self.apply_btn_row)

        group2 = Adw.PreferencesGroup(title="Apply")
        right.append(group2)

        actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.apply_btn = Gtk.Button(label="Apply")
        self.apply_btn.add_css_class("pill")
        self.apply_btn.add_css_class("suggested-action")
        self.apply_btn.connect("clicked", self.on_apply)
        off_btn = Gtk.Button(label="Turn Off")
        off_btn.connect("clicked", self.on_off)
        actions.append(self.apply_btn)
        actions.append(off_btn)
        group2.add(actions)

        self.autostart_row = Adw.SwitchRow(
            title="Apply this look at login",
            subtitle="Installs an autostart entry that re-runs `mystic-center lighting apply`",
        )
        self.autostart_row.set_active(self.autostart_installed())
        self.autostart_row.connect("notify::active", self.on_autostart_toggled)
        group2.add(self.autostart_row)

        group3 = Adw.PreferencesGroup(
            title="Profiles",
            description="Your saved looks — nothing is predefined. "
                        "Use 'Save current as…' to create one.")
        right.append(group3)

        self.profile_names: list[str] = []
        self.profile_row = Adw.ComboRow(title="Profile")
        group3.add(self.profile_row)
        self.refresh_profiles()

        prof_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
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
        rgba = Gdk.RGBA()
        rgba.parse("#" + hexstr.strip("#"))
        return rgba

    @staticmethod
    def _rgba_ints(rgba: Gdk.RGBA) -> tuple[int, int, int]:
        clamp = lambda c: max(0, min(255, round(c * 255)))  # noqa: E731
        return clamp(rgba.red), clamp(rgba.green), clamp(rgba.blue)

    def setup_css(self) -> None:
        self.provider = Gtk.CssProvider()
        self.style_full()
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def style_full(self) -> None:
        css = [BASE_CSS, LIGHT_KEY_CSS]
        for row in SWATCH_ROWS:
            for _, hx in row:
                r, g, b = hex_to_rgb(hx)
                css.append(
                    f".swatch-{hx.lower()} {{ background-color: rgb({r},{g},{b}); }}")
        for i in range(NUM_ZONES):
            r, g, b = hex_to_rgb(self.preview_colors[i])
            css.append(ZONE_CSS_TEMPLATE.format(idx=i, r=r, g=g, b=b))
        self.provider.load_from_string("\n".join(css))

    def _sync_effect_ui(self) -> None:
        """Show only the tuning rows relevant to the selected effect."""
        eff = self.state.effect
        show = set(TUNING.get(eff, ()))
        self.color_row.set_visible("color" in show or eff == "steady")
        self.palette_box.set_visible("color" in show)
        self.speed_row.set_visible("speed" in show)
        self.dir_row.set_visible("direction" in show)
        self.level_row.set_visible("level" in show)
        self.apply_btn_row.set_visible(eff == "steady")

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

    def sync_preview_from_state(self) -> None:
        st = self.state
        if st.effect in ("cycle", "wave", "swave", "scycle"):
            self.preview_colors = gradient(NUM_ZONES)
        elif st.effect == "breath":
            r, g, b = hex_to_rgb(st.color)
            self.preview_colors = [
                rgb_to_hex(max(r // 2, 1), max(g // 2, 1), max(b // 2, 1))
            ] * NUM_ZONES
        elif st.effect == "off":
            self.preview_colors = ["16161C"] * NUM_ZONES
        else:  # steady
            self.preview_colors = [
                st.zone_colors.get(str(z), st.color).lstrip("#")
                for z in range(NUM_ZONES)
            ]
        for zone in range(NUM_ZONES):
            self.kb_layout.set_zone_selected(zone, zone in self.selected)
        self.style_full()

    # -------------------------------------------------------------------- events

    def on_zone_clicked(self, _btn: Gtk.Button, zone: int) -> None:
        if zone in self.selected:
            self.selected.discard(zone)
        else:
            self.selected.add(zone)
        for z in (zone,):
            self.kb_layout.set_zone_selected(z, z in self.selected)

    def on_effect_tile(self, tile: EffectTile) -> None:
        self.state.effect = tile.effect_key
        self._sync_effect_ui()

    def on_select_all(self, *_a) -> None:
        self.selected = set(range(NUM_ZONES))
        for z in range(NUM_ZONES):
            self.kb_layout.set_zone_selected(z, True)

    def on_clear_selection(self, *_a) -> None:
        self.selected.clear()
        for z in range(NUM_ZONES):
            self.kb_layout.set_zone_selected(z, False)

    def on_swatch(self, _btn, hexstr: str) -> None:
        self.color_btn.set_rgba(self._rgba(hexstr))

    def _current_machine(self) -> MysticKeyboard | None:
        try:
            kb = MysticKeyboard()
            kb.hid.ensure_open()
            return kb
        except Exception as e:
            self.window.toast(str(e))
            return None

    def _collect_state(self) -> None:
        r, g, b = self._rgba_ints(self.color_btn.get_rgba())
        self.state.color = rgb_to_hex(r, g, b)
        self.state.speed = self.speed_row.get_value()
        self.state.direction = 1 if self.dir_row.get_active() else 0
        self.state.level = int(self.level_row.get_value())

    def on_apply(self, *_a) -> None:
        self._collect_state()
        state = self.state
        if state.effect in ("swave", "scycle", "spulse"):
            from .lighting import daemon as daemon_ctl
            from .lighting.animate import get_engine
            try:
                daemon_ctl.stop()
                daemon_ctl.start_detached(state.effect, state.speed, state.direction)
            except Exception as e:
                self.window.toast(f"renderer failed: {e}")
                return
            prof.save_state(state)
            self._preview_engine = None
            try:
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
        from .lighting import daemon as daemon_ctl
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
                state.apply_to(kb)
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
        self._preview_t0 = getattr(self, "_preview_t0", GLib.get_monotonic_time() / 1e6)
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

        from .lighting import daemon as daemon_ctl
        if st.effect in ("swave", "scycle", "spulse"):
            kb = self._current_machine()
            if kb is None:
                return
            st.apply_to(kb)
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
            daemon_ctl.stop()
            self._stop_preview_timer()
            kb = self._current_machine()
            if kb is None:
                return
            st.apply_to(kb)
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
        super().__init__(window, "emblem-system-symbolic")

        g = Adw.PreferencesGroup(title="Daemon (mystic-centerd)")
        self.col.append(g)
        self.svc_row = Adw.ActionRow(
            title="Service status",
            subtitle="checks systemctl is-active (no root needed)")
        check_btn = Gtk.Button(label="Check now")
        check_btn.set_valign(Gtk.Align.CENTER)
        check_btn.connect("clicked", self.on_check)
        self.svc_row.add_suffix(check_btn)
        g.add(self.svc_row)

        restart_row = Adw.ActionRow(
            title="Restart service",
            subtitle="runs `pkexec systemctl restart mystic-center` "
                     "(password dialog appears)")
        rst_btn = Gtk.Button(label="Restart")
        rst_btn.set_valign(Gtk.Align.CENTER)
        rst_btn.connect("clicked", self.on_restart)
        restart_row.add_suffix(rst_btn)
        g.add(restart_row)

    def on_check(self, *_a):
        out = _sysctl("is-active")
        self.svc_row.set_subtitle(f"service: {out or 'unknown'}")
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

    def refresh(self, snap: dict) -> None:
        pass

    def reachability(self, ok, why) -> None:
        self.svc_row.set_subtitle(f"service: {'active' if ok else 'unreachable — ' + why}")


def _sysctl(*args) -> str:
    from .util import run_cmd
    out = run_cmd(["systemctl", "is-active", "mystic-center"], timeout_s=3.0)
    return (out or "").strip() or _raw_is_active()


def _raw_is_active() -> str:
    from .util import run_cmd
    out = run_cmd(["systemctl", "is-active", "mystic-center"], timeout_s=3.0)
    return (out or "").strip()


# ===================================================================== window

class CenterWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1120, 760)

        layout = Adw.ToolbarView()
        header = Adw.HeaderBar()

        overlay = Adw.ToastOverlay()
        layout.set_content(overlay)
        layout.add_top_bar(header)
        self.set_content(layout)
        self.toast_overlay = overlay

        self.stack = Adw.ViewStack()
        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)

        self.pages: list[Page] = [
            OverviewPage(self), PerfPage(self), FansPage(self),
            BatteryPage(self), LightingPage(self), SettingsPage(self),
        ]
        names = ["overview", "performance", "fans", "battery", "lighting", "settings"]
        icons = [p.icon_name for p in self.pages]
        titles = ["Overview", "Performance", "Fans", "Battery", "Lighting", "Settings"]
        for name, title, page, icon in zip(names, titles, self.pages, icons):
            self.stack.add_titled(page, name, title)

        self.banner = Adw.Banner()
        self.banner.button_label = "Retry"
        self.banner.connect("button-clicked", self._retry_now)

        top_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top_box.append(self.banner)
        top_box.append(self.stack)
        overlay.set_child(top_box)

        # snapshot stream -----------------------------------------------------
        self._snap_lock = threading.Lock()
        self._last_snap: dict = {}
        self._thread = threading.Thread(
            target=self._snap_loop, name="snapshot", daemon=True)
        self._thread.start()
        self.connect("close-request", self._on_close)

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
            GLib.idle_add(self._apply_snap, snap, why)  # posts to main thread
            if self._closed:
                return
            import time as _t
            _t.sleep(REFRESH_MS / 1000.0)

    def _apply_snap(self, snap: dict | None, why: str) -> bool:
        self.banner.set_revealed(snap is None)
        if snap is None:
            self.banner.set_title(f"daemon unreachable — {why}")
            for p in self.pages:
                p.reachability(False, why)
            return False
        self._last_snap = snap
        for p in self.pages:
            try:
                p.refresh(snap)
                p.reachability(True, "")
            except Exception as e:  # one pages failure never kills the loop
                print(f"[gui] page refresh error: {e}")
        return False

    def _retry_now(self, *_a) -> None:
        self.banner.set_revealed(False)
        self._apply_snap(None, "manual retry pending")

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
        self.win = CenterWindow(self)
        self.win.present()


def main() -> int:
    app = MysticCenterApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
