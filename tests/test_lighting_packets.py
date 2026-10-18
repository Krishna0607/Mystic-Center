#!/usr/bin/env python3
"""Hardware-free validation: intercepts HID writes and checks packet bytes
against the reverse-engineered MSI Center capture for 1462:1603.

Run:  python3 tests/dry_test.py
"""

import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()  # never touch real config in tests

import colorsys  # noqa: E402
import sys  # noqa: E402

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mystic_center.lighting import effects           # noqa: E402
from mystic_center.lighting import animate           # noqa: E402
from mystic_center.lighting.keyboard import MysticKeyboard, NUM_ZONES  # noqa: E402
from mystic_center.lighting import profiles as prof  # noqa: E402


class FakeHid:
    """Replaces MysticHid — records every 64-byte feature report."""

    def __init__(self):
        self.sent: list[bytes] = []
        self.fail = False

    # interface used by MysticKeyboard
    def ensure_open(self):
        pass

    def send_feature(self, payload: bytes) -> bool:
        assert len(payload) <= 63, "payload overflow (>63 bytes cmd)"
        padded = bytes(payload).ljust(63, b"\x00")
        # record full wire report: report id byte + padded payload
        self.sent.append(bytes([2]) + padded)
        return True

    def read_ack(self):
        return None


def run(kb: MysticKeyboard, action, *a, **kw):
    kb.hid.sent.clear()
    action(*a, **kw)
    return [bytes(p) for p in kb.hid.sent]


