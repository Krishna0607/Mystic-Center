"""Privileged operation dispatch — every root-side action lives here."""

from __future__ import annotations

import time

from . import battery, msiec, sensors
from .ecio import ALLOWED, EC, ECError
from .powersaver import PowerSaver

_SHIFT_WAIT_S = 0.4
_WAIT_MAX_TRIES = 4

_FAN_MODES_WITH_CONTROL = ("auto", "silent")  # EC duty/curve writes are not
# accepted by this firmware generation (see docs/EC-MAP.md) — "advanced" has
# nothing to offer without them, so only firmware modes are exposed.


def _wait_mode(getter, want: str) -> str:
    """Read back a mode until the driver reflects it (EC applies with delay)."""
    got = None
    for _ in range(_WAIT_MAX_TRIES):
        time.sleep(_SHIFT_WAIT_S)
        got = getter()
        if got == want:
            return want
    return got or ""


class PrivOps:
    def __init__(self) -> None:
        self.ec = EC()
        self.saver = PowerSaver()
        self.shift_map_note: dict = {}

    def close(self) -> None:
        self.ec.close()

    # ---- snapshot ---------------------------------------------------------------

    def snapshot(self) -> dict:
        gpu_temp, gpu_util, gpu_power = sensors.gpu_temp_util_power()
        rt = msiec.realtime()
        duty: dict = {}
        if self.ec.available():
            try:
                space = self.ec.read_space()
                duty = {"cpu": space[0x71], "gpu": space[0x89]}
            except Exception:
                duty = {}
        bat = sensors.battery_states()
        batnode = next((k for k, v in bat.items() if "percent" in v), None)
        charge = bat.get(batnode, {}) if batnode else {}
        mains = next((v for v in bat.values() if "online" in v), {})
        return {
            "time": time.time(),
            "cpu": {"temp": sensors.cpu_temp(), "fan_pct": rt.get("cpu_fan_pct"),
                    "ec_temp": rt.get("cpu_temp")},
            "gpu": {"temp": gpu_temp, "util": gpu_util, "power_w": gpu_power,
                    "fan_pct": rt.get("gpu_fan_pct")},
            "fans": {"rpm": sensors.fan_rpm(),
                     "fan_mode": msiec.fan_mode(),
                     "duty": duty},
            "system": {"ram": sensors.ram_usage(),
                       "storage": sensors.rootfs_usage()},
            "battery": {
                "node": batnode,
                **charge,
                "charger_online": bool(mains.get("online")),
            },
            "system_temps": sensors.system_temps(),
            "modes": self.modes(),
            "fw": msiec.fw(),
        }

    def modes(self) -> dict:
        sm = msiec.shift_mode()
        return {
            "shift": sm["name"] or f"unknown ({sm['raw']})",
            "shift_raw": sm["raw"],
            "shift_text": sm["text"],
            "fan": msiec.fan_mode(),
            "cooler_boost": msiec.cooler_boost(),
            "super_battery": msiec.super_battery(),
            "super_saver": self.saver.active(),
            "webcam": msiec.webcam(),
            "fn_key": msiec.fn_key(),
            "win_key": msiec.win_key(),
        }

    # ---- performance ------------------------------------------------------------

    def set_shift_mode(self, name: str, wait: bool = True) -> dict:
        name = name.strip().lower()
        note = None
        if self.saver.active() and name != "eco":
            # leaving eco defeats the saver — turn it off implicitly
            self.set_super_saver(False, internal=True)
            note = "Super Battery Saver turned off (mode changed)"
        if not msiec.set_shift_mode(name):
            raise RuntimeError("shift_mode write failed (permissions?)")
        result = name
        if wait:
            result = _wait_mode(lambda: (msiec.shift_mode()["name"]), name)
        return {"set": name, "read": result,
                **({"note": note} if note else {})}

    def set_super_saver(self, on: bool, internal: bool = False) -> dict:
        on = bool(on)
        if on and self.saver.active():
            return {"saver": True, "detail": self.saver.status(),
                    "note": "already on"}
        if not on and not self.saver.active():
            return {"saver": False, "note": "already off"}
        detail = self.saver.enable() if on else self.saver.disable()
        try:
            if on:
                msiec.set_super_battery(True)
                msiec.set_fan_mode("silent")
                self.set_shift_mode("eco")
            else:
                msiec.set_super_battery(False)
                msiec.set_fan_mode("auto")
                self.set_shift_mode("comfort")
        except Exception:
            pass  # EC part failing must not block the frequency restore
        out = {"saver": self.saver.active(), "detail": detail}
        if not internal:
            out["note"] = "saver on — eco · silent fans · 2 GHz CPU cap" if on \
                else "saver off — balanced restored"
        return out

    def calibrate_shift_mode(self) -> dict:
        """Write each mode and observe raw EC bytes — builds the turbo 0xC0/C4 map."""
        if not self.ec.available():
            raise ECError("EC debugfs not available for calibration")
        before = msiec.shift_mode()["raw"]
        observed: dict[str, int] = {}
        for mode in ("eco", "comfort", "turbo"):
            if not msiec.set_shift_mode(mode):
                raise RuntimeError(f"could not enter {mode} during calibration")
            time.sleep(_SHIFT_WAIT_S)
            observed[mode] = self.ec.read_reg(0xD2)
        restore = None
        if before in (0xC2, 0xC1, 0xC4, 0xC0):
            restore = {0xC2: "eco", 0xC1: "comfort", 0xC4: "turbo", 0xC0: "turbo"}[before]
        else:
            restore = "comfort"
        msiec.set_shift_mode(restore)
        time.sleep(_SHIFT_WAIT_S)
        self.shift_map_note = {"observed": observed, "restored": restore}
        return self.shift_map_note

    def set_cooler_boost(self, on: bool) -> dict:
        ok = msiec.set_cooler_boost(bool(on))
        return {"set": bool(on), "ok": ok, "now": msiec.cooler_boost()}

    def set_super_battery(self, on: bool) -> dict:
        ok = msiec.set_super_battery(bool(on))
        return {"set": bool(on), "ok": ok, "now": msiec.super_battery()}

    # ---- fans ------------------------------------------------------------------

    def set_fan_preset(self, name: str) -> dict:
        name = name.strip().lower()
        if name not in _FAN_MODES_WITH_CONTROL:
            raise ValueError(
                f"fan preset {name!r} not available on this firmware — "
                "this EC ignores user duty/curve writes; use cooler boost for "
                "max cooling")
        if not msiec.set_fan_mode(name):
            raise RuntimeError("fan_mode write failed (permissions?)")
        return {"now": msiec.fan_mode()}

    def fan_status(self) -> dict:
        return {"fan": msiec.fan_mode(), "duty": self._duty()}

    def _duty(self) -> dict:
        if not self.ec.available():
            return {}
        try:
            space = self.ec.read_space()
            return {"cpu": space[0x71], "gpu": space[0x89]}
        except Exception:
            return {}

    # ---- battery ----------------------------------------------------------------

    def set_battery_limit(self, end: int, start: int | None = None) -> dict:
        got = battery.set(end, start)
        return {"start": got[0], "end": got[1]}

    # ---- perks ------------------------------------------------------------------

    def set_webcam(self, on: bool) -> dict:
        ok = msiec.set_webcam(bool(on))
        return {"set": bool(on), "ok": ok, "now": msiec.webcam()}

    def set_fn_switch(self, side: str) -> dict:
        """Swap which side carries Fn (the other side becomes Win/hyper)."""
        msiec.set_fn_key(side)
        other = "right" if side == "left" else "left"
        msiec.set_win_key(other)
        return {"fn": msiec.fn_key(), "win": msiec.win_key()}

    # ---- EC introspection ---------------------------------------------------------

    def ec_dump(self, addrs: list[int] | None = None) -> dict:
        regs = None
        if self.ec.available():
            space = self.ec.read_space()
            regs = {f"0x{a:02x}": space[a]
                    for a in (addrs or sorted(ALLOWED) + [0x71, 0x89])}
        return {
            "available": self.ec.available(),
            "allow": {f"0x{a:02x}": v for a, v in ALLOWED.items()},
            "regs": regs,
            "last_error": self.ec.last_error,
        }
