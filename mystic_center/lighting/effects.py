"""Color utilities, effect keyframe generators and MSI Center effect parity.

Protocol reference (verified against MSI Center 2.0.74's own code —
UC_Mystic Light.dll, ``Class_24Zone``, the module that drives this exact
keyboard, VID_1462&PID_1603):

  Effect packet (feature report id 2, packet type 0x02):
    [1] 0x02  packet id = reconfigure effect
    [2]       animation type: 0 off, 1 steady, 2 breath, 3 color cycle,
              4 wave, 5 dance, 6 wheel, 7 bouncy ball
    [3..4]    speed u16 little endian = cycle seconds * 100
    [5]       key-center X
    [6]       key-center Y
    [7]       wave length * 10
    [8]       wave mode: 1 = full-screen/rainbow, 0 = part-screen/custom
    [9]       direction (0/1; meaning depends on the effect)
    [10..]    color balls: u8 position, u8 r, u8 g, u8 b  (up to 13)

  Earlier revisions of this file read bytes 7/8/9 as "level / constant /
  direction" from an MS-1565 capture; the MSI Center module for the
  24-zone keyboards proves they are wave-length, wave-mode and direction,
  and bytes 10+ are colour *positions* (0..100), not time percentages.
  The byte layout is unchanged, so hardware-validated traffic still holds;
  steady/breath/cycle keep their historically-captured byte-7/8 values
  (50 / 0x08) because MSI Center itself leaves those fields at stale
  defaults for those effects.

MSI Center parameter tables (also from Class_24Zone) live in this module:
speed presets, direction codes, rainbow streams, colour-ball builders.
"""

from __future__ import annotations

import colorsys
import random
from dataclasses import dataclass


@dataclass(frozen=True)
class Keyframe:
    t: int  # 0..100
    r: int
    g: int
    b: int

    def as_bytes(self) -> tuple[int, int, int, int]:
        return (self.t, self.r, self.g, self.b)


def hex_to_rgb(hexstr: str) -> tuple[int, int, int]:
    s = hexstr.strip().lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) != 6:
        raise ValueError(f"bad color hex: {hexstr!r}")
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    return f"{r:02X}{g:02X}{b:02X}"


def scale(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(c * factor))) for c in rgb)


def hsl_hex(h: float, s: float = 1.0, l: float = 0.55) -> str:
    r, g, b = colorsys.hls_to_rgb(((h % 360.0) / 360.0), l, s)
    return rgb_to_hex(int(r * 255), int(g * 255), int(b * 255))


def gradient(n: int, sat: float = 1.0, light: float = 0.55, shifted: bool = False) -> list[str]:
    """n colors sampled evenly across the hue circle (for per-zone painting)."""
    return [hsl_hex((i / n) * 360.0, sat, light) for i in range(n)]


# --- Effects: each returns (animation_type, keyframes) --------------------------------


def steady(color: str) -> tuple[int, list[Keyframe]]:
    """MSI Center sends exactly: speed=600, kf0(t=0,c), kf1(t=100,c)."""
    r, g, b = hex_to_rgb(color)
    return 1, [Keyframe(0, r, g, b), Keyframe(100, r, g, b)]


def off() -> tuple[int, list[Keyframe]]:
    """Disabled animation (type 0). Some firmwares keep last keyframe color,
    so callers should also paint black steady when they want a real Off."""
    return 0, []


def breath(color: str) -> tuple[int, list[Keyframe]]:
    """Fade out to near-black and back (seamless: ends on the start color)."""
    r, g, b = hex_to_rgb(color)
    dim = scale((r, g, b), 1 / 6)
    return 2, [
        Keyframe(0, r, g, b),
        Keyframe(50, *dim),
        Keyframe(100, r, g, b),
    ]


