#!/usr/bin/env python3
"""Hardware-free validation: intercepts HID writes and checks packet bytes
against the reverse-engineered MSI Center capture for 1462:1603.

Run:  python3 tests/dry_test.py
"""

import os
import tempfile

os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()  # never touch real config in tests

import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mystic_center.lighting import effects           # noqa: E402
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

    # ---------------------------------------------------------------- breath / cycle / wave
    pkts = run(kb, kb.breath, "FF2040", speed=450)
    anim = pkts[1][2]
    speed = pkts[1][3] | (pkts[1][4] << 8)
    check("breath animation type 2", anim, 0x02)
    check("breath speed 450", speed, 450)
    check("breath kf0 color", pkts[1][11:14], bytes([0xFF, 0x20, 0x40]))

    pkts = run(kb, kb.cycle)
    check("cycle animation type 2? no — 3", pkts[1][2], 0x03)
    pkts = run(kb, kb.wave, direction=0)
    check("wave animation type 4", pkts[1][2], 0x04)
    check("wave direction R→L", pkts[1][9], 0x00)

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

    print("\n" + ("ALL CHECKS PASSED ✓" if ok else "FAILURES ✗"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
