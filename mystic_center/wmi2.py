"""MSI WMI2-platform interface access (root side of mysticd).

MSI Center 2.x (Desktop) drives notebooks of this generation through the
MSI-ACPI ``MSI_ACPI`` WMI class (GUID ABBC0F6E-8EA1-11D1-00A0-C90629100000):
its service marshals requests into the same ``Get_*/Set_*`` WMI methods the
in-kernel ``msi-wmi-platform`` driver exposes under debugfs
(``/sys/kernel/debug/msi-wmi-platform-<dev>/get_fan``, ``set_fan``, ...).

Reverse-engineered semantics (confirmed against the MSI Center 2.0.74.0
binaries and upstream driver work — see docs/WMI2-MAP.md):

* every method takes/returns a 32-byte "package"; byte [0] selects the
  subfeature on input and reports success (non-zero) on output,
* ``Get_Fan(0x00)``  -> four 16-bit fan-speed readings (RPM = 480000 / v) at
  package bytes 1..8 (big endian, one per channel),
* ``Get/Set_Fan(0x01/0x02)`` -> CPU/GPU fan duty tables — six u8 values in
  percent at package bytes 2..7 (byte 1 and 8..9 remain unknown/reserved),
* ``Get/Set_Temperature(0x01/0x02)`` -> CPU/GPU temp points (°C): package
  bytes 1 and 4..8 hold six values (bytes 2..3 unused by firmware),
* ``Get/Set_AP(0x01)`` -> flags byte at package byte 1; BIT(7) set switches
  fan control to the tables above (MSI Center's "user-defined" curve), clear
  it for firmware automatic control.

The debugfs files are 0600: callers must be root (the daemon is). Writes
here are serialized, verified by read-back, and never end up fanning at an
uncontrolled state: enable/disable flows always confirm the AP flag and the
daemon watchdog reverts to firmware auto at temperature runaway.
"""

from __future__ import annotations

import glob
import os
import struct
import threading
import time

# ---- WMI2 method file map (names in debugfs are the kernel constants) ------

METHODS = ("get_package", "set_package", "get_ec", "set_ec",
           "get_bios", "set_bios", "get_smbus", "set_smbus",
           "get_master_battery", "set_master_battery",
           "get_slave_battery", "set_slave_battery",
           "get_temperature", "set_temperature",
           "get_thermal", "set_thermal",
           "get_fan", "set_fan",
           "get_device", "set_device",
           "get_power", "set_power",
           "get_debug", "set_debug",
           "get_ap", "set_ap",
           "get_data", "set_data",
           "get_wmi")

# fan / ap / temperature subfeatures
FAN_SUB_RPM = 0x00
FAN_SUB_CPU_TABLE = 0x01
FAN_SUB_GPU_TABLE = 0x02
AP_SUB_FAN_MODE = 0x01
TEMP_SUB_CPU_TABLE = 0x01
TEMP_SUB_GPU_TABLE = 0x02
AP_ENABLE_FAN_TABLES = 0x80

RPM_DIVISOR = 480_000
PACKAGE_SIZE = 32

MAX_RETRIES = 2
WRITE_PAUSE_S = 0.03


class WMI2Error(RuntimeError):
    pass


def _find_debugfs_dir() -> str | None:
    for path in sorted(glob.glob("/sys/kernel/debug/msi-wmi-platform-*")):
        names = {os.path.basename(p) for p in glob.glob(os.path.join(path, "*"))}
        if {m for m in METHODS if m.startswith("get_")} <= names:
            return path
    return None


def _is_package32(data: bytes) -> bool:
    return len(data) == PACKAGE_SIZE


