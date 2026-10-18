#!/usr/bin/env python3
"""mystic-control — command line companion.

Examples:
  mystic-control list                     # locate the device + access state
  mystic-control steady FF7000            # all zones steady orange
  mystic-control zone 3 FF0055            # paint zone 3
  mystic-control zones "FF0000 0022FF 00CC22 ..."   # all 24 zones, in order
  mystic-control rainbow                  # cycle effect
  mystic-control wave --dir 0             # hardware wave right→left
  mystic-control breath FF2040
  mystic-control sweep                    # light zones one-by-one (mapping check)
  mystic-control profile save "My Look"
  mystic-control profile list|apply NAME|delete NAME
  mystic-control apply                    # re-apply saved state (login/autostart)
  mystic-control test-effects | test-zones
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import profiles as prof
from .effects import hex_to_rgb, rgb_to_hex, gradient, hsl_hex
from .hid import DeviceNotFound, find_hidraw
from .keyboard import NUM_ZONES, MysticKeyboard

NO_DEVICE = (
    "\n  ✗ MSI MysticLight keyboard not found.\n"
    "  Check that `lsusb` shows 1462:1603 ...MysticLight."
)


def _build(args) -> MysticKeyboard | None:
    # any hardware write stops the software animator to avoid packet fights
    from .daemon import stop as daemon_stop
    daemon_stop()
    try:
        kb = MysticKeyboard(debug=getattr(args, "debug", False))
        kb.hid.ensure_open()
        return kb
    except DeviceNotFound:
        if not args.quiet:
            print(NO_DEVICE, file=sys.stderr)
        return None
    except Exception as e:
        if not args.quiet:
            print(f"\n  ✗ {e}", file=sys.stderr)
        return None


def parse_zone_list(s: str) -> list[int]:
    out: set[int] = set()
    for part in s.replace(",", " ").split():
        if "-" in part and not part.startswith("-"):
            a, b = part.split("-", 1)
            for z in range(int(a), int(b) + 1):
                out.add(z)
        else:
            out.add(int(part))
    zones = sorted(out)
    for z in zones:
        if not (0 <= z < NUM_ZONES):
            raise ValueError(f"zone {z} out of range")
    return zones


# --- root parser -------------------------------------------------------------------


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="mystic-control", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--debug", action="store_true", help="hex dump TX/ACK")
        return sp

    add("list", "show detected device and access state")
    sp = add("steady", "solid color on selected (or all) zones")
    sp.add_argument("color")
    sp.add_argument("-z", "--zones", default=None)
    sp = add("zone", "paint one zone (use `sweep` to learn indices)")
    sp.add_argument("zone_index", type=int)
    sp.add_argument("color")
    sp.add_argument("--out", action="store_true", help="use direct per-zone output report")
    sp = add("zones", "24 colors: space or comma separated hex list")
    sp.add_argument("colors")
    sp = add("rainbow", "rainbow color cycle").add_argument("--speed", type=float, default=6.0)
    sp = add("wave", "hardware color wave").add_argument("--dir", type=int, default=1)
    sp = add("breath", "breathing color").add_argument("color")
    sp = add("off", "turn lighting off").add_argument("-z", "--zones", default=None)
    sp = add("level", "level byte 0..100 (MSI Center constant is 50)")
    sp.add_argument("value", type=int)
    sp = add("sweep", "light every zone in order for visual mapping")
    sp.add_argument("--step", type=float, default=0.9)
    add("test-effects", "cycle through every effect briefly")
    add("test-out", "cycle each zone via output reports (out-path test)")
    add("test-zones", "zone order testboard: 24 rainbow colors at once")
    sp = add("daemon", "run a software animation in the foreground (debug)")
    sp.add_argument("engine", choices=["swave", "scycle", "spulse"])
    sp.add_argument("--speed", type=float, default=6.0)
    sp.add_argument("--dir", type=int, default=1)
    sp.add_argument("--fps", type=int, default=12)
    add("apply", "apply persisted state (used by autostart at login)")

    sp = sub.add_parser("profile", help="save/list/apply/delete named profiles")
    sp.add_argument("action", choices=["save", "list", "apply", "delete"])
    sp.add_argument("name", nargs="?", default=None)

    p.add_argument("--quiet", action="store_true")
    args = p.parse_args(argv)

    if args.cmd == "list":
        node = find_hidraw()
        print(f"Device node: {node or 'not found'}")
        if node:
            try:
                fd = os.open(node, os.O_RDWR)  # O_RDWR proves device write perms
                os.close(fd)
                print("Access:      OK (you can write to this node as the current user)")
            except PermissionError:
                print("Access:      DENIED — run `sudo ./install.sh --udev` once, "
                      "then re-run this command.")
            except OSError as e:
                print(f"Access:      ERROR ({e})")
        return 0

    if args.cmd == "test-zones":
        kb = _build(args)
        if not kb:
            return 1
        kb.steady_zones({z: gradient(NUM_ZONES)[z] for z in range(NUM_ZONES)})
        print(f"Set every zone to a distinct rainbow color (zone 0 = red). "
              "Note which physical areas match; adjust with `zone <idx> <color>`.")
        return 0

    if args.cmd == "sweep":
        kb = _build(args)
        if not kb:
            return 1
        print("Lighting zone 0 now; each next zone follows every %.1fs…" % args.step)
        kb.sweep(step_s=args.step)
        return 0

    kb = _build(args)
    if not kb:
        return 1

    if args.cmd == "steady":
        zones = parse_zone_list(args.zones) if args.zones else None
        kb.steady(args.color.upper(), zones=zones)
        state = prof.load_state()
        state.effect = "steady"
        state.color = args.color.upper()
        if zones is not None:
            for z in zones:
                state.zone_colors[str(z)] = args.color.upper()
        prof.save_state(state)
        return 0

    if args.cmd == "zone":
        if args.out:
            kb.steady_zone_out(args.zone_index, args.color.upper())
        else:
            kb.steady(args.color.upper(), zones=[args.zone_index])
        state = prof.load_state()
        state.effect = "steady"
        state.zone_colors[str(args.zone_index)] = args.color.upper()
        prof.save_state(state)
        return 0

    if args.cmd == "test-out":
        print("Output-report path: one block lights solid white, moving zone by zone…")
        for z in range(NUM_ZONES):
            print(f"out-zone {z:>2}", flush=True)
            kb.steady_zone_out(z, "FFFFFF")
            time.sleep(0.7)
        kb.off()
        print("done — out-path works if blocks lit up in order")
        return 0

    if args.cmd == "zones":
        tokens = [t for t in args.colors.replace(",", " ").split() if t]
        if len(tokens) != NUM_ZONES:
            print(f"Need exactly {NUM_ZONES} colors, got {len(tokens)}", file=sys.stderr)
            return 2
        kb.steady_zones({z: c.upper() for z, c in enumerate(tokens)})
        state = prof.load_state()
        state.effect = "steady"
        state.zone_colors = {str(z): c.upper() for z, c in enumerate(tokens)}
        prof.save_state(state)
        return 0

    if args.cmd == "rainbow":
        kb.cycle(speed=int(round(args.speed * 100)))
        state = prof.load_state()
        state.effect = "cycle"
        state.speed = args.speed
        prof.save_state(state)
        return 0

    if args.cmd == "wave":
        kb.wave(speed=600, direction=args.dir)
        state = prof.load_state()
        state.effect = "wave"
        state.direction = args.dir
        prof.save_state(state)
        return 0

    if args.cmd == "breath":
        kb.breath(args.color.upper())
        state = prof.load_state()
        state.effect = "breath"
        state.color = args.color.upper()
        prof.save_state(state)
        return 0

    if args.cmd == "off":
        zones = parse_zone_list(args.zones) if args.zones else None
        kb.off(zones)
        state = prof.load_state()
        state.effect = "off"
        for z in (range(NUM_ZONES) if zones is None else zones):
            state.zone_colors[str(z)] = "#000000"
        prof.save_state(state)
        return 0

    if args.cmd == "level":
        kb.level = min(100, max(0, args.value))
        state = prof.load_state()
        state.level = kb.level
        prof.save_state(state)
        print(f"level byte persisted: {kb.level}")
        return 0

    if args.cmd == "test-effects":
        kb.off()
        time.sleep(0.4)
        kb.steady("#FF5030")
        print("steady red — 2s")
        time.sleep(2)
        kb.breath("#FF5030")
        print("breath red — 2s")
        time.sleep(2)
        kb.cycle()
        print("rainbow cycle — 3s")
        time.sleep(3)
        kb.wave(direction=1)
        print("hardware wave L→R — 3s")
        time.sleep(3)
        kb.wave(direction=0)
        print("hardware wave R→L — 3s")
        time.sleep(3)
        kb.off()
        return 0

    if args.cmd == "apply":
        state = prof.load_state()
        try:
            state.apply_to(kb)
        except Exception as e:
            if not args.quiet:
                print(f"apply failed: {e}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "daemon":
        from .animate import run as run_anim
        print(f"animating: {args.engine} speed={args.speed}s dir={args.dir} fps={args.fps} "
              "(Ctrl-C to stop; restores off state)")
        try:
            run_anim(args.engine, kb, speed=args.speed, direction=args.dir, fps=args.fps)
        except KeyboardInterrupt:
            pass
        return 0

    if args.cmd == "profile":
        if args.action == "save":
            if not args.name:
                print("usage: profile save NAME", file=sys.stderr)
                return 2
            state = prof.load_state()
            prof.save_profile(args.name, state)
            print(f"saved profile '{args.name}'")
            return 0
        if args.action == "list":
            names = sorted(set(prof.builtins()) | set(prof.load_profiles()))
            user = set(prof.load_profiles())
            for n in names:
                print(f"{'*' if n in user else ' '} {n}")
            return 0
        if args.action == "apply":
            if not args.name:
                print("usage: profile apply NAME", file=sys.stderr)
                return 2
            st = prof.get_profile(args.name) or _preset(args.name)
            if st is None:
                print(f"profile '{args.name}' not found", file=sys.stderr)
                return 1
            st.apply_to(kb)
            prof.save_state(st)
            print(f"applied '{args.name}'")
            return 0
        if args.action == "delete":
            if not args.name:
                print("usage: profile delete NAME", file=sys.stderr)
                return 2
            prof.delete_profile(args.name)
            return 0

    return 0


def _preset(name: str) -> prof.LightingState | None:
    data = prof.builtins().get(name)
    if data is None:
        return None
    return prof.LightingState(**data)


if __name__ == "__main__":
    raise SystemExit(main())
