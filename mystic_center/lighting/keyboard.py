"""MSI MysticLight 24-zone keyboard protocol (MS-1603, VID 1462 PID 1603).

Packet channel: 64-byte HID feature reports, report id 2 (the "cmd" report
declared in the device HID report descriptor). After each command the host
reads the 64-byte feature report id 1 (ack/status, best effort).

Commands (all mirrored from MSI Center's own module for this keyboard,
UC_Mystic Light.dll -> Class_24Zone):

  report 2, packet 0x01 — zone select:   32-byte bitmask (bit N = zone N)
  report 2, packet 0x02 — effect config: effect + speed + center + wave
                         params + colour balls (see effects.py header)
  report 2, packet 0x03 — all-sync initialise
  report 2, packet 0x04 — all-sync solid colour: r, g, b
  report 2, byte 160    — persist current lighting to the MCU (Save)
  report 2, byte 176    — revert to the MCU's saved lighting (Cancel)
  report 2, byte 255    — reset the MCU (recovery)
  report 1 with [145,1] — firmware version query
  report 1 with [160]   — keyboard language query (US/JP)

Zone count: 24. The MSI effect set for this keyboard: Off, Steady, Breath,
Color Cycle, Wave, Dance, Wheel, Bouncy Ball, plus a per-zone Customize
mode (24 individually configured zones) and all-sync commands.

Byte layout of the effect payload (index 0 = first byte after report id):

  idx 0 packet id (0x02) | idx 1 effect | idx 2-3 speed u16 LE
  idx 4 center X | idx 5 center Y | idx 6 wave length * 10
  idx 7 wave mode | idx 8 direction
  idx 9.. colour balls (position, r, g, b) x13 max

The legacy entry points (steady/breath/cycle/apply_effect) keep the byte-6/7
values of the original hardware-validated capture (50 / 0x08); the MSI
parity effects below pass the exact MSI Center parameter values.
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

    MAX_BALLS = 13  # 10-byte header + 4 bytes each must fit a 63-byte payload

    def _effect_packet(
        self,
        effect: int,
        balls,
        *,
        speed: int,
        center: tuple[int, int],
        wavelength_tenths: int,
        wave_mode: int,
        direction: int,
    ) -> bytes:
        if len(balls) > self.MAX_BALLS:
            raise ValueError(f"max {self.MAX_BALLS} colour balls per packet")
        payload = bytearray(63)
        payload[0] = 0x02
        payload[1] = effect & 0xFF
        payload[2] = speed & 0xFF
        payload[3] = (speed >> 8) & 0xFF
        payload[4] = center[0] & 0xFF
        payload[5] = center[1] & 0xFF
        payload[6] = wavelength_tenths & 0xFF
        payload[7] = wave_mode & 0xFF
        payload[8] = direction & 0xFF
        for i, (pos, r, g, b) in enumerate(balls):
            payload[9 + i * 4 : 9 + i * 4 + 4] = bytes(
                (pos & 0xFF, r & 0xFF, g & 0xFF, b & 0xFF))
        return bytes(payload)

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
        balls = [kf.as_bytes() for kf in kfs]
        self._send(self._effect_packet(
            anim, balls, speed=speed, center=(0, 0),
            wavelength_tenths=self.level, wave_mode=self.byte8, direction=d))

    def msi_effect(
        self,
        effect: int,
        balls=(),
        *,
        zones=None,
        speed: int = 600,
        center: tuple[int, int] = (0, 0),
        wavelength_tenths: int | None = None,
        wave_mode: int = 0x08,
        direction: int = 1,
    ) -> None:
        """One exact MSI Center Set_KeyEffect packet (effect 0..7).

        `balls` are (position, r, g, b) tuples from effects.balls_*().
        The default header (wavelength 50 / wave-mode 0x08) matches the
        hardware-validated capture for steady/breath/cycle — MSI Center
        itself also leaves those fields stale for those effects."""
        self.select_zones(zones)
        wl = effects.WAVELENGTH["legacy"] if wavelength_tenths is None \
            else int(wavelength_tenths)
        self._send(self._effect_packet(
            effect, list(balls), speed=int(speed), center=center,
            wavelength_tenths=wl, wave_mode=wave_mode, direction=direction))

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

    # --- MSI Center parity commands -------------------------------------------------

    def effect_off(self, zones=None) -> None:
        """Raw Off command (packet type 0) — MSI Center's Set_KeyEffect_Off."""
        self.select_zones(zones)
        payload = bytearray(63)
        payload[0] = 0x02
        payload[1] = effects.EFFECT_OFF
        self._send(bytes(payload))

    def dance(self, speed_s: float = 2.0) -> None:
        """MSI Dance (effect 5, whole board). MSI Center first clears every
        zone, then applies Dance with no explicit colours."""
        self.effect_off()
        self.msi_effect(effects.EFFECT_DANCE, (),
                        speed=int(round(speed_s * 100)))

    def mcu_save(self) -> None:
        """Persist the current lighting into keyboard memory ([2,160])."""
        self._send(bytes([0x02, 160]))

    def mcu_cancel(self) -> None:
        """Restore the keyboard's saved lighting ([2,176], MSI's Cancel)."""
        self._send(bytes([0x02, 176]))

    def mcu_reset(self) -> None:
        """Reset the keyboard MCU ([2,255]) — recovery for a wedged MCU."""
        self._send(bytes([0x02, 255]))

    def allsync_begin(self) -> None:
        """Start the all-sync handshake ([2,3])."""
        self._send(bytes([0x02, 0x03]))

    def allsync_rgb(self, r: int, g: int, b: int) -> None:
        """All-sync solid colour ([2,4,r,g,b])."""
        self._send(bytes([0x02, 0x04, r & 0xFF, g & 0xFF, b & 0xFF]))

    def allsync_rainbow(self) -> None:
        """All-sync rainbow preset: MSI's wave + 7-ball rainbow stream."""
        self.msi_effect(effects.EFFECT_WAVE, effects.ALLSYNC_RAINBOW_STREAM,
                        speed=500, wavelength_tenths=10, wave_mode=1,
                        direction=1)

    def firmware_version(self) -> bytes | None:
        """Query the MCU firmware version (report 1: [145,1]); raw reply."""
        self.hid.send_feature(bytes([145, 1]), report_id=1)
        return self.hid.read_ack()

    def language(self) -> str:
        """Keyboard layout language exactly as MSI Center reads it: US/JP."""
        self.hid.send_feature(bytes([160]), report_id=1)
        resp = self.hid.read_ack()
        if resp is not None and len(resp) > 2 and resp[2] == 2:
            return "JP"
        return "US"

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