def cycle_rainbow(
    sat: float = 1.0, light: float = 0.55, stops: int = 8
) -> tuple[int, list[Keyframe]]:
    """Rainbow color cycle: 8 evenly spaced hues + wrap to the first color."""
    kfs: list[Keyframe] = []
    for i in range(stops):
        t = int(round(i * 100.0 / stops))
        r, g, b = hex_to_rgb(hsl_hex((i / stops) * 360.0, sat, light))
        kfs.append(Keyframe(t, r, g, b))
    # close the loop: hue 360 == hue 0 color at t=100 (MCU does not wrap colors)
    r, g, b = hex_to_rgb(hsl_hex(0.0, sat, light))
    kfs.append(Keyframe(100, r, g, b))
    return 3, kfs


# --- MSI Center 24-zone effect parity ------------------------------------------------
#
# Everything below mirrors `Class_24Zone` in UC_Mystic Light.dll (MSI Center
# 2.0.74) — the exact parameter tables, colour-ball builders and command
# semantics MSI Center uses for VID_1462&PID_1603. See docs/PROTOCOL.md.

EFFECT_OFF = 0
EFFECT_STEADY = 1
EFFECT_BREATH = 2
EFFECT_CYCLE = 3
EFFECT_WAVE = 4
EFFECT_DANCE = 5
EFFECT_WHEEL = 6
EFFECT_BOUNCY = 7

EFFECT_NAMES = {
    "off": "Off",
    "steady": "Steady",
    "breath": "Breathing",
    "cycle": "Color Cycle",
    "wave": "Wave",
    "dance": "Dance",
    "wheel": "Wheel",
    "bouncy": "Bouncy Ball",
}

# Speed radio (0 slow, 1 medium, 2 fast) -> cycle seconds, per effect.
# MSI Center: wave/wheel 5 / 3.5 / 2 s; dance 3 / 2 / 1 s; others 8 / 6 / 4 s.
# Steady has no speed control; bouncy ball is fixed at 1.0 s.
SPEED_TABLE = {
    "steady": (6.0, 6.0, 6.0),   # not sent as a control; firmware ignores it
    "breath": (8.0, 6.0, 4.0),
    "cycle": (8.0, 6.0, 4.0),
    "wave": (5.0, 3.5, 2.0),
    "wheel": (5.0, 3.5, 2.0),
    "dance": (3.0, 2.0, 1.0),
    "bouncy": (1.0, 1.0, 1.0),   # fixed 1.0 s in MSI Center
}
SPEED_LABELS = ("Slow", "Medium", "Fast")

# Direction codes from MSI's Enum_Direction (values used on the wire).
DIR_LEFT = 2       # wave left  -> center (0,50), direction byte 0
DIR_RIGHT = 3      # wave right -> center (0,50), direction byte 1
DIR_CW = 4         # wheel/bouncy clockwise        -> byte 0
DIR_CCW = 5        # wheel/bouncy counterclockwise -> byte 1
DIRECTION_LABELS = {DIR_LEFT: "Left", DIR_RIGHT: "Right",
                    DIR_CW: "Clockwise", DIR_CCW: "Counterclockwise"}

# Wave length byte (payload[7]) per effect, MSI Center values * 10.
WAVELENGTH = {"wave": 8, "wheel": 5, "wheel_custom": 3,
              "bouncy": 2, "bouncy_custom": 3,
              "legacy": 50}  # steady/breath/cycle: MSI leaves the stale 50

# Rainbow colour streams (position, r, g, b).  MSI Center's
# Set_ColorBall_Rainbow: wave / color-cycle / wheel get the 7-ball stream;
# bouncy ball gets a black-in black-out list; sync uses 16% spacing.
RAINBOW_STREAM = ((0, 255, 0, 0), (17, 255, 255, 0), (33, 0, 255, 0),
                  (50, 0, 255, 255), (67, 0, 0, 255), (83, 255, 0, 255),
                  (100, 255, 0, 0))