def main() -> int:
    hid = FakeHid()
    kb = MysticKeyboard(hid=hid)

    ok = True

    def check(desc: str, got, want) -> None:
        nonlocal ok
        if got == want:
            print(f"  ✓ {desc}")
        else:
            ok = False
            gh = got.hex(" ") if isinstance(got, (bytes, bytearray)) else repr(got)
            wh = want.hex(" ") if isinstance(want, (bytes, bytearray)) else repr(want)
            print(f"  ✗ {desc}\n      got  {gh}\n      want {wh}")

    # ---------------------------------------------------------------- steady, 1 zone
    pkts = run(kb, kb.steady, "FF8000", zones=[3])
    check("2 packets for one steady zone", len(pkts), 2)

    # full wire frame: [report_id 02][packet payload 0x01 + u32 LE zone mask]
    want_mask = bytes([0x02, 0x01, 1 << 3, 0, 0, 0])
    check("zone mask packet (zone 3)", pkts[0][:6], want_mask)

    # MSI Center captured steady packet, framed:
    #   02 02 | 01 | 58 02 | 00 00 | 32 | 08 | 01 | 00 | r g b | 64 | r g b
    want_effect = bytes(
        [0x02, 0x02, 0x01, 0x58, 0x02, 0x00, 0x00, 0x32, 0x08, 0x01]  # hdr w/ level, const, dir
        + [0x00, 0xFF, 0x80, 0x00]                                   # keyframe 0 (t=0)
        + [0x64, 0xFF, 0x80, 0x00]                                   # keyframe 1 (t=100)
    )
    check("MSI-Center-identical steady effect packet (FF8000@50)", pkts[1][:18], want_effect)
    check("padding zeros", pkts[1][18:], bytes(len(pkts[1]) - 18))

    # ---------------------------------------------------------------- all zones mask
    pkts = run(kb, kb.steady, "00AA55")
    want_mask_all = bytes([0x02, 0x01] + list(int.to_bytes(0xFFFFFF, 4, "little")))
    check("mask for all 24 zones", pkts[0][:6], want_mask_all)

    # ---------------------------------------------------------------- per-zone paint
    colors = {0: "FF0000", 1: "00FF00", 2: "0000FF"}
    pkts = run(kb, kb.steady_zones, colors)
    check("3 zone groups → 3 masks + 3 effect packets", len(pkts), 6)
    check("first mask = zone 0", pkts[0][:6], bytes([0x02, 0x01, 1, 0, 0, 0]))
    check("second mask = zone 1", pkts[2][:6], bytes([0x02, 0x01, 2, 0, 0, 0]))
    check("third mask = zone 2", pkts[4][:6], bytes([0x02, 0x01, 4, 0, 0, 0]))

    # ---------------------------------------------------------------- breath / cycle
    pkts = run(kb, kb.breath, "FF2040", speed=450)
    anim = pkts[1][2]
    speed = pkts[1][3] | (pkts[1][4] << 8)
    check("breath animation type 2", anim, 0x02)
    check("breath speed 450", speed, 450)
    check("breath kf0 color", pkts[1][11:14], bytes([0xFF, 0x20, 0x40]))

    pkts = run(kb, kb.cycle)
    check("cycle animation type 2? no — 3", pkts[1][2], 0x03)

    # ---------------------------------------------------------------- firmware rainbow
    # Per-zone colour cycles phase-offset by band slot (row-major hardware:
    # the travel axis is z % 4); the MCU animates them by itself.
    bands = ["FF0000", "00FF00", "0000FF", "FFFF00", "FF00FF"]

    def rgb_of(name: str) -> bytes:
        return bytes(int(name[i:i + 2], 16) for i in (0, 2, 4))

    pkts = run(kb, kb.rainbow, bands, speed=500, direction=1)
    check("rainbow: mask+effect packet per zone", len(pkts), NUM_ZONES * 2)
    effs = pkts[1::2]
    check("rainbow: animation type 3 (colour cycle)",
          all(p[2] == 0x03 for p in effs), True)
    check("rainbow: speed 500 everywhere",
          {p[3] | (p[4] << 8) for p in effs}, {500})
    check("rainbow: direction L→R everywhere", {p[9] for p in effs}, {0x01})
    check("zones in one column share identical keyframes",
          all(tuple(effs[z * 4 + c][10:]) == tuple(effs[c][10:])
              for z in range(6) for c in range(4)), True)

    def _band_tbl(p: bytes) -> list[tuple]:
        kfs = [tuple(p[10 + i * 4:14 + i * 4]) for i in range(10)]
        return [k for k in kfs if k[0] != 0 or any(k[1:])]

    tbl = _band_tbl(effs[0])
    check("rainbow band table: exactly 10 keyframes", len(tbl), 10)
    check("rainbow band table: t 0→100, seams at 20% steps",
          [k[0] for k in tbl], [0, 16, 20, 36, 40, 56, 60, 76, 80, 100])
    check("rainbow band table: holds its colour solid (kf0 == kf1)",
          tbl[0][1:], tbl[1][1:])
    check("rainbow band table: loop wraps into kf0's colour",
          bytes(tbl[-1][1:]), bytes(tbl[0][1:]))

    # dir=1 (bands travel right): column c starts one band slot further,
    # then runs BACKWARDS through the stream
    for c in range(4):
        check(f"rainbow col {c}: first colour = stream[{c}]",
              bytes(_band_tbl(effs[c])[0][1:]), rgb_of(bands[c]))
        nxt = bands[(c - 1) % 5]
        check(f"rainbow col {c}: next band earlier in the stream",
              bytes(_band_tbl(effs[c])[2][1:]), rgb_of(nxt))

    pkts = run(kb, kb.rainbow, bands, speed=500, direction=0)
    effs = pkts[1::2]
    check("rainbow: direction R→L everywhere", {p[9] for p in effs}, {0x00})
    for c in range(4):
        check(f"rainbow R→L col {c}: next band later in the stream",
              bytes(_band_tbl(effs[c])[2][1:]), rgb_of(bands[(c + 1) % 5]))

    # band keyframe shape for every stream length (MCU cap: 10)
    for n in (2, 3, 4, 5):
        kfs = effects.band_keyframes(bands[:n], 1, 0)
        check(f"band table n={n}: ≤10 keyframes, ascending, wraps",
              (len(kfs) <= 10, [k.t for k in kfs] == sorted(k.t for k in kfs),
               kfs[0].t, kfs[-1].t, (kfs[-1].r, kfs[-1].g, kfs[-1].b)),
              (True, True, 0, 100, (kfs[0].r, kfs[0].g, kfs[0].b)))

    rc = effects.random_band_colors()
    check("random bands: 5 vivid hex colours",
          all(len(c) == 6 and all(ch in "0123456789ABCDEF" for ch in c)
              for c in rc), True)
    hues = [colorsys.rgb_to_hls(*(int(c[i:i + 2], 16) / 255 for i in (0, 2, 4)))[0] * 360
            for c in rc]

    def _hue_dist(a: float, b: float) -> float:
        return min((a - b) % 360.0, (b - a) % 360.0)

    check("random bands: hue separation ≥ ~40° (vivid randomness)",
          all(_hue_dist(hues[i], hues[j]) >= 39.0
              for i in range(5) for j in range(5) if i != j), True)

    # ---------------------------------------------------- rainbow preview engine
    eng = animate.get_engine("rainbow", speed=5.0, direction=1, colors=bands)

    def _fold_expect(r: int, g: int, b: int, z: int) -> str:
        r = (r & 0b11111100) | (z & 0x03)
        b = (b & 0b11111000) | ((z >> 2) & 0x07)
        return "%02X%02X%02X" % (r, g, b)

    frame = eng.colors(0.0)
    check("rainbow preview: 24 distinct colours (packet contract)",
          len(set(frame)), 24)
    want = []
    for z in range(NUM_ZONES):
        col = z % 4
        r, g, b = rgb_of(bands[col])
        want.append(_fold_expect(r, g, b, z))
    check("rainbow preview t=0: deck = stream colours 0..3, one per column",
          frame, want)

    for col in range(4):
        rgbs = [tuple(int(frame[row * 4 + col][i:i + 2], 16)
                      for i in (0, 2, 4)) for row in range(6)]
        head = rgbs[0]
        check(f"rainbow column {col}: one clean colour (fold ≤7/255)",
              all(abs(r[0] - head[0]) <= 3 and r[1] == head[1] and
                  abs(r[2] - head[2]) <= 7 for r in rgbs), True)

    f15 = eng.colors(1.5)  # one band step done (slot = 5s/5 = 1s), mid-hold
    check("rainbow L→R: after one step column 0 shows the 5th colour",
          f15[0], _fold_expect(0xFF, 0x00, 0xFF, 0))
    check("rainbow L→R: after one step column 1 shows stream colour 0",
          f15[1], _fold_expect(0xFF, 0x00, 0x00, 1))
    eng0 = animate.get_engine("rainbow", speed=5.0, direction=0, colors=bands)
    f05 = eng0.colors(1.5)
    check("rainbow R→L: after one step column 0 shows stream colour 1",
          f05[0], _fold_expect(0x00, 0xFF, 0x00, 0))

    for k in range(8):
        frame = eng.colors(k * 1.0)
        check(f"rainbow frame {k}: 24 distinct colours",
              len(set(frame)), 24)

    # ---------------------------------------------------------------- keyframe integrity
    anim, kfs = effects.cycle_rainbow()
    check("cycle keyframes ≤ 10", len(kfs) <= 10, True)
    check("cycle kf0 t==0", kfs[0].t, 0)
    check("cycle last t==100", kfs[-1].t, 100)
    check("cycle wraps (first==last color)",
          (kfs[-1].r, kfs[-1].g, kfs[-1].b),
          (kfs[0].r, kfs[0].g, kfs[0].b))

    # ---------------------------------------------------------------- profiles round trip
    st = prof.LightingState(effect="steady", color="10AA55",
                            zone_colors={str(z): effects.hsl_hex(z * 15) for z in range(24)})
    prof.save_state(st)
    st2 = prof.load_state()
    check("state round trip", st2.color, "10AA55")
    check("state round trip zones", st2.zone_colors["7"], st.zone_colors["7"])

    st.apply_to(kb)  # must not raise
    check("apply_to(steady full) sends 24-zone grouped packets", len(kb.hid.sent) > 2, True)

    # rainbow state: band colours persist; re-apply keeps them
    rb = prof.LightingState(effect="rainbow", color="10AA55", speed=5.0,
                            direction=0, band_colors=bands)
    prof.save_state(rb)
    rb2 = prof.load_state()
    check("rainbow state round trip", rb2.band_colors, bands)
    check("rainbow state direction round trip", rb2.direction, 0)
    rb2.apply_to(kb)  # must not raise; re-uses the saved stream
    pkts = run(kb, rb2.apply_to, kb)
    effs = pkts[1::2]
    check("apply_to(rainbow) sends mask+effect per zone", len(pkts), NUM_ZONES * 2)
    check("apply_to(rainbow) first colours = saved stream",
          [bytes(_band_tbl(effs[c])[0][1:]) for c in range(4)],
          [rgb_of(bands[c]) for c in range(4)])
    check("apply_to(rainbow) direction from state", {p[9] for p in effs}, {0x00})

    # legacy wave-named states/profiles load as rainbow
    legacy = prof.LightingState(effect="wave", speed=6.0)
    prof.save_state(legacy)
    check("legacy 'wave' state migrates to rainbow",
          prof.load_state().effect, "rainbow")
    try:
        prof.LightingState(effect="wave").validate()
        check("'wave' no longer a valid effect name", False, True)
    except ValueError:
        check("'wave' no longer a valid effect name", True, True)

    print("\n" + ("ALL CHECKS PASSED ✓" if ok else "FAILURES ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
