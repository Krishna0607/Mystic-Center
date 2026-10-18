#!/usr/bin/env python3
"""First-contact probe for the MSI WMI2 platform fan interface.

Talks to the msi-wmi-platform debugfs files **directly** (needs root) —
this validates the firmware interface on its own, before/without the
mystic-center daemon. After `install.sh --system`, the daemonized equivalent
is `mystic-center wmi2` / `mystic-center fan-curve show`.

Read-only by default. `--write-test` briefly enables table-driven control,
programs a temporary curve on both fans, watches the RPM response and then
restores whatever was there before (or MSI defaults if there was no prior
curve). `--watch SEC` keeps printing fan speeds so you can physically verify
that a curve change actually reaches the hardware.

See docs/WMI2-MAP.md for the protocol semantics and evidence.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mystic_center import wmi2  # noqa: E402


def inventory(w: wmi2.WMI2) -> dict:
    """Read-only: versions, AP flag, RPM, stored tables."""
    inv: dict = {"dir": w._dir, "note": w.note}
    inv.update(w.versions())
    inv["ap"] = w.ap_fan_mode()
    inv["rpm"] = w.fan_rpm()
    for which in ("cpu", "gpu"):
        inv[f"{which}_fan_table"] = w.fan_table(which)
        inv[f"{which}_temp_table"] = w.temp_table(which)
    return inv


def dump(title: str, data: dict) -> None:
    print(f"---- {title}")
    print(json.dumps(data, indent=2))


def watch_rpm(w: wmi2.WMI2, seconds: float, interval: float) -> None:
    """Sample the fan-speed channel; useful to verify a curve change."""
    print(f"---- watching fan RPM for {seconds:g}s (ctrl-c to stop)")
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            rpm = w.fan_rpm()
            print(time.strftime("%H:%M:%S"), "rpm:", rpm)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass


def write_test(w: wmi2.WMI2, seconds: float) -> None:
    """Round-trip duty + temperature tables and the AP enable bit."""
    print("---- write test (state is fully restored afterwards)")

    ap_before = w.ap_fan_mode()
    tables_before = w.fan_tables()
    temps_before = {k: w.temp_table(k) for k in ("cpu", "gpu")}
    print("before:", {"ap": ap_before, "tables": tables_before,
                      "temps": temps_before})

    temp_curve = [[50, 40], [60, 45], [70, 55], [80, 65], [90, 75], [100, 85]]
    try:
        w.set_ap_fan_tables(True)
        print("manual table control enabled:", w.ap_fan_mode())
        for which in ("cpu", "gpu"):
            w.set_fan_table(which, [p[1] for p in temp_curve])
            w.set_temp_table(which, [p[0] for p in temp_curve])
            print(f"{which}: duties={w.fan_table(which)} "
                  f"temps={w.temp_table(which)}")
        print("fans should now follow 40/45/55/65/75/85 % at "
              "50/60/70/80/90/100 °C — listen/verify:")
        watch_rpm(w, seconds, 1.0)
    finally:
        print("---- restoring")
        for which in ("cpu", "gpu"):
            table = tables_before.get(which)
            temps = temps_before.get(which)
            if table and temps:
                msic_curve = [list(p) for p in zip(temps, table)]
                w.set_fan_table(which, [p[1] for p in msic_curve])
                w.set_temp_table(which, [p[0] for p in msic_curve])
                print(f"{which}: restored previous curve "
                      f"{[list(p) for p in zip(temps, table)]}")
            else:
                w.set_fan_table(which, [p[1] for p in (
                    wmi2.DEFAULT_CPU_CURVE if which == "cpu"
                    else wmi2.DEFAULT_GPU_CURVE)])
                w.set_temp_table(which, [p[0] for p in (
                    wmi2.DEFAULT_CPU_CURVE if which == "cpu"
                    else wmi2.DEFAULT_GPU_CURVE)])
                print(f"{which}: no previous curve stored — wrote the "
                      "MSI default")
        w.set_ap_fan_tables(ap_before["manual"])
        print("AP flag back to:", w.ap_fan_mode())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--watch", type=float, default=None, metavar="SEC",
                    help="sample fan RPM for SEC seconds (read-only)")
    ap.add_argument("--write-test", action="store_true",
                    help="briefly program a temporary curve, then restore")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="sampling interval for --watch (seconds)")
    args = ap.parse_args(argv)

    if os.geteuid() != 0:
        print("error: this probe needs root (debugfs is 0600) — "
              "run with sudo", file=sys.stderr)
        return 2

    w = wmi2.WMI2()
    if not w.available():
        print("error: no msi-wmi-platform debugfs directory found", file=sys.stderr)
        print("hints:", file=sys.stderr)
        print("  - `lsmod | grep msi_wmi_platform` — is the driver loaded?", file=sys.stderr)
        print("  - mount debugfs: `mount -t debugfs debugfs /sys/kernel/debug`", file=sys.stderr)
        print("  - the daemonized equivalent: `mystic-center wmi2`", file=sys.stderr)
        return 1

    try:
        dump("inventory", inventory(w))
        if args.watch is not None:
            watch_rpm(w, args.watch, args.interval)
        if args.write_test:
            write_test(w, max(4.0, args.watch or 6.0))
    except wmi2.WMI2Error as exc:
        print(f"WMI2Error: {exc}", file=sys.stderr)
        if w.last_error:
            print(f"last error detail: {w.last_error}", file=sys.stderr)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
