#!/usr/bin/env python3
"""Lighting CLI — built into mystic-center (`mystic-center lighting ...`).

Examples:
  mystic-center lighting list                     # locate the device + access state
  mystic-center lighting steady FF7000            # all zones steady orange
  mystic-center lighting zone 3 FF0055            # paint zone 3
  mystic-center lighting zones "FF0000 0022FF 00CC22 ..."   # all 24 zones, in order
  mystic-center lighting breath FF2040            # MSI breathing
  mystic-center lighting cycle --speed 6          # MSI color cycle (or slow|medium|fast)
  mystic-center lighting wave --rainbow --dir right --speed medium   # MSI Wave
  mystic-center lighting dance --speed fast       # MSI Dance
  mystic-center lighting wheel --colors FF0000,FFFF00,00FFFF --dir cw
  mystic-center lighting bouncy --colors 00FFFF   # MSI Bouncy Ball
  mystic-center lighting custom-set 0-7 steady --colors FF0000   # per-zone styles
  mystic-center lighting custom-apply             # push stored per-zone styles
  mystic-center lighting save                     # persist to the keyboard (MCU)
  mystic-center lighting revert                   # restore the MCU's saved lighting
  mystic-center lighting sync --color FF00AA      # all-sync solid colour
  mystic-center lighting sync --rainbow           # all-sync rainbow preset
  mystic-center lighting info                     # MCU firmware + language
  mystic-center lighting rainbow --dir 0          # band conveyor right→left
  mystic-center lighting sweep                    # light zones one-by-one (mapping check)
  mystic-center lighting profile save "My Look"
  mystic-center lighting profile list|apply NAME|delete NAME
  mystic-center lighting apply                    # re-apply saved state (login/autostart)
  mystic-center lighting test-effects | test-zones
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from . import profiles as prof
from . import effects
from .effects import hex_to_rgb, rgb_to_hex, gradient, hsl_hex
from .hid import DeviceNotFound, find_hidraw
from .keyboard import NUM_ZONES, MysticKeyboard

SPEED_WORDS = {"slow": 0, "medium": 1, "fast": 2}


def parse_speed(effect: str, value: str | None) -> tuple[float, int]:
    """MSI speed radio: slow|medium|fast (or seconds → nearest preset)."""
    if value is None:
        return effects.SPEED_TABLE[effect][1], 1
    word = str(value).strip().lower()
    if word in SPEED_WORDS:
        idx = SPEED_WORDS[word]
        return effects.SPEED_TABLE[effect][idx], idx
    try:
        secs = float(word)
    except ValueError:
        raise SystemExit(f"bad speed: {value!r} (use slow|medium|fast or seconds)")
    table = effects.SPEED_TABLE[effect]
    idx = min(range(3), key=lambda i: abs(table[i] - secs))
    return secs, idx


def parse_colors(value: str | None) -> list[str] | None:
    if value is None:
        return None
    toks = [t for t in value.replace(",", " ").split() if t]
    out = []
    for t in toks:
        h = t.upper().lstrip("#")
        effects.hex_to_rgb(h)  # validate
        out.append(h)
    return out

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
    p = argparse.ArgumentParser(prog="mystic-center lighting", description=__doc__,
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
    sp = add("cycle", "MSI Color Cycle")
    sp.add_argument("--speed", default="medium", help="slow|medium|fast (or seconds)")
    sp.add_argument("--colors", default=None, help="customized colour list")
    sp.add_argument("--rainbow", action="store_true", help="Rainbow colour mode")
    sp = add("breath", "MSI Breathing")
    sp.add_argument("color")
    sp.add_argument("--speed", default="medium", help="slow|medium|fast (or seconds)")
    sp = add("wave", "MSI Wave — colour bands sweeping across the deck")
    sp.add_argument("--colors", default=None, help="customized colour list")
    sp.add_argument("--rainbow", action="store_true", help="Rainbow colour mode")
    sp.add_argument("--speed", default="medium", help="slow|medium|fast (or seconds)")
    sp.add_argument("--dir", default="right", choices=["left", "right"])
    sp = add("dance", "MSI Dance — whole-board dance animation")
    sp.add_argument("--speed", default="medium", help="slow|medium|fast (or seconds)")
    sp = add("wheel", "MSI Wheel — colour wheel spinning on the deck")
    sp.add_argument("--colors", default=None, help="up to 3 customized colours")
    sp.add_argument("--rainbow", action="store_true", help="Rainbow colour mode")
    sp.add_argument("--speed", default="medium", help="slow|medium|fast (or seconds)")
    sp.add_argument("--dir", default="cw", choices=["cw", "ccw"])
    sp = add("bouncy", "MSI Bouncy Ball")
    sp.add_argument("--colors", default=None, help="customized colour list")
    sp.add_argument("--rainbow", action="store_true", help="Rainbow colour mode")
    sp = add("custom-set", "Customize mode: style zones (ZONES e.g. 0-5,8)")
    sp.add_argument("zones")
    sp.add_argument("effect", choices=["off", "steady", "breath", "cycle",
                                       "wave", "dance"])
    sp.add_argument("--colors", default=None)
    sp.add_argument("--speed", default=None, help="slow|medium|fast (or seconds)")
    sp.add_argument("--dir", default="right", choices=["left", "right"])
    sp = add("custom-clear", "Customize mode: turn zones off / plain steady")
    sp.add_argument("zones")
    add("custom-apply", "Customize mode: re-apply the stored zone styles")
    add("save", "persist current lighting into the keyboard's memory (MCU save)")
    add("revert", "restore the keyboard's saved lighting (MSI Center Cancel)")
    add("reset", "reset the keyboard MCU (recovery for a wedged MCU)")
    sp = add("sync", "MSI all-device sync command (drives this keyboard)")
    mx = sp.add_mutually_exclusive_group()
    mx.add_argument("--color", default=None, help="solid colour hex (default FF0000)")
    mx.add_argument("--rainbow", action="store_true", help="rainbow preset")
    add("info", "read MCU firmware version + keyboard language")
    sp = add("rainbow", "rainbow band conveyor (up to 5 random colours marching)")
    sp.add_argument("--speed", type=float, default=6.0,
                    help="seconds per full 5-band lap (3..12)")
    sp.add_argument("--dir", type=int, default=1)
    sp = add("off", "turn lighting off").add_argument("-z", "--zones", default=None)
    sp = add("sweep", "light every zone in order for visual mapping")
    sp.add_argument("--step", type=float, default=0.9)
    add("test-effects", "cycle through every effect briefly")
    add("test-out", "cycle each zone via output reports (out-path test)")
    add("test-zones", "zone order testboard: 24 rainbow colors at once")
    sp = add("daemon", "run a software animation in the foreground (debug)")
    sp.add_argument("engine", choices=["scycle", "spulse", "rbreath", "rain",
                                       "fire", "radar", "lightning"])
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
        state.colors = [args.color.upper().lstrip("#")]
        state.color_mode = "customized"
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
        state.color = args.color.upper()
        state.colors = [args.color.upper().lstrip("#")]
        state.color_mode = "customized"
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

    if args.cmd == "cycle":
        state = prof.load_state()
        state.effect = "cycle"
        state.speed, state.speed_index = parse_speed("cycle", args.speed)
        cs = parse_colors(args.colors)
        if args.rainbow:
            state.color_mode = "rainbow"
        elif cs:
            state.color_mode = "customized"
        if cs:
            state.colors = cs
        state.apply_to(kb)
        prof.save_state(state)
        return 0

    if args.cmd == "rainbow":
        speed = min(12.0, max(3.0, args.speed))
        bands = effects.random_band_colors()
        kb.rainbow(bands, speed=int(round(speed * 100)), direction=args.dir)
        state = prof.load_state()
        state.effect = "rainbow"
        state.speed = speed
        state.direction = args.dir
        state.band_colors = bands
        prof.save_state(state)
        return 0

    if args.cmd == "breath":
        state = prof.load_state()
        state.effect = "breath"
        state.color = args.color.upper().lstrip("#")
        state.colors = [state.color]
        state.color_mode = "customized"
        state.speed, state.speed_index = parse_speed("breath", args.speed)
        state.apply_to(kb)
        prof.save_state(state)
        return 0

    if args.cmd in ("wave", "dance", "wheel", "bouncy"):
        state = prof.load_state()
        state.effect = args.cmd
        if args.cmd == "dance":
            state.speed, state.speed_index = parse_speed("dance", args.speed)
        else:
            cs = parse_colors(args.colors)
            if args.rainbow:
                state.color_mode = "rainbow"
            elif cs:
                state.color_mode = "customized"
            if cs:
                state.colors = cs
            if args.cmd != "bouncy":
                # firmware-fixed 1.0 s; MSI Center exposes no speed control
                state.speed, state.speed_index = parse_speed(args.cmd,
                                                             args.speed)
            if args.cmd == "wave":
                state.direction = (effects.DIR_LEFT if args.dir == "left"
                                   else effects.DIR_RIGHT)
            elif args.cmd == "wheel":
                state.direction = (effects.DIR_CCW if args.dir == "ccw"
                                   else effects.DIR_CW)
        state.apply_to(kb)
        prof.save_state(state)
        if args.cmd == "dance":
            print(f"applied dance · speed {state.speed:g}s")
        else:
            parts = [f"applied {args.cmd}", f"mode {state.color_mode}"]
            if state.color_mode != "rainbow":
                cols = "/".join("#" + c.lstrip("#")
                                for c in state.effective_colors()[:3])
                parts.append(cols)
            if args.cmd in ("wave", "wheel"):
                parts.append(f"dir {args.dir}")
            print(" · ".join(parts))
        return 0

    if args.cmd == "custom-set":
        zones = parse_zone_list(args.zones)
        state = prof.load_state()
        state.effect = "custom"
        table_effect = args.effect if args.effect in effects.SPEED_TABLE \
            else "breath"
        _, idx = parse_speed(table_effect, args.speed)
        style: dict = {"effect": args.effect, "speed_index": idx}
        cs = parse_colors(args.colors)
        if cs:
            style["colors"] = cs
        if args.effect == "wave":
            style["direction"] = (effects.DIR_LEFT if args.dir == "left"
                                  else effects.DIR_RIGHT)
        for z in zones:
            state.zone_styles[str(z)] = dict(style)
        state.apply_to(kb)
        prof.save_state(state)
        print(f"customize: {args.effect} on zones "
              f"{','.join(map(str, zones))}")
        return 0

    if args.cmd == "custom-clear":
        zones = parse_zone_list(args.zones)
        state = prof.load_state()
        for z in zones:
            state.zone_styles[str(z)] = {"effect": "off"}
        state.effect = "custom"
        state.apply_to(kb)
        prof.save_state(state)
        return 0

    if args.cmd == "custom-apply":
        state = prof.load_state()
        state.effect = "custom"
        state.apply_to(kb)
        prof.save_state(state)
        return 0

    if args.cmd == "save":
        kb.mcu_save()
        print("lighting saved into the keyboard's memory (MCU)")
        return 0

    if args.cmd == "revert":
        kb.mcu_cancel()
        print("keyboard lighting restored from MCU memory")
        return 0

    if args.cmd == "reset":
        kb.mcu_reset()
        print("keyboard MCU reset command sent ([2,255])")
        return 0

    if args.cmd == "sync":
        kb.allsync_begin()
        if args.rainbow:
            kb.allsync_rainbow()
            print("all-sync rainbow preset sent")
        else:
            h = (args.color or "FF0000").upper().lstrip("#")
            r, g, b = hex_to_rgb(h)
            kb.allsync_rgb(r, g, b)
            print(f"all-sync colour #{h} sent")
        return 0

    if args.cmd == "info":
        fw = kb.firmware_version()
        if fw is None:
            print("firmware: no reply")
        else:
            hexs = " ".join(f"{b:02X}" for b in fw[:16])
            pretty = "".join(chr(b) if 32 <= b < 127 else "." for b in fw[:16])
            print(f"firmware reply: {hexs}   {pretty}")
        print(f"language: {kb.language()}")
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

    if args.cmd == "test-effects":
        kb.off()
        time.sleep(0.4)
        kb.steady("#FF5030")
        print("steady red — 2s")
        time.sleep(2)
        kb.breath("#FF5030")
        print("MSI breathing red — 2s")
        time.sleep(2)
        kb.cycle()
        print("MSI color cycle — 3s")
        time.sleep(3)
        kb.msi_effect(effects.EFFECT_WAVE, effects.RAINBOW_STREAM, speed=500,
                      center=(0, 50),
                      wavelength_tenths=effects.WAVELENGTH["wave"],
                      wave_mode=1, direction=1)
        print("MSI wave rainbow → — 3s")
        time.sleep(3)
        kb.msi_effect(effects.EFFECT_WHEEL, effects.RAINBOW_STREAM, speed=500,
                      wavelength_tenths=effects.WAVELENGTH["wheel"],
                      wave_mode=1, direction=0)
        print("MSI wheel rainbow — 3s")
        time.sleep(3)
        kb.msi_effect(effects.EFFECT_BOUNCY, effects.BOUNCY_RAINBOW_STREAM,
                      speed=100, wavelength_tenths=effects.WAVELENGTH["bouncy"],
                      wave_mode=1, direction=1)
        print("MSI bouncy ball rainbow — 3s")
        time.sleep(3)
        kb.dance()
        print("MSI dance — 3s")
        time.sleep(3)
        kb.rainbow(effects.random_band_colors())
        print("rainbow conveyor L→R — 3s")
        time.sleep(3)
        kb.rainbow(effects.random_band_colors(), direction=0)
        print("rainbow conveyor R→L — 3s")
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
            for n in sorted(prof.load_profiles()):
                print(n)
            return 0
        if args.action == "apply":
            if not args.name:
                print("usage: profile apply NAME", file=sys.stderr)
                return 2
            st = prof.get_profile(args.name)
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
    """Legacy convenience: preset lookup removed with the built-in list."""
    return None


if __name__ == "__main__":
    raise SystemExit(main())