class WMI2:
    """Serialized access to one MSI-ACPI WMI device via kernel debugfs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._dir: str | None = None
        self.last_error: str | None = None
        self.note: str | None = None

    # ---- discovery ---------------------------------------------------------------

    def available(self) -> bool:
        self._dir = self._dir or _find_debugfs_dir()
        return self._dir is not None

    def _path(self, method: str) -> str | None:
        if not self.available():
            return None
        path = os.path.join(self._dir, method)  # type: ignore[type-arg]
        return path if os.path.exists(path) else None

    def quant(self) -> dict:
        """Cheap metadata for op responses."""
        return {"dir": self._dir, "note": self.note}

    # ---- IO ------------------------------------------------------------

    def _write_raw(self, method: str, payload: bytes) -> None:
        path = self._path(method)
        if path is None:
            raise WMI2Error(f"debugfs {method!r} missing — msi-wmi-platform "
                            "driver not on debugfs today")
        if not _is_package32(payload):
            raise WMI2Error("payload must be exactly 32 bytes")
        with open(path, "wb") as fh:
            fh.write(payload)

    def _read_raw(self, method: str) -> bytes:
        path = self._path(method)
        if path is None:
            raise WMI2Error(f"debugfs {method!r} missing")
        with open(path, "rb") as fh:
            data = fh.read(PACKAGE_SIZE)
        if not _is_package32(data):
            self.last_error = f"{method}: short output ({len(data)} bytes)"
            raise WMI2Error(self.last_error)
        return data

    def query(self, method: str, payload: bytes) -> bytes:
        """Invoke one WMI2 method: write trigger then read fresh output."""
        if not method.startswith(("get_", "set_")):
            raise WMI2Error(f"unknown method {method!r}")
        with self._lock:
            # one method call = write trigger on a fresh fd, then a second
            # fd whose open re-renders the seq_file with the just-stored buffer
            self._write_raw(method, payload)
            time.sleep(WRITE_PAUSE_S)
            out = self._read_raw(method)
            if out[0] == 0x00:
                self.last_error = f"{method}: firmware reported failure"
                raise WMI2Error(self.last_error)
            self.last_error = None
            return out

    def _query_retry(self, method: str, payload: bytes) -> bytes:
        err: Exception | None = None
        for _ in range(MAX_RETRIES + 1):
            try:
                return self.query(method, payload)
            except (WMI2Error, OSError) as exc:
                err = exc
                time.sleep(0.05)
        self.last_error = f"{method}: {err}"
        raise WMI2Error(self.last_error)

    # ---- readbacks -------------------------------------------------------------

    def fan_rpm(self) -> list[int]:
        out = self._query_retry("get_fan", bytes([FAN_SUB_RPM] + [0] * 31))
        rpm = []
        for i in range(4):
            v = struct.unpack_from(">H", out, 1 + i * 2)[0]
            rpm.append(RPM_DIVISOR // v if v else 0)
        return rpm

    def fan_table(self, which: str) -> list[int] | None:
        sub = FAN_SUB_CPU_TABLE if which == "cpu" else FAN_SUB_GPU_TABLE
        out = self._query_retry("get_fan", bytes([sub] + [0] * 31))
        # duty points at data offsets 1..6 == package bytes 2..7; byte 1 and
        # the extra 8.. behave as unknown/preserved fields in the kernel patch
        table = list(out[2:8])
        return table if any(t != 0 for t in table) else None

    def fan_tables(self) -> dict[str, list[int] | None]:
        try:
            return {"cpu": self.fan_table("cpu"), "gpu": self.fan_table("gpu")}
        except WMI2Error:
            return {"cpu": None, "gpu": None}

    def temp_table(self, which: str) -> list[int] | None:
        """Temperature points: firmware exposes offsets 0 and 3..7."""
        sub = TEMP_SUB_CPU_TABLE if which == "cpu" else TEMP_SUB_GPU_TABLE
        out = self._query_retry("get_temperature", bytes([sub] + [0] * 31))
        pts = [out[1 + idx] for idx in (0, 3, 4, 5, 6, 7)]
        return pts if any(p > 0 for p in pts) else None

    def ap_fan_mode(self) -> dict:
        out = self._query_retry("get_ap", bytes([AP_SUB_FAN_MODE] + [0] * 31))
        flags = out[1]
        return {"flags": flags, "manual": bool(flags & AP_ENABLE_FAN_TABLES)}

    def versions(self) -> dict:
        info: dict = {}
        try:
            out = self._query_retry("get_wmi", bytes(32))
            info["wmi_interface"] = f"{out[1]}.{out[2]}"
        except WMI2Error:
            pass
        try:
            out = self._query_retry("get_ec", bytes(32))
            flags = out[1]
            info["ec"] = f"{(flags >> 4) & 0b11}.{flags & 0b1111}"
            info["ec_fw"] = out[2:30].split(b"\x00")[0].decode("ascii", "replace")
            info["is_tigerlake_ec"] = bool(flags & 0x80)
        except WMI2Error:
            pass
        return info

    # ---- control -------------------------------------------------------------

    def set_ap_fan_tables(self, enable: bool) -> dict:
        """Turn table-driven (MSI Center 'user-defined') fan control on/off."""
        current = self._query_retry("get_ap", bytes([AP_SUB_FAN_MODE] + [0] * 31))
        payload = bytearray(current)
        payload[0] = AP_SUB_FAN_MODE
        flags = current[1]
        flags |= AP_ENABLE_FAN_TABLES if enable else 0
        if not enable:
            flags &= ~AP_ENABLE_FAN_TABLES
        payload[1] = flags
        self.query("set_ap", bytes(payload))
        now = self.ap_fan_mode()["manual"]
        if now != enable:
            self.last_error = "AP flag did not stick"
            raise WMI2Error(self.last_error)
        return {"manual": now}

    def set_fan_table(self, which: str, table: list[int]) -> dict:
        """Write one fan duty table (six percentages) to the EC firmware."""
        sub = FAN_SUB_CPU_TABLE if which == "cpu" else FAN_SUB_GPU_TABLE
        table = _sanitize_table(table)
        # mirror the kernel driver: GET -> modify the six duty bytes preserving
        # every other field -> SET
        before = self._query_retry("get_fan", bytes([sub] + [0] * 31))
        payload = bytearray(before)
        payload[0] = sub
        payload[2:8] = bytes(table)
        self.query("set_fan", bytes(payload))
        after = self.fan_table(which)
        if after is None:
            if any(table):
                self.last_error = (f"{which} fan table verify failed: "
                                   f"{after} != {table}")
                raise WMI2Error(self.last_error)
            return {"table": table}
        if after != table:
            self.last_error = (f"{which} fan table verify failed: "
                               f"{after} != {table}")
            raise WMI2Error(self.last_error)
        return {"table": table}

    def set_temp_table(self, which: str, temps: list[int]) -> dict:
        """Write fan temp points (firmware stores them at odd offsets)."""
        sub = TEMP_SUB_CPU_TABLE if which == "cpu" else TEMP_SUB_GPU_TABLE
        temps = _sanitize_monotonic(list(temps)[:6], min_val=30, max_val=120)
        temps = [30] * max(0, 6 - len(temps)) + temps  # pad from the cold end
        out_before = self._query_retry("get_temperature", bytes([sub] + [0] * 31))
        payload = bytearray(out_before)  # preserve the unknown bytes 0x1, 0x2
        payload[0] = sub
        for i, target in enumerate((0, 3, 4, 5, 6, 7)):
            payload[1 + target] = temps[i]
        self.query("set_temperature", bytes(payload))
        got = self.temp_table(which)
        if got != temps:
            self.last_error = (f"{which} temp table verify failed: "
                               f"{got} != {temps}")
            raise WMI2Error(self.last_error)
        return {"temps": temps}

    # ---- inventory ------------------------------------------------------------

    def inventory(self) -> dict:
        if not self.available():
            raise WMI2Error(
                "no msi-wmi-platform debugfs dir — driver loaded with debugfs?")
        inv: dict = {
            "dir": self._dir,
            **self.versions(),
            "manual": self._try(self.ap_fan_mode, "manual"),
            "rpm": self._try(self.fan_rpm, "rpm"),
            "cpu_fan_table": self._try(self.fan_table, "cpu_fan_table",
                                       "cpu"),
            "gpu_fan_table": self._try(self.fan_table, "gpu_fan_table",
                                       "gpu"),
            "cpu_temp_table": self._try(self.temp_table, "cpu_temp_table",
                                        "cpu"),
            "gpu_temp_table": self._try(self.temp_table, "gpu_temp_table",
                                        "gpu"),
        }
        return inv

    @staticmethod
    def _try(fn, key: str, *args):
        try:
            return fn(*args)
        except (WMI2Error, OSError):
            return None


# ---- curves ------------------------------------------------------------------

# MSI-Center defaults observed in the EC (docs/EC-MAP.md): a gentle floor at
# 50 °C/35 % climbing to 100 °C/90 % on both fans.
DEFAULT_CPU_CURVE: list[list[int]] = [[50, 35], [60, 45], [70, 55],
                                     [80, 70], [90, 80], [100, 90]]
DEFAULT_GPU_CURVE: list[list[int]] = [[50, 35], [60, 45], [70, 55],
                                      [80, 65], [90, 80], [100, 90]]


def _sanitize_table(table: list[int]) -> list[int]:
    """Fan duty percentages must be six monotonic bytes 0..100."""
    out = []
    last = 0
    for v in list(table)[:6]:
        v = max(0, min(100, int(v)))
        v = max(last, v)  # never allowed to decrease, EC wants a ramp
        out.append(v)
        last = v
    out += [out[-1]] * (6 - len(out))
    return out


def sanitize_temps(curve: list[tuple[int, int]]) -> tuple[list[int], list[int]]:
    """From (temp,duty) pairs to firmware (temps list, duties list)."""
    pts = sorted((int(t), int(d)) for t, d in curve)[:6]
    core = [30, 40, 45, 50, 55, 60]  # fallback floor curve
    while len(pts) < 6:
        pts.append(pts[-1])
    temps = _sanitize_monotonic([p[0] for p in pts], min_val=30, max_val=120)
    duties = _sanitize_table([p[1] for p in pts])
    return temps, duties


def _sanitize_monotonic(values: list[int], min_val: int, max_val: int) -> list[int]:
    out: list[int] = []
    last = min_val
    for v in values:
        v = max(min_val, min(max_val, int(v)))
        v = max(last, v)
        out.append(v)
        last = v
    return out


def sanitize_curve(points: list[tuple[int, int]]) -> list[list[int]]:
    """Public: normalized (temp, duty) pairs, 6 ascending points."""
    temps, duties = sanitize_temps(points)
    return list(map(list, zip(temps, duties)))


# ---- watchdog ------------------------------------------------------------------

RUNAWAY_TEMP_C = 97     # CPU °C that must *sustain* to trip the fail-safe
RUNAWAY_SUSTAIN = 4     # consecutive samples at poll_s apart (~10 s)
FAN_DUTY_READBACKS = (0x71, 0x89)


class FanWatchdog(threading.Thread):
    """While table-driven control is on: watch CPU temp runaway + revert.

    The firmware applies the curve itself (hardware-side), so normally no
    daemon polling is needed; this stays quiet unless the machine cannot
    cool (curve stalled / lid closed / failure), then hands the fans back.
    """

    poll_s = 2.5

    def __init__(self, wmi: WMI2, cpu_temp_fn, log, extra_revert=None) -> None:
        super().__init__(daemon=True, name="mystic-fan-watchdog")
        self.wmi = wmi
        self.cpu_temp_fn = cpu_temp_fn
        self.log = log
        self.extra_revert = extra_revert
        self.strikes = 0
        self.triggered = False
        self.stop_now = threading.Event()
        self._watching = False

    def watching(self, on: bool) -> None:
        self._watching = bool(on)
        if on:
            self.strikes = 0
            self.triggered = False

    def run(self) -> None:
        self.log.info("fan watchdog armed")
        while not self.stop_now.wait(self.poll_s):
            if not self._watching:
                continue
            try:
                if not self.wmi.ap_fan_mode()["manual"]:
                    self.strikes = 0
                    continue
            except WMI2Error:
                continue
            try:
                temp = int(self.cpu_temp_fn() or 0)
            except Exception:
                temp = 0
            if temp >= RUNAWAY_TEMP_C:
                self.strikes += 1
                self.log.warning("cpu %d°C under manual fan control (%d/%d)",
                                 temp, self.strikes, RUNAWAY_SUSTAIN)
                if self.strikes >= RUNAWAY_SUSTAIN:
                    self._revert("thermal runaway")
            else:
                if self.strikes:
                    self.log.info("cpu %d°C — watchdog strikes reset", temp)
                self.strikes = 0

    def _revert(self, why: str) -> None:
        try:
            self.wmi.set_ap_fan_tables(False)
            self.triggered = True
            self.wmi.note = f"fan watchdog reverted to auto ({why})"
            self.log.warning("manual fan control disabled: %s", why)
        except (WMI2Error, OSError) as exc:
            self.log.error("watchdog revert failed: %s", exc)
        if callable(self.extra_revert):
            try:
                self.extra_revert()
            except Exception as exc:  # best effort only
                self.log.warning("watchdog extra revert: %s", exc)

    def revert(self, why: str) -> None:
        self._revert(why)