ALLSYNC_RAINBOW_STREAM = ((0, 255, 0, 0), (16, 255, 255, 0), (32, 0, 255, 0),
                          (48, 0, 255, 255), (64, 0, 0, 255),
                          (80, 255, 0, 255), (100, 255, 0, 0))
BOUNCY_RAINBOW_STREAM = ((0, 0, 0, 0), (100, 0, 0, 0))

# Steady/Breath "Rainbow" radio: MSI Center lights 9 fixed hue groups.
ZONE_RAINBOW_GROUPS = ((0, 11, 12), (1, 13, 14), (2, 15), (3, 16, 17),
                       (4,), (5, 18, 19), (6,), (7, 8, 20, 21),
                       (9, 10, 22, 23))
ZONE_RAINBOW_COLORS = ("FF0000", "FF8000", "80FF00", "00FF00", "00FF80",
                       "0080FF", "0000FF", "8000FF", "FF0080")


def _ball(position: int, color) -> tuple[int, int, int, int]:
    r, g, b = hex_to_rgb(color) if isinstance(color, str) else tuple(color)[:3]
    return (position & 0xFF, r & 0xFF, g & 0xFF, b & 0xFF)


def balls_default(colors) -> list[tuple[int, int, int, int]]:
    """MSI default colour balls: colours spread at 100//n steps, wrap to the
    first colour at position 100 (one seamless loop)."""
    if not colors:
        raise ValueError("need at least one colour")
    n = len(colors)
    balls = [_ball(100 // n * i, c) for i, c in enumerate(colors)]
    balls.append(_ball(100, colors[0]))
    return balls


def balls_wheel(colors) -> list[tuple[int, int, int, int]]:
    """MSI wheel balls: black in, up to 3 colours at 15/50/85, black out."""
    balls = [_ball(0, "000000")]
    for j, c in enumerate(colors[:3]):
        balls.append(_ball(j * 35 + 15, c))
    balls.append(_ball(100, "000000"))
    return balls


def balls_bouncy(colors) -> list[tuple[int, int, int, int]]:
    """MSI bouncy-ball balls: black in, first colour mid-track, black out."""
    return [_ball(0, "000000"), _ball(50, colors[0]), _ball(100, "000000")]


def balls_zone_rainbow(group: int) -> list[tuple[int, int, int, int]]:
    """The two-ball (solid) colour list for one of the 9 rainbow groups."""
    c = ZONE_RAINBOW_COLORS[group]
    return [_ball(0, c), _ball(100, c)]


def balls_for_effect(effect: str, color_mode: str,
                     colors) -> list[tuple[int, int, int, int]]:
    """Colour balls exactly as MSI's Set_ColorBall_Normal / _Rainbow would.

    `color_mode` is "rainbow" (the Rainbow radio) or "customized".
    Steady/Breath rainbow is handled by the zone groups (see
    ZONE_RAINBOW_GROUPS), callers pass those separately."""
    if color_mode == "rainbow":
        if effect == "bouncy":
            return list(BOUNCY_RAINBOW_STREAM)
        return list(RAINBOW_STREAM)
    if effect == "wheel":
        return balls_wheel(colors)
    if effect == "bouncy":
        return balls_bouncy(colors)
    return balls_default(colors)


def zone_rainbow_colors() -> list[str]:
    """24 zone colours for the Steady/Breath rainbow radio (GUI preview)."""
    out = ["000000"] * 24
    for group, zones in enumerate(ZONE_RAINBOW_GROUPS):
        for z in zones:
            out[z] = ZONE_RAINBOW_COLORS[group]
    return out


def dim_hex(hexstr: str, factor: float) -> str:
    return rgb_to_hex(*scale(hex_to_rgb(hexstr), factor))


def sample_balls(balls, position: float) -> str:
    """Piecewise-linear colour at `position` (0..100) inside a ball list."""
    pts = sorted(balls, key=lambda b: b[0])
    if not pts:
        return "000000"
    if position <= pts[0][0]:
        return rgb_to_hex(*pts[0][1:4])
    for (p0, r0, g0, b0), (p1, r1, g1, b1) in zip(pts, pts[1:]):
        if position <= p1:
            if p1 <= p0:
                return rgb_to_hex(r1, g1, b1)
            t = (position - p0) / (p1 - p0)
            return rgb_to_hex(
                int(round(r0 + (r1 - r0) * t)),
                int(round(g0 + (g1 - g0) * t)),
                int(round(b0 + (b1 - b0) * t)))
    return rgb_to_hex(*pts[-1][1:4])


# Default colours per effect, matching MSI Center's shipped defaults.
DEFAULT_COLORS = {
    "steady": ("00FFFF",),
    "breath": ("00FF80", "FFB90F"),
    "cycle": ("FF00FF", "FFFF00", "00FFFF"),
    "wave": ("FF0000", "FF00FF", "9696FF", "000000", "000000"),
    "wheel": ("FF0000", "FFFF00", "00FFFF"),
    "bouncy": ("00FFFF",),
}


# --- Rainbow (firmware band conveyor) ----------------------------------------------------
#
# A stream of at most 5 randomly picked band colours marches across the deck.
# The deck shows four of them at a time — one per zone-column (the 24 zones
# are row-major, so the travel axis is z % 4 and all six rows of a column
# share one colour) — and a new band enters at the trailing edge exactly when
# the leading band exits. Firmware-driven: one colour cycle (animation
# type 3) per zone, every column's table starting one band slot further
# along the stream, so the pattern slides sideways one band step at a time.

BAND_COUNT = 5  # colours in the rotation; the deck displays four


def random_band_colors(n: int = BAND_COUNT, min_sep: float = 40.0) -> list[str]:
    """n vivid full-saturation colours whose hues are pairwise >= min_sep apart."""
    hues: list[float] = []
    while len(hues) < n:
        h = random.uniform(0.0, 360.0)
        if all(min((h - o) % 360.0, (o - h) % 360.0) >= min_sep for o in hues):
            hues.append(h)
    out: list[str] = []
    for h in hues:
        r, g, b = colorsys.hsv_to_rgb(h / 360.0, 1.0, 1.0)
        out.append(rgb_to_hex(int(round(r * 255)), int(round(g * 255)),
                              int(round(b * 255))))
    return out


def band_keyframes(colors, direction: int = 1,
                   offset: int = 0) -> list[Keyframe]:
    """Keyframes for one full lap of the rainbow band conveyor.

    colors: the stream in order (2..5 hex strings or RGB tuples, each band
    one zone-column wide). direction 1 = the stream travels left→right, so a
    fixed column runs *backwards* through the stream (earlier bands arrive).
    offset shifts the starting slot — one band (100/n %) per zone-column,
    which is what makes the pattern slide.

    Shape (fits the MCU's 10-keyframe limit exactly): each of the first n−1
    bands holds solid then blends ~1/5 of its slot into the next; the final
    band starts at its slot and blends across into keyframe 0 as the cycle
    wraps (last keyframe = 100 at the stream's first colour, the seamless-
    loop convention)."""
    palette = [hex_to_rgb(c) if isinstance(c, str) else tuple(c[:3])
               for c in colors]
    n = max(2, len(palette))
    rotate = -1 if direction == 1 else 1
    seg = 100 // n                    # percent of the cycle per band
    fade = max(1, min(seg // 5, seg - 2))  # blend zone between bands
    slot = [(offset + rotate * k) % n for k in range(n)]
    kfs = [Keyframe(0, *palette[slot[0]])]
    for k in range(n - 1):
        kfs.append(Keyframe(k * seg + seg - fade, *palette[slot[k]]))
        kfs.append(Keyframe((k + 1) * seg, *palette[slot[k + 1]]))
    kfs.append(Keyframe(100, *palette[slot[0]]))
    return kfs
