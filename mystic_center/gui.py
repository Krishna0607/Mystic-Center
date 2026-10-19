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

    def __init__(self, window: "CenterWindow", icon: str):
        super().__init__(vexpand=True)
        self.window = window
        self.icon_name = icon
        self.clamp = Adw.Clamp(maximum_size=1160, tightening_threshold=980)
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
        for t in ("CPU", "GPU"):
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


# ==================================================================== gpu

GPU_MODE_INFO = {
    "hybrid": ("MSHybrid", "Both GPUs active · Optimus battery life · "
                           "dGPU renders, iGPU presents"),
    "discrete": ("Discrete", "dGPU drives the panel directly · highest FPS · "
                             "more power draw"),
    "integrated": ("Integrated", "iGPU only · dGPU powered down · "
                                 "maximum battery life"),
}


class GpuPage(Page):
    """GPU MUX switch — MSI Center's 'GPU Switch' (hybrid/discrete/integrated)."""

    def __init__(self, window):
        super().__init__(window, "video-display-symbolic")

        g = Adw.PreferencesGroup(
            title="Graphics mode (MUX switch)",
            description="Firmware-level panel routing. Changing it stages the "
                        "mode and requires a full shutdown to apply.")
        self.col.append(g)

        row = Adw.ActionRow(title="Mode",
                            subtitle="Choose the graphics mode to stage")
        self.mode_btns = SegButtons(
            [("hybrid", "MSHybrid"), ("discrete", "Discrete"),
             ("integrated", "Integrated")],
            self.on_mode)
        self.mode_btns.select("hybrid")
        # The snapshot loop runs every second; it must not overwrite a
        # selection the user has made but not yet applied.
        self._picked: str | None = None
        self._last_target: str | None = None
        self.mode_btns.set_valign(Gtk.Align.CENTER)
        row.add_suffix(self.mode_btns)
        g.add(row)

        self.apply_row = Adw.ActionRow(
            title="Apply staged mode",
            subtitle="Runs the MSI Center firmware handshake, then asks for a "
                     "full shutdown")
        apply_btn = Gtk.Button(label="Apply")
        apply_btn.add_css_class("suggested-action")
        apply_btn.set_valign(Gtk.Align.CENTER)
        apply_btn.connect("clicked", self.on_apply)
        self.apply_row.add_suffix(apply_btn)
        g.add(self.apply_row)

        self.pending_row = Adw.ActionRow(
            title="Reboot pending",
            subtitle="A different mode is staged — power off fully to apply")
        self.pending_row.set_visible(False)
        g.add(self.pending_row)

        g_status = Adw.PreferencesGroup(title="Current state")
        self.col.append(g_status)

        self.cur_row = _mk_row("Active mode", "reported by firmware")
        self.cur_label = Gtk.Label(label="—")
        self.cur_label.add_css_class("monospace")
        self.cur_label.add_css_class("heading")
        self.cur_row.add_suffix(self.cur_label)
        g_status.add(self.cur_row)

        self.tgt_row = _mk_row("Staged target", "applied on next power-on")
        self.tgt_label = Gtk.Label(label="—")
        self.tgt_label.add_css_class("monospace")
        self.tgt_row.add_suffix(self.tgt_label)
        g_status.add(self.tgt_row)

        self.sup_row = _mk_row("Supported modes", "advertised by the firmware")
        self.sup_label = Gtk.Label(label="—")
        self.sup_label.add_css_class("monospace")
        self.sup_row.add_suffix(self.sup_label)
        g_status.add(self.sup_row)

        self.mux_row = _mk_row("EC MUX register", "0x2E bit 6 (PXCT)")
        self.mux_label = Gtk.Label(label="—")
        self.mux_label.add_css_class("monospace")
        self.mux_row.add_suffix(self.mux_label)
        g_status.add(self.mux_row)

        g_gpu = Adw.PreferencesGroup(title="Detected GPUs")
        self.col.append(g_gpu)
        self.gpu_rows: list[Adw.ActionRow] = []
        self.gpu_group = g_gpu

        self.warn = Adw.PreferencesGroup()
        self.col.append(self.warn)
        note = Adw.ActionRow(
            title="What happens on Apply",
            subtitle="The target is written to the MsiDCVarData UEFI variable, "
                     "the EC apply trigger runs, then the firmware acknowledges. "
                     "Nothing changes until the next power-on. If any step "
                     "fails, the previous target is restored.")
        note.set_activatable(False)
        self.warn.add(note)

    # ---- actions ---------------------------------------------------------------

    def on_mode(self, key: str) -> None:
        self._picked = key
        self.window.toast(
            f"{GPU_MODE_INFO.get(key, (key,))[0]} selected — press Apply to stage it")

    def on_apply(self, *_a):
        key = self._picked or self.mode_btns.selected or "hybrid"
        self.mode_btns.select(key)
        label = GPU_MODE_INFO.get(key, (key,))[0]
        dlg = Adw.MessageDialog(
            transient_for=self.window,
            heading=f"Switch to {label}?",
            body="The firmware mode changes on the next power-on. Save all work "
                 "and close applications before shutting down.\n\n"
                 "A full shutdown (power off) is required — a reboot alone does "
                 "not apply the MUX change.")
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
        try:
            with daemon_client.Client(timeout=15.0) as c:
                res = c.request("set_gpu_mode", mode=key)
        except (daemon_client.DaemonDown, RuntimeError) as e:
            self.window.toast(f"GPU mode change failed: {e}")
            self.window.request_snapshot()
            return
        self._picked = res.get("mode", key)
        self._last_target = None      # force a resync from the new state
        self.window.toast(
            f"{res.get('label', key)} staged — save your work and shut down "
            "completely to apply")
        self.window.request_snapshot()

    # ---- refresh ---------------------------------------------------------------

    def refresh(self, snap: dict) -> None:
        st = snap.get("gpu_mode") or {}
        if not st.get("available"):
            self.mode_btns.set_sensitive(False)
            self.apply_row.set_sensitive(False)
            self.cur_label.set_label("not available")
            self.tgt_label.set_label("—")
            self.sup_label.set_label("—")
            self.mux_label.set_label("—")
            self.pending_row.set_visible(False)
            return

        self.mode_btns.set_sensitive(True)
        self.apply_row.set_sensitive(True)

        cur, tgt = st.get("current"), st.get("target")
        self.cur_label.set_label(
            GPU_MODE_INFO.get(cur, (cur,))[0] if cur else "unknown")
        self.tgt_label.set_label(
            GPU_MODE_INFO.get(tgt, (tgt,))[0] if tgt else "unknown")
        sup = st.get("supported") or []
        self.sup_label.set_label(
            " · ".join(GPU_MODE_INFO.get(m, (m,))[0] for m in sup) or "—")

        # Only offer modes the firmware advertises
        for key, btn in self.mode_btns.buttons.items():
            btn.set_sensitive(key in sup)

        # Sync the selector from the daemon only when the target actually
        # changes (initial load, or a completed apply). Re-selecting on every
        # snapshot would snap the user's pending choice back to the staged
        # target before they could press Apply.
        if tgt and tgt != self._last_target:
            self._last_target = tgt
            if self._picked is None or self._picked == tgt:
                self._picked = tgt
                self.mode_btns.select(tgt)

        mux = st.get("mux_ec")
        self.mux_label.set_label(
            f"0x{mux:02X} · {'discrete' if mux & 0x40 else 'hybrid'}"
            if mux is not None else "—")
        self.pending_row.set_visible(bool(st.get("reboot_pending")))

        # Detected GPUs (from the detailed state op, filled lazily)
        self._refresh_gpus()

    def _refresh_gpus(self) -> None:
        try:
            with daemon_client.Client() as c:
                st = c.request("gpu_mode_state")
        except (daemon_client.DaemonDown, RuntimeError):
            return
        gpus = st.get("gpus") or []
        if len(self.gpu_rows) == len(gpus):
            for row, gpu in zip(self.gpu_rows, gpus):
                row.set_subtitle(self._gpu_text(gpu))
            return
        for row in self.gpu_rows:
            self.gpu_group.remove(row)
        self.gpu_rows = []
        for gpu in gpus:
            row = Adw.ActionRow(title=gpu["pci"],
                                subtitle=self._gpu_text(gpu))
            self.gpu_group.add(row)
            self.gpu_rows.append(row)
        if not gpus:
            row = Adw.ActionRow(title="No display controllers detected")
            self.gpu_group.add(row)
            self.gpu_rows.append(row)

    @staticmethod
    def _gpu_text(gpu: dict) -> str:
        vendor = {"0x10de": "NVIDIA", "0x8086": "Intel",
                  "0x1002": "AMD"}.get(gpu.get("vendor", ""), gpu.get("vendor", ""))
        return f"{vendor} {gpu.get('device', '')} · driver {gpu.get('driver')}"

    def reachability(self, ok, why):
        pass


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
        super().__init__(window, "preferences-color-symbolic")
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
        self.set_default_size(1240, 830)
        self.set_size_request(940, 700)

        # F11 fullscreen toggle (and app-positioned full-width layout)
        self.install_action("win.toggle-fullscreen", None, self._toggle_fullscreen)
        app_obj = self.get_application()
        if app_obj is not None:
            app_obj.set_accels_for_action(
                "win.toggle-fullscreen", ["F11"])

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
            BatteryPage(self), GpuPage(self), LightingPage(self),
            SettingsPage(self),
        ]
        names = ["overview", "performance", "fans", "battery", "gpu",
                 "lighting", "settings"]
        icons = [p.icon_name for p in self.pages]
        titles = ["Overview", "Performance", "Fans", "Battery", "GPU",
                  "Lighting", "Settings"]
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
        self.win = CenterWindow(self)
        self.win.present()


def main() -> int:
    app = MysticCenterApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
