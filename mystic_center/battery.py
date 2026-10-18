"""Battery charge-limit handling (mirrors the kernel power_supply sysfs)."""

from __future__ import annotations

import glob

from .util import read_int, write_file

MIN_END = 20
DEFAULT_POSTSET = {"start": 60, "end": 80}


def _battery_dir() -> str | None:
    for base in glob.glob("/sys/class/power_supply/*"):
        ptype = None
        try:
            with open(f"{base}/type") as fh:
                ptype = fh.read().strip()
        except OSError:
            continue
        if ptype == "Battery":
            return base
    return None


def battery_dir() -> str | None:
    return _battery_dir()


def supported() -> bool:
    base = battery_dir()
    return bool(base) and (
        read_int(f"{base}/charge_control_end_threshold") is not None)


def thresholds() -> tuple[int, int] | None:
    base = battery_dir()
    if not base:
        return None
    start = read_int(f"{base}/charge_control_start_threshold")
    end = read_int(f"{base}/charge_control_end_threshold")
    if start is None or end is None:
        return None
    return start, end


def validate(end: int, start: int) -> tuple[int, int]:
    end = max(MIN_END, min(100, int(end)))
    start = max(0, min(95, int(start)))
    if end >= 100:
        start = 0  # full charge = no limit
    elif start > end - 5:
        start = end - 5
    return start, end


def set(end: int, start: int | None = None) -> tuple[int, int]:
    """Write thresholds in an order that never creates an invalid state."""
    base = battery_dir()
    if not base:
        raise RuntimeError("no battery sysfs with charge thresholds")
    old = thresholds() or (0, 100)
    new_start = old[0] if start is None else int(start)
    new_start, new_end = validate(end, new_start)

    def write(kind: str, value: int) -> None:
        if not write_file(f"{base}/charge_control_{kind}_threshold", str(value)):
            raise RuntimeError(f"could not write charge_control_{kind}_threshold")

    if new_end >= old[1]:
        write("end", new_end)
        write("start", new_start)
    else:
        write("start", new_start)
        write("end", new_end)

    got = thresholds()
    if got is None:
        raise RuntimeError("threshold read-back failed")
    return got
