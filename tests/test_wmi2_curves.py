#!/usr/bin/env python3
"""Hardware-free tests for the WMI2 fan-table module (mystic_center/wmi2.py)
and its CLI curve parser.

A FakeWmiDevice models the EC side of the MSI-ACPI methods, byte-exactly per
the upstream kernel patch (docs/WMI2-MAP.md):

* GET/SET_FAN subfeature 0x00 -> 4x BE16 rpm words at bytes 1..8
* GET/SET_FAN subfeature 0x01/0x02 -> six duty bytes at bytes 2..7; byte 1 and
  bytes 8..9 are reserved fields the firmware keeps
* GET/SET_TEMPERATURE subfeature 0x01/0x02 -> six temp bytes at bytes 1 and
  4..8; bytes 2..3 reserved
* GET/SET_AP subfeature 0x01 -> flags byte at byte 1, BIT(7) tables-on

Run:  tests/test_wmi2_curves.py
"""

from __future__ import annotations

import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mystic_center import wmi2                      # noqa: E402
from mystic_center.cli import curve_text, parse_curve  # noqa: E402

CHECKS = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        raise SystemExit(1)


# --------------------------------------------------------------- curve helpers

def pairs_to_lists(pairs):  # type: ignore
    return [list(p) for p in pairs]


check("default curves are 6 ascending (temp, duty) pairs",
      all(len(c) == 6 and all(c[i][0] <= c[i + 1][0] and c[i][1] <= c[i + 1][1]
                              for i in range(5))
          for c in (wmi2.DEFAULT_CPU_CURVE, wmi2.DEFAULT_GPU_CURVE)))

check("default curve survives a sanitize round trip",
      wmi2.sanitize_curve([tuple(p) for p in wmi2.DEFAULT_CPU_CURVE]) ==
      wmi2.DEFAULT_CPU_CURVE)

pairs = wmi2.sanitize_curve([(30, 90), (100, 10), (30, 20)])
check("sanitize_curve sorts points and forces monotonic duty",
      pairs == [[30, 20], [30, 90], [100, 90], [100, 90], [100, 90], [100, 90]],
      str(pairs))

lo_hi = wmi2.sanitize_curve([(10, 200), (200, 0)])
check("sanitize_curve clamps temps to 30..120",
      lo_hi[0][0] == 30 and lo_hi[2][0] == 120, str(lo_hi))

pairs = wmi2.sanitize_curve([(40, 5)])
check("sanitize_curve pads to 6 points by repeating the last one",
      len(pairs) == 6 and pairs[3] == [40, 5], str(pairs))

# ------------------------------------------------------------ CLI parsing

check("parse_curve accepts commas, semicolons, whitespace",
      parse_curve("50:35, 60:45;70:55") == [[50, 35], [60, 45], [70, 55]])
check("parse_curve accepts hex values",
      parse_curve("0x32:0x23,0x3c:0x2d") == [[50, 35], [60, 45]])
try:
    parse_curve("50,60,70")
    check("parse_curve rejects chunks without T:D", False)
except ValueError:
    check("parse_curve rejects chunks without T:D", True)
try:
    parse_curve("50:35")
    check("parse_curve needs at least 2 points", False)
except ValueError:
    check("parse_curve needs at least 2 points", True)
check("curve_text <-> parse_curve round trip",
      parse_curve(curve_text(wmi2.DEFAULT_CPU_CURVE)) == wmi2.DEFAULT_CPU_CURVE)

# ------------------------------------------------------- fake firmware device


