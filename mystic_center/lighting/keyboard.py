"""MSI MysticLight 24-zone keyboard protocol (MS-1603, VID 1462 PID 1603).

Packet channel: 64-byte HID feature reports, report id 2 (the "cmd" report
declared in the device HID report descriptor). After each command the host
reads the 64-byte feature report id 1 (ack/status, best effort).

Command packets (63-byte payload after the report id byte):
  type 0x01 — zone select:   u32 little-endian bitmask, bit N = zone N
  type 0x02 — effect config: see effects.py header for full layout

Zone count: 24. Physical zone order (left→right / row-major?) must be
verified visually with `mystic-control sweep`.

Byte layout of the effect payload (index 0 = first byte after report id,
aligned with the MS-1565 gist offsets shifted by one):

  idx 1 packet id (0x02) | idx 2 anim type | idx 3-4 speed u16 LE
  idx 5-6 zeros          | idx 7 level (MSI Center: 0x32)
  idx 8 constant (MSI Center: 0x08)        | idx 9 direction (1 = L->R)
  idx 10.. keyframes (time%, r, g, b) x10 max

This reproduces the captured MSI Center steady packet exactly.
"""

from __future__ import annotations

import logging
import sys
import time

from . import effects
from .effects import Keyframe
from .hid import MysticHid

log = logging.getLogger(__name__)

NUM_ZONES = 24

ANIM_OFF = 0
ANIM_STEADY = 1
ANIM_BREATH = 2
ANIM_CYCLE = 3
ANIM_WAVE = 4


