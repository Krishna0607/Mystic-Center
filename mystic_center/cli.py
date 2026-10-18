"""Mystic Center CLI. `mystic-center <cmd>` — no args launches the GUI."""

from __future__ import annotations

import argparse
import json
import sys

from . import client as daemon_client


def _snap_text(snap: dict) -> str:
    cpu, gpu = snap.get("cpu", {}), snap.get("gpu", {})
    fans = snap.get("fans", {})
    bat = snap.get("battery", {})
    modes = snap.get("modes", {})
    duty = fans.get("duty") or {}
    return (
        f"CPU: {fmt(cpu.get('temp'), '°C')}  (fan duty {fmt(cpu.get('fan_pct'), '%')})\n"
        f"GPU: {fmt(gpu.get('temp'), '°C')}  {fmt(gpu.get('util'), '%')} load, "
        f"{fmt(gpu.get('power_w'), ' W')}\n"
        f"Fan: {fmt(fans.get('rpm'), ' RPM')} · mode "
        f"{fans.get('fan_mode') or modes.get('fan') or 'auto'}  "
        f"(duty cpu {duty.get('cpu', '—')}% / gpu {duty.get('gpu', '—')}%)\n"
        f"Battery: {fmt(bat.get('percent'), '%')} {bat.get('status') or ''} "
        f"| limit {bat.get('start_threshold')}/{bat.get('end_threshold')}"
        f" | charger {'on' if bat.get('charger_online') else 'off'}\n"
        f"Mode: shift={modes.get('shift')} fan={modes.get('fan')} "
        f"cooler_boost={modes.get('cooler_boost')} "
        f"super_battery={modes.get('super_battery')}\n"
        f"Perks: webcam={'on' if modes.get('webcam') else 'off'} "
        f"fn={modes.get('fn_key')} win={modes.get('win_key')}"
    )


def fmt(v, suffix=""):
    if v is None or v == "":
        return "—"
    try:
        return f"{round(float(v), 1):g}{suffix}"
    except (TypeError, ValueError):
        return str(v)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mystic-center", add_help=True)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="print live snapshot")
    sp = sub.add_parser("perf", help="eco | comfort | turbo")
    sp.add_argument("mode", nargs="?", choices=["eco", "comfort", "turbo"])
    sp.add_argument("--cooler", choices=["on", "off"], default=None)
    sp.add_argument("--super-battery", choices=["on", "off"], default=None)

    sp = sub.add_parser("fans", help="auto | silent")
    sp.add_argument("preset", nargs="?", choices=["auto", "silent"])

    sp = sub.add_parser("battery", help="set charge limit")
    sp.add_argument("--start", type=int, default=None)
    sp.add_argument("--end", type=int, default=None)
    sp.add_argument("--show", action="store_true")

    sp = sub.add_parser("webcam", help="on | off")
    sp.add_argument("state", choices=["on", "off"])

    sp = sub.add_parser("fn-swap", help="which side carries Fn")
    sp.add_argument("side", choices=["left", "right"])

    sub.add_parser("calibrate", help="probe raw EC shift-mode codes")
    sub.add_parser("gui", help="open the dashboard")

    lp = sub.add_parser("lighting", help="run a legacy lighting CLI command")
    lp.add_argument("args", nargs="*", help="passed to the lighting CLI")

    return p


def run(argv: list[str]) -> int:
    p = build_parser()
    args = p.parse_args(argv)

    if args.cmd is None or args.cmd == "gui":
        from .gui import main as gui_main
        return gui_main()

    if args.cmd == "lighting":
        from .lighting.cli import main as light_main
        return light_main(args.args)

    try:
        with daemon_client.Client() as c:
            if args.cmd == "status":
                print(_snap_text(c.snapshot()))
                return 0
            if args.cmd == "perf":
                out = {}
                if args.mode:
                    out["shift"] = c.request("set_shift_mode", mode=args.mode)
                if args.cooler:
                    out["cooler"] = c.request(
                        "set_cooler_boost", on=args.cooler == "on")
                if args.super_battery:
                    out["super_battery"] = c.request(
                        "set_super_battery", on=args.super_battery == "on")
                if not out:
                    out = {"modes": c.modes()}
                print(json.dumps(out, indent=2))
                return 0
            if args.cmd == "fans":
                if not args.preset:
                    print(json.dumps(c.request("fan_status"), indent=2))
                else:
                    res = c.request("set_fan_preset", preset=args.preset)
                    print(json.dumps(res, indent=2))
                return 0
            if args.cmd == "battery":
                if args.show or (args.start is None and args.end is None):
                    b = c.snapshot()["battery"]
                    print(f"battery {fmt(b.get('percent'), '%')} "
                          f"{b.get('status') or ''} · limit "
                          f"{b.get('start_threshold')}/{b.get('end_threshold')}"
                          f" · charger {'on' if b.get('charger_online') else 'off'}")
                    return 0
                res = c.request("set_battery_limit", end=args.end,
                                start=args.start)
                print(json.dumps(res, indent=2))
                return 0
            if args.cmd == "webcam":
                print(json.dumps(c.request("set_webcam", on=args.state == "on")))
                return 0
            if args.cmd == "fn-swap":
                print(json.dumps(c.request("set_fn_switch", side=args.side)))
                return 0
            if args.cmd == "calibrate":
                print(json.dumps(c.request("calibrate_shift_mode"), indent=2))
                return 0
    except daemon_client.DaemonDown as e:
        print(f"error: {e}", file=sys.stderr)
        print("hint: systemctl start mystic-center (or install: ./install.sh --system)",
              file=sys.stderr)
        return 2
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    p.print_help()
    return 0


def main(av: list[str]) -> int:
    return run(av)
