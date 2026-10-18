"""Read-only telemetry helpers. All of these work without root."""

from __future__ import annotations

import glob

from .util import read_file, read_int, run_cmd

_HWMON_CACHE: dict[str, str] | None = None


def _hwmon_map() -> dict[str, str]:
    """name -> hwmon dir path, e.g. coretemp -> /sys/class/hwmon/hwmon7."""
    global _HWMON_CACHE
    out: dict[str, str] = {}
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read_file(f"{path}/name")
        if name and name not in out:
            out[name] = path
    _HWMON_CACHE = out
    return out


def _hwmon_by_name(name: str) -> str | None:
    return _hwmon_map().get(name)


def cpu_temp() -> float | None:
    base = _hwmon_by_name("coretemp")
    if not base:
        return None
    # prefer the package sensor
    for label_path in sorted(glob.glob(f"{base}/temp*_label")):
        if read_file(label_path) == "Package id 0":
            idx = label_path.split("_label")[0][-1]
            val = read_int(f"{base}/temp{idx}_input")
            if val is not None:
                return val / 1000.0
    val = read_int(f"{base}/temp1_input")
    return None if val is None else val / 1000.0


def gpu_temp_util_power() -> tuple[float, int, float]:
    """(temp C, util %, power W) — via nvidia-smi; (0,0,0) driver-less."""
    out = run_cmd(
        ["nvidia-smi", "--query-gpu=temperature.gpu,utilization.gpu,power.draw",
         "--format=csv,noheader,nounits"], timeout_s=4.0)
    if out:
        parts = [p.strip() for p in out.strip().split(",")]
        try:
            temp = float(parts[0]) if parts and parts[0] not in ("N/A", "[N/A]") else 0.0
            util = int(float(parts[1])) if len(parts) > 1 and parts[1] not in ("N/A", "[N/A]") else 0
            power = float(parts[2]) if len(parts) > 2 and parts[2] not in ("N/A", "[N/A]") else 0.0
            return temp, util, power
        except (ValueError, IndexError):
            pass
    return 0.0, 0, 0.0


def fan_rpm() -> int | None:
    base = _hwmon_by_name("msi_wmi_platform")
    if not base:
        return None
    return read_int(f"{base}/fan1_input")


def _chip_attr(name: str, attr: str) -> dict[str, float]:
    base = _hwmon_by_name(name)
    if not base:
        return {}
    out: dict[str, float] = {}
    import glob as _g
    for path in _g.glob(f"{base}/{attr}_input"):
        key = path.rsplit("_input", 1)[0].rsplit("/", 1)[1]
        val = read_int(path)
        if val is not None:
            out[key] = val / 1000.0
    return out


def system_temps() -> dict[str, float]:
    """nvme / ram / wifi / acpitz temperatures for the overview."""
    temps: dict[str, float] = {}
    nvme = _chip_attr("nvme", "temp")
    if "temp1" in nvme:
        temps["nvme"] = nvme["temp1"]
    ram = sorted(_chip_attr("spd5118", "temp").values())
    if ram:
        temps["ram"] = max(ram)
    wifi = _chip_attr("iwlwifi_1", "temp") or _chip_attr("iwlwifi_0", "temp")
    if "temp1" in wifi:
        temps["wifi"] = wifi["temp1"]
    acpitz = _chip_attr("acpitz", "temp")
    if "temp1" in acpitz:
        temps["right_side"] = acpitz["temp1"]
    return temps


def ram_usage() -> dict | None:
    """MemTotal / MemAvailable from /proc/meminfo."""
    try:
        with open("/proc/meminfo") as fh:
            info = {}
            for line in fh:
                key, rest = line.split(":", 1)
                info[key] = int(rest.strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None
    total = info.get("MemTotal")
    avail = info.get("MemAvailable")
    if not total or avail is None:
        return None
    used = total - avail
    return {"used_gb": round(used / 1048576, 1),
            "total_gb": round(total / 1048576, 1),
            "pct": round(used * 100 / total, 1)}


def rootfs_usage() -> dict | None:
    """Root-filesystem usage via statvfs."""
    import os
    try:
        st = os.statvfs("/")
        total = st.f_blocks * st.f_frsize
        free = st.f_bfree * st.f_frsize
    except OSError:
        return None
    if not total:
        return None
    used = total - free
    return {"used_gb": round(used / 1e9, 1),
            "total_gb": round(total / 1e9, 1),
            "pct": round(used * 100 / total, 1)}


def battery_states() -> dict[str, dict]:
    out: dict[str, dict] = {}
    for base in sorted(glob.glob("/sys/class/power_supply/*")):
        ptype = read_file(f"{base}/type") or ""
        node = base.rsplit("/", 1)[1]
        if ptype == "Battery":
            b: dict = {"percent": read_int(f"{base}/capacity")}
            status = read_file(f"{base}/status")
            b["status"] = status
            full = read_int(f"{base}/charge_full") or read_int(f"{base}/energy_full")
            now = read_int(f"{base}/charge_now") or read_int(f"{base}/energy_now")
            if full and now:
                b["charge_wh"] = round(now * 15.6 / 1e9, 2)  # ~pack voltage fallback
                b["charge_full_wh"] = round(full * 15.6 / 1e9, 2)
            volts = read_int(f"{base}/voltage_now")
            cur = read_int(f"{base}/current_now")
            if volts and cur:
                b["voltage"] = volts / 1e6
                b["current"] = cur / 1e6
                b["power_w"] = round(volts * cur / 1e12, 1)
            for key in ("charge_control_start_threshold", "charge_control_end_threshold"):
                v = read_int(f"{base}/{key}")
                if v is not None:
                    b["start_threshold" if "start" in key else "end_threshold"] = v
            out[node] = b
        elif ptype == "Mains":
            out[node] = {"online": read_int(f"{base}/online")}
    return out


def nvidia_available() -> bool:
    return _hwmon_by_name("msi_wmi_platform") is not None  # cheap unrelated check placeholder