class MysticKeyboard:
    def __init__(
        self,
        hid: MysticHid | None = None,
        *,
        vid: int = 0x1462,
        pid: int = 0x1603,
        debug: bool = False,
    ):
        self.debug = debug
        self.vid = vid
        self.pid = pid
        if hid is None:
            hid = MysticHid.open_first(vid, pid)
        self.hid = hid

        # Observed MSI Center steady-mode constants we don't fully understand:
        # byte idx 7 ("level"): MSI Center sends 0x32 (50) — may be brightness
        # or transition; byte idx 8 constant: MSI Center sends 0x08.
        self.level = 50
        self.byte8 = 0x08
        self.direction = 1  # 1 = left→right, 0 = right→left

    # --- low level ---------------------------------------------------------------

    def _send(self, payload: bytes) -> None:
        if self.debug:
            print(f"TX  {' '.join(f'{b:02X}' for b in payload)}", file=sys.stderr)
        ok = self.hid.send_feature(payload)
        if not ok:
            raise RuntimeError("SET_FEATURE incomplete — device rejected packet")
        # The protocol notes an ack read after every packet; also paces the MCU
        # during rapid multi-zone updates.
        ack = self.hid.read_ack()
        if self.debug and ack is not None:
            print(f"ACK {' '.join(f'{b:02X}' for b in ack)}", file=sys.stderr)

    @staticmethod
    def _mask(zones) -> int:
        zones = None if zones is None else list(zones)
        if zones is None:
            zones = range(NUM_ZONES)
        mask = 0
        for z in zones:
            if not (0 <= z < NUM_ZONES):
                raise ValueError(f"zone index out of range: {z} (0..{NUM_ZONES - 1})")
            mask |= 1 << z
        return mask

    # --- commands ----------------------------------------------------------------

    def select_zones(self, zones) -> None:
        """Send zone-select mask packet (packet type 1)."""
        m = self._mask(zones)
        self._send(
            bytes([0x01, m & 0xFF, (m >> 8) & 0xFF, (m >> 16) & 0xFF, (m >> 24) & 0xFF])
        )

    def apply_effect(
        self,
        anim: int,
        kfs: list[Keyframe],
        *,
        zones=None,
        speed: int = 600,
        direction: int | None = None,
    ) -> None:
        self.select_zones(zones)
        d = self.direction if direction is None else direction
        payload = bytearray(63)
        payload[0] = 0x02
        payload[1] = anim & 0xFF
        payload[2] = speed & 0xFF
        payload[3] = (speed >> 8) & 0xFF
        payload[4] = 0x00
        payload[5] = 0x00
        payload[6] = self.level & 0xFF
        payload[7] = self.byte8
        payload[8] = d & 0xFF
        if len(kfs) > 10:
            raise ValueError("max 10 keyframes per packet")
        for i, kf in enumerate(kfs):
            t, r, g, b = kf.as_bytes()
            payload[9 + i * 4 : 9 + i * 4 + 4] = bytes((t, r, g, b))
        self._send(bytes(payload))

    # --- high level ---------------------------------------------------------------

    def steady(self, color: str, zones=None, speed: int = 600) -> None:
        anim, kfs = effects.steady(color)
        self.apply_effect(anim, kfs, zones=zones, speed=speed)

    def steady_zones(self, zone_colors: dict[int, str], speed: int = 600) -> None:
        """Paint individual zones with their own colors (per-zone mode)."""
        if not zone_colors:
            return
        by_color: dict[str, list[int]] = {}
        for z, c in zone_colors.items():
            by_color.setdefault(c, []).append(int(z))
        for color, zones in by_color.items():
            anim, kfs = effects.steady(color)
            self.apply_effect(anim, kfs, zones=zones, speed=speed)

    def off(self, zones=None) -> None:
        """Turn lights off with a black steady packet (works on all firmware)."""
        self.steady("#000000", zones=zones)

    def disable(self, zones=None) -> None:
        """Raw animation-disable packet (type 0) — experimental."""
        self.select_zones(zones)
        payload = bytearray(63)
        payload[0] = 0x02
        payload[1] = ANIM_OFF
        payload[2] = 0x58
        payload[3] = 0x02
        payload[6] = self.level & 0xFF
        payload[7] = self.byte8
        payload[8] = self.direction
        self._send(bytes(payload))

    def breath(self, color: str, zones=None, speed: int = 600) -> None:
        anim, kfs = effects.breath(color)
        self.apply_effect(anim, kfs, zones=zones, speed=speed)

    def cycle(self, zones=None, speed: int = 600) -> None:
        anim, kfs = effects.cycle_rainbow()
        self.apply_effect(anim, kfs, zones=zones, speed=speed)

    def rainbow(self, colors, speed: int = 500, direction: int | None = None,
                zones=None) -> None:
        """Rainbow band conveyor, run entirely by the keyboard firmware.

        `colors` is the stream in order (up to 5 colours); the deck shows
        four of them at a time — one clean colour per zone-column, all six
        rows of a column matching — and the stream slides sideways:
        direction 1 = left→right. A colour only enters at the trailing edge
        when the leading colour has fully exited. Every zone gets its own
        colour cycle (animation type 3) whose band table starts one band
        slot further per column. `speed` = the packet byte: full 5-band lap
        in seconds × 100. One burst of packets — nothing keeps running on
        the host afterwards."""
        d = self.direction if direction is None else direction
        targets = list(range(NUM_ZONES)) if zones is None else list(zones)
        for z in targets:
            kfs = effects.band_keyframes(colors, d, offset=z % 4)
            self.apply_effect(ANIM_CYCLE, kfs, zones=[z], speed=speed,
                              direction=d)

    # --- legacy direct-output path (per-zone writes, no mask/select race) --------

    def steady_zone_out(self, zone: int, color: str) -> None:
        """Paint a single zone with a raw OUTPUT report (report id 1):
        [1][zone][r][g][b] + zero padding. One packet per zone, no state."""
        r, g, b = effects.hex_to_rgb(color)
        if not (0 <= zone < NUM_ZONES):
            raise ValueError(f"zone index out of range: {zone}")
        self.hid.write_output(bytes([zone, r, g, b]), report_id=1)

    # --- diagnostics ----------------------------------------------------------------

    def sweep(self, step_s: float = 0.9) -> None:
        """Light each zone white in order so the user can map zones visually."""
        for z in range(NUM_ZONES):
            print(f"zone {z:>2} is now lit — note which area lights up", flush=True)
            self.steady("#FFFFFF", zones=[z])
            time.sleep(step_s)
        self.off()
