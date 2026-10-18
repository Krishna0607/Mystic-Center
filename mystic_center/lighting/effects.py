"""Color utilities and effect keyframe generators.

Protocol reference (reverse engineered from the MS-1565 protocol gist by
natanalt + MS-1603 MSI Center captures documented in the MSI-KeyLight repo):

  Effect packet (feature report id 2, packet type 0x02):
    [1] 0x02  packet id = reconfigure effect
    [2]       animation type: 0 off, 1 steady, 2 breath, 3 color cycle, 4 wave
    [3..4]    speed u16 little endian = cycle seconds * 100 (MSI Center: 600)
    [5..6]    0x00
    [7]       level-ish byte: MSI Center steady = 0x32 (exposed as "level")
    [8]       0x01
    [9]       direction: 0 = right-to-left, 1 = left-to-right
    [10..]    keyframes: u8 time%, u8 r, u8 g, u8 b  (up to 10; first t=0,
              last t=100; for seamless loops last color == first color)
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
