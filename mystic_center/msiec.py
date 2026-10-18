"""msi-ec kernel-driver sysfs access (root side; the driver builds ours from
the CONF_G2_10 register map — see docs/EC-MAP.md)."""

from __future__ import annotations

from .util import MSIEC_BASE, read_file, read_int, write_file

SHIFT_MODES = ("eco", "comfort", "turbo")
FAN_MODES = ("auto", "silent", "advanced")


def _p(name: str) -> str:
    return f"{MSIEC_BASE}/{name}"


def present() -> bool:
    import os
    return os.path.isdir(MSIEC_BASE)


def available_shift_modes() -> list[str]:
    raw = read_file(_p("available_shift_modes")) or ""
    return [m for m in raw.split() if m]


def available_fan_modes() -> list[str]:
    raw = read_file(_p("available_fan_modes")) or ""
    return [m for m in raw.split() if m]


def shift_mode() -> dict:
    """Returns {'name': 'turbo'|..., 'raw': 192, 'text': 'unknown (192)'}."""
    text = read_file(_p("shift_mode")) or ""
    raw = None
    name = text
    if "(" in text:
        name, inside = text.split("(", 1)
        try:
            raw = int(inside.strip().rstrip(")"))
        except ValueError:
            raw = None
    name = name.strip() or None
    if name == "unknown":  # driver doesn't decode this raw value
        name = None
    return {"name": name, "raw": raw, "text": text.strip()}


def set_shift_mode(name: str) -> bool:
    name = name.strip().lower()
    if name not in SHIFT_MODES and name not in available_shift_modes():
        raise ValueError(f"unknown shift mode {name!r}")
    return write_file(_p("shift_mode"), name)


def fan_mode() -> str | None:
    return read_file(_p("fan_mode"))


def set_fan_mode(name: str) -> bool:
    name = name.strip().lower()
    if name not in FAN_MODES and name not in available_fan_modes():
        raise ValueError(f"unknown fan mode {name!r}")
    return write_file(_p("fan_mode"), name)


def cooler_boost() -> bool:
    return (read_file(_p("cooler_boost")) or "").strip() == "on"


def set_cooler_boost(on: bool) -> bool:
    return write_file(_p("cooler_boost"), "on" if on else "off")


def super_battery() -> bool:
    return (read_file(_p("super_battery")) or "").strip() == "on"


def set_super_battery(on: bool) -> bool:
    return write_file(_p("super_battery"), "on" if on else "off")


def realtime() -> dict:
    return {
        "cpu_temp": read_int(_p("cpu/realtime_temperature")),
        "cpu_fan_pct": read_int(_p("cpu/realtime_fan_speed")),
        "gpu_temp": read_int(_p("gpu/realtime_temperature")),
        "gpu_fan_pct": read_int(_p("gpu/realtime_fan_speed")),
    }


def webcam() -> bool:
    return (read_file(_p("webcam")) or "").strip() == "on"


def set_webcam(on: bool) -> bool:
    return write_file(_p("webcam"), "on" if on else "off")


def fn_key() -> str | None:
    return read_file(_p("fn_key"))


def win_key() -> str | None:
    return read_file(_p("win_key"))


def set_fn_key(side: str) -> bool:
    side = side.strip().lower()
    if side not in ("left", "right"):
        raise ValueError("side must be left/right")
    return write_file(_p("fn_key"), side)


def set_win_key(side: str) -> bool:
    side = side.strip().lower()
    if side not in ("left", "right"):
        raise ValueError("side must be left/right")
    return write_file(_p("win_key"), side)


def fw() -> dict:
    return {
        "version": read_file(_p("fw_version")),
        "released": read_file(_p("fw_release_date")),
    }
