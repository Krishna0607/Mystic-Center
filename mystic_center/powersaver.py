"""Super Battery Saver: eco shift + super-battery plane + firmware silent fans
+ a userspace CPU frequency cap and power EPP.

The cap is what MSI Center's "super battery" mode feels like on Windows:
strong performance limiting for maximum runtime. State is kept in
/run/mystic-center/saver.json so the daemon can restore exact EPP values
after a restart while the saver is still active. Everything resets by boot.
"""

from __future__ import annotations

import glob
import json
import os

from .util import RUN_DIR, read_file, read_int, write_file

STATE_PATH = os.path.join(RUN_DIR, "saver.json")
DEFAULT_CAP_KHZ = 2_000_000   # 2.0 GHz — strong saver, still fully usable


def _cpufreq_dirs() -> list[str]:
    return sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq"))


class PowerSaver:
    def __init__(self) -> None:
        self.cap_khz = DEFAULT_CAP_KHZ

    # ---- state ---------------------------------------------------------------

    def active(self) -> bool:
        return os.path.exists(STATE_PATH)

    def _read_state(self) -> dict:
        try:
            with open(STATE_PATH) as fh:
                return json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_state(self, data: dict) -> None:
        os.makedirs(RUN_DIR, exist_ok=True)
        with open(STATE_PATH, "w") as fh:
            json.dump(data, fh)

    # ---- implementation ---------------------------------------------------------

    def enable(self) -> dict:
        if self.active():
            return {"already": True}
        saved: dict[str, dict] = {}
        capped = 0
        epp_set = 0
        for cpu_dir in _cpufreq_dirs():
            node = cpu_dir.rsplit("/", 2)[1]  # e.g. cpu17
            max_else = read_int(f"{cpu_dir}/cpuinfo_max_freq") or self.cap_khz
            cur = read_int(f"{cpu_dir}/scaling_max_freq")
            epp = read_file(f"{cpu_dir}/energy_performance_preference")
            saved[node] = {"epp": epp}
            # cap frequency
            target = min(self.cap_khz, max_else)
            if cur is not None and write_file(
                    f"{cpu_dir}/scaling_max_freq", str(target)):
                capped += 1
            # power EPP
            if epp is not None and write_file(
                    f"{cpu_dir}/energy_performance_preference", "power"):
                epp_set += 1
        self._write_state({"epp": saved, "cap_khz": self.cap_khz})
        return {"capped_cpus": capped, "epp_set": epp_set, "cap_ghz":
                round(min(self.cap_khz, 5_000_000) / 1e6, 1)}

    def disable(self) -> dict:
        if not self.active():
            return {"already": False}
        state = self._read_state()
        restored = 0
        epp_restored = 0
        for cpu_dir in _cpufreq_dirs():
            node = cpu_dir.rsplit("/", 2)[1]
            boot_max = read_int(f"{cpu_dir}/cpuinfo_max_freq")
            if boot_max is not None:
                # boot value is the source of truth (survives partial states)
                if write_file(f"{cpu_dir}/scaling_max_freq", str(boot_max)):
                    restored += 1
            epp = (state.get("epp") or {}).get(node, {}).get("epp")
            if not epp:
                epp = "balance_performance"
            if read_file(f"{cpu_dir}/energy_performance_preference") is not None:
                if write_file(f"{cpu_dir}/energy_performance_preference", epp):
                    epp_restored += 1
        try:
            os.unlink(STATE_PATH)
        except OSError:
            pass
        return {"restored_cpus": restored, "epp_restored": epp_restored}

    def status(self) -> dict:
        if not self.active():
            return {"active": False}
        capped = 0
        total = 0
        for cpu_dir in _cpufreq_dirs():
            total += 1
            cur = read_int(f"{cpu_dir}/scaling_max_freq")
            boot = read_int(f"{cpu_dir}/cpuinfo_max_freq")
            if cur is not None and boot is not None and cur < boot:
                capped += 1
        return {"active": True, "capped_cpus": capped, "of": total}
