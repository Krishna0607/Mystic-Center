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
        f"Mode: {perf_name(modes.get('shift'))} fan={modes.get('fan')} "
        f"cooler_boost={modes.get('cooler_boost')} "
        f"super_battery={modes.get('super_battery')} "
        f"super_saver={modes.get('super_saver')}\n"
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


_PERF_NAMES = {"eco": "battery-saving", "comfort": "balanced",
               "turbo": "performance"}


def perf_name(mode):
    if not mode:
        return "—"
    key = str(mode).split(" ")[0].lower()
    return _PERF_NAMES.get(key, mode)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mystic-center", add_help=True)
    sub = p.add_subparsers(dest="cmd")

    sub.add_parser("status", help="print live snapshot")
    sp = sub.add_parser("perf", help="battery-saving | balanced | performance")
    sp.add_argument("mode", nargs="?", default=None)
    sp.add_argument("--cooler", choices=["on", "off"], default=None)
    sp.add_argument("--super-battery", choices=["on", "off"], default=None)
    sp.add_argument("--saver", choices=["on", "off"], default=None,
                    help="Super Battery Saver (eco + power plane + silent "
                         "fans + 2 GHz CPU cap)")

    sp = sub.add_parser("fans", help="auto | silent")
    sp.add_argument("preset", nargs="?", choices=["auto", "silent"])

    sp = sub.add_parser("fan-curve", help="custom fan curves (MSI WMI tables)")
    sp.add_argument("action", nargs="?", default="show",
                    choices=["show", "manual", "set", "reset",
                             "profiles"])
    sp.add_argument("--cpu", default=None,
                    help='points "T:D" pairs, e.g. "50:35,60:45,70:55,80:70'
                         ',90:80,100:90"')
    sp.add_argument("--gpu", default=None, help="same format, GPU fan")
    sp.add_argument("--on", action="store_true", dest="on",
                    help="with 'manual': enable table-driven control")
    sp.add_argument("--off", action="store_false", dest="on",
                    help="with 'manual': back to firmware auto")
    sp.add_argument("--save-as", default=None, metavar="NAME",
                    help="save the applied curve as a profile first")
    sp.add_argument("--apply", default=None, metavar="NAME",
                    help="apply a saved profile")

    sp = sub.add_parser("battery", help="set charge limit")
    sp.add_argument("--start", type=int, default=None)
    sp.add_argument("--end", type=int, default=None)
    sp.add_argument("--show", action="store_true")

    sp = sub.add_parser("webcam", help="on | off")
    sp.add_argument("state", choices=["on", "off"])

    sp = sub.add_parser("fn-swap", help="which side carries Fn")
    sp.add_argument("side", choices=["left", "right"])

    sub.add_parser("calibrate", help="probe raw EC shift-mode codes")
    sub.add_parser("wmi2", help="inspect the MSI WMI platform interface")
    sub.add_parser("gui", help="open the dashboard")

    lp = sub.add_parser("lighting", help="run a legacy lighting CLI command")
    lp.add_argument("args", nargs="*", help="passed to the lighting CLI")

    return p


_PERF_ALIASES = {
    "battery-saving": "eco", "battery": "eco", "saver": "eco", "eco": "eco",
    "balanced": "comfort", "comfort": "comfort",
    "performance": "turbo", "turbo": "turbo",
}


def parse_curve(text: str) -> list[list[int]]:
    """Hex/decimal `T:D` pairs (MSI-Center's seven-point expectance minus one)."""
    pairs = []
    for chunk in text.replace(",", " ").replace(";", " ").split():
        if ":" not in chunk:
            raise ValueError(f"bad point {chunk!r} — expected T:D")
        t_s, d_s = chunk.split(":", 1)
        pairs.append([int(t_s, 0), int(d_s, 0)])
    if len(pairs) < 2:
        raise ValueError("need at least 2 points")
    return pairs


def curve_text(curve):
    if not curve:
        return "—"
    return " ".join(f"{t:d}:{d:d}" for t, d in curve)


def _run_fan_curve(c, args) -> int:
    action = args.action
    snap = c.request("fan_curve_get") if action != "profiles" else None
    if action == "show":
        if not snap.get("available"):
            print(f"error: {snap.get('error') or 'msi-wmi-platform debugfs not present'}",
                  file=sys.stderr)
            return 1
        print(f"table-driven control: {'ON' if snap.get('manual') else 'off'}")
        print(f"cpu curve: {curve_text(snap.get('cpu_curve'))}")
        print(f"gpu curve: {curve_text(snap.get('gpu_curve'))}")
        print(f"rpm: {snap.get('rpm')}")
        return 0
    if action == "manual":
        if args.on is None:
            raise RuntimeError("use --on or --off with 'manual'")
        res = c.request("fan_curve_manual", on=args.on)
        print(json.dumps(res, indent=2))
        return 0
    if action == "set":
        cpu = parse_curve(args.cpu) if args.cpu else None
        gpu = parse_curve(args.gpu) if args.gpu else None
        if cpu is None and gpu is None:
            raise RuntimeError("nothing to set: pass --cpu and/or --gpu")
        if args.save_as:
            c.request("fan_profile_save", name=args.save_as,
                      data={"cpu": cpu, "gpu": gpu})
            print(f"profile saved: {args.save_as}")
        res = c.request("fan_curve_set", cpu=cpu, gpu=gpu)
        print(json.dumps(res, indent=2))
        return 0
    if action == "reset":
        res = c.request("fan_curve_reset")
        print("wrote MSI default curve to both fans:")
        print(json.dumps(res, indent=2))
        return 0
    if action == "profiles":
        if args.apply:
            data = c.request("fan_profile_get", name=args.apply)
            res = c.request("fan_curve_set", cpu=data.get("cpu"),
                            gpu=data.get("gpu"))
            print(f"applied profile {args.apply!r}")
            print(json.dumps(res, indent=2))
            return 0
        listing = c.request("fan_profile_list")["profiles"]
        print(json.dumps(listing, indent=2))
        return 0
    return 0


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
                if getattr(args, "saver", None):
                    out["saver"] = c.request(
                        "set_super_saver", on=args.saver == "on")
                if args.mode:
                    mode = _PERF_ALIASES.get(args.mode.lower())
                    if mode is None:
                        print(f"error: unknown mode {args.mode!r} — use "
                              "battery-saving | balanced | performance",
                              file=sys.stderr)
                        return 1
                    out["shift"] = c.request("set_shift_mode", mode=mode)
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
            if args.cmd == "fan-curve":
                return _run_fan_curve(c, args)
            if args.cmd == "wmi2":
                inv = c.request("wmi2_inventory")
                print(json.dumps(inv, indent=2))
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