class FakeWmiDevice:
    """EC-side model of the MSI-ACPI WMI methods (see module docstring)."""

    def __init__(self) -> None:
        self.rpm = [2000, 2400, 0, 0]           # channel speed in RPM
        self.duty = {1: bytearray(32), 2: bytearray(32)}
        for d in self.duty.values():
            d[1] = 0x01                          # byte 1: reserved, non-zero
            d[8:10] = b"\xab\xcd"                # bytes 8..9: reserved
        self.temps = {1: bytearray(32), 2: bytearray(32)}
        for t in self.temps.values():
            t[2:4] = b"\x11\x22"                 # bytes 2..3: reserved
        self.ap = bytearray(32)
        self.ap[1] = 0x00
        self.set_fail = False

    def feed(self, method: str, payload: bytes) -> bytes:
        sub = payload[0]
        if self.set_fail:
            return bytes(32)                     # out[0] == 0 -> failure
        if method == "set_fan":
            new = bytearray(self.duty[sub])
            new[2:8] = payload[2:8]              # firmware stores the six duties
            self.duty[sub] = new
            return self._ok()
        if method == "get_fan":
            out = bytearray(32)
            out[0] = 1
            if sub == wmi2.FAN_SUB_RPM:
                for i, r in enumerate(self.rpm):
                    word = (wmi2.RPM_DIVISOR // r) if r else 0
                    out[1 + i * 2:1 + i * 2 + 2] = struct.pack(">H", word)
            else:
                out[1:] = self.duty[sub][1:]
                out[0] = 1
            return bytes(out)
        if method == "set_temperature":
            new = bytearray(self.temps[sub])
            new[1] = payload[1]
            new[4:9] = payload[4:9]              # bytes 2..3 stay firmware-owned
            self.temps[sub] = new
            return self._ok()
        if method == "get_temperature":
            out = bytearray(self.temps[sub])
            out[0] = 1
            return bytes(out)
        if method == "set_ap":
            self.ap = bytearray(payload)
            return self._ok()
        if method == "get_ap":
            out = bytearray(self.ap)
            out[0] = 1
            return bytes(out)
        if method == "get_wmi":
            out = bytearray(32)
            out[0] = 1
            out[1:3] = b"\x02\x01"
            return bytes(out)
        if method == "get_ec":
            out = bytearray(32)
            out[0] = 1
            out[1] = 0x11
            out[2:30] = b"15P2EMS1.110"
            return bytes(out)
        raise AssertionError(f"fake device: unhandled {method}")

    @staticmethod
    def _ok() -> bytes:
        out = bytearray(32)
        out[0] = 1
        return bytes(out)


def attach_fake(device: FakeWmiDevice) -> None:
    """Route WMI2.query through the fake device (skipping debugfs)."""
    def fake_query(self: wmi2.WMI2, method: str, payload: bytes) -> bytes:
        if not method.startswith(("get_", "set_")):
            raise wmi2.WMI2Error(f"unknown method {method!r}")
        if len(payload) != wmi2.PACKAGE_SIZE:
            raise wmi2.WMI2Error("payload must be exactly 32 bytes")
        out = device.feed(method, bytes(payload))
        if out[0] == 0x00:
            self.last_error = f"{method}: firmware reported failure"
            raise wmi2.WMI2Error(self.last_error)
        self.last_error = None
        return out
    wmi2.WMI2.query = fake_query                     # type: ignore[method-assign]
    wmi2._find_debugfs_dir = lambda: "/fake/debugfs"  # type: ignore[assignment]


dev = FakeWmiDevice()
attach_fake(dev)
w = wmi2.WMI2()

check("WMI2 discovers the fake debugfs dir",
      w.available() is True and w._dir == "/fake/debugfs")

try:
    inv = w.inventory()
except wmi2.WMI2Error as exc:
    inv = None
    check("inventory handles an empty fake device", False, str(exc))
check("inventory merges versions + rpm + tables + ap",
      isinstance(inv, dict) and inv.get("dir") == "/fake/debugfs" and
      inv.get("rpm") == [2000, 2400, 0, 0] and
      isinstance(inv.get("manual"), dict) and
      inv.get("ec") == "1.1" and inv.get("ec_fw") == "15P2EMS1.110",
      str(inv))

check("rpm decode returns the channel speeds",
      w.fan_rpm() == [2000, 2400, 0, 0])

check("fan_table: empty firmware tables read as None",
      w.fan_table("cpu") is None and w.fan_table("gpu") is None)

table = [35, 45, 55, 70, 80, 90]
res = w.set_fan_table("cpu", table)
check("set_fan_table read-back verify succeeds",
      res == {"table": table} and w.fan_table("cpu") == table, str(res))
stored = dev.duty[wmi2.FAN_SUB_CPU_TABLE]
check("duty table lands at bytes 2..7, bytes 1/8/9 preserved",
      list(stored[2:8]) == table and stored[1] == 0x01 and
      list(stored[8:10]) == [0xab, 0xcd], stored.hex())

check("set_fan_table clamps a decreasing tail to a valid ramp",
      w.set_fan_table("cpu", [100, 90, 80])["table"] ==
      [100, 100, 100, 100, 100, 100])

check("gpu table set + read back",
      w.set_fan_table("gpu", [40, 50, 60, 70, 80, 90]) ==
      {"table": [40, 50, 60, 70, 80, 90]} and
      w.fan_table("gpu") == [40, 50, 60, 70, 80, 90])

temps = [50, 60, 70, 80, 90, 100]
check("temp table set + read back", w.temp_table("cpu") != temps)
w.set_temp_table("cpu", temps)
stored_t = dev.temps[wmi2.TEMP_SUB_CPU_TABLE]
check("temp table lands at bytes 1, 4..8; bytes 2..3 untouched",
      stored_t[1] == 50 and list(stored_t[4:9]) == temps[1:] and
      list(stored_t[2:4]) == [0x11, 0x22], stored_t.hex())
check("temp_table(read-back) == written",
      w.temp_table("cpu") == temps)

check("ap_fan_mode: manual is False initially",
      w.ap_fan_mode() == {"flags": 0x00, "manual": False})
dev.ap[1] = 0x12                      # other flags the firmware may carry
w.set_ap_fan_tables(True)
check("set_ap_fan_tables(True) sets BIT(7), preserving other flag bits",
      w.ap_fan_mode() == {"flags": 0x92, "manual": True} and
      dev.ap[1] == 0x92, f"{dev.ap[1]:#x}")
w.set_ap_fan_tables(False)
check("set_ap_fan_tables(False) clears only BIT(7)",
      w.ap_fan_mode() == {"flags": 0x12, "manual": False}, f"{dev.ap[1]:#x}")

dev.set_fail = True
try:
    w.set_fan_table("cpu", [1, 2, 3, 4, 5, 6])
    check("set_fan_table raises when firmware reports failure", False)
except wmi2.WMI2Error:
    check("set_fan_table raises when firmware reports failure", True,
          str(w.last_error))
dev.set_fail = False

check("full curve pairing as privops consumes it (zip(temps, duties))",
      pairs_to_lists(zip(temps, table)) == [[50, 35], [60, 45], [70, 55],
                                            [80, 70], [90, 80], [100, 90]])

print(f"\n{CHECKS} checks passed")
