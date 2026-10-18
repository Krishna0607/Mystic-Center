"""Software-driven 24-zone animations over the HID feature path.

The firmware's hardware wave (animation type 4) is coarse (2–3 visible bands
on the 24-zone keyboard), so fine-grained effects paint every zone per frame
using zone-select + steady packets (the same MSI Center color packets).

Frame loop: for each frame, compute 24 zone colors and send them via
MysticKeyboard.steady_zones (which groups zones by color → 2 packets/group).
"""

from __future__ import annotations

import colorsys
import time

from .keyboard import NUM_ZONES, MysticKeyboard


def hsl(hue: float, sat: float = 1.0, light: float = 0.55) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, light, sat)
    return int(r * 255), int(g * 255), int(b * 255)


class RainbowWave:
    """Continuous rainbow moving physically across the 24 zones."""

    name = "swave"

    def __init__(self, direction: int = 1, speed: float = 6.0,
                 sat: float = 1.0, light: float = 0.55):
        self.direction = 1 if direction == 1 else -1
        self.period = max(1.0, float(speed))

    def colors(self, t: float) -> list[str]:
        shift = 360.0 * self.direction * (t / self.period)
        return [
            "%02X%02X%02X" % hsl((z / NUM_ZONES) * 360.0 + shift)
            for z in range(NUM_ZONES)
        ]


class RainbowCycle:
    """Whole keyboard cycles hue together (one color at a time)."""

    name = "scycle"

    def __init__(self, speed: float = 6.0, sat: float = 1.0, light: float = 0.55):
        self.period = max(1.0, float(speed))
        self.sat, self.light = sat, light

    def colors(self, t: float) -> list[str]:
        r, g, b = hsl((t / self.period) * 360.0 * -1, self.sat, self.light)
        c = "%02X%02X%02X" % (r, g, b)
        return [c] * NUM_ZONES


class RainbowBreathPulse:
    """Breathing pulse with slowly rotating hue (24-zone soft glow)."""

    name = "spulse"

    def __init__(self, speed: float = 6.0, hue0: float = 210.0):
        self.period = max(1.0, float(speed))
        self.hue0 = hue0

    def colors(self, t: float) -> list[str]:
        import math

        phase = (t / self.period) % 1.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * phase)  # 0..1 breathing
        r, g, b = hsl(self.hue0 + (t / (self.period * 8)) * 360.0)
        c = "%02X%02X%02X" % (int(r * glow), int(g * glow), int(b * glow))
        return [c] * NUM_ZONES


def get_engine(name: str, *, speed: float = 6.0, direction: int = 1):
    if name in ("swave", "rainbow-wave"):
        return RainbowWave(direction=direction, speed=speed)
    if name in ("scycle", "rainbow-cycle"):
        return RainbowCycle(speed=speed)
    if name in ("spulse", "rainbow-pulse"):
        return RainbowBreathPulse(speed=speed, hue0=210.0)
    raise KeyError(f"no software engine named {name!r}")


def run(engine_name: str, kb: MysticKeyboard, *, speed: float = 6.0,
        direction: int = 1, fps: int = 12) -> None:
    """Block forever painting frames. Exits cleanly on SIGTERM/SIGINT."""
    import signal

    eng = get_engine(engine_name, speed=speed, direction=direction)
    stop = {"v": False}

    def _term(*_a):
        stop["v"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    frame_min = 1.0 / max(1, fps)
    t0 = time.monotonic()
    while not stop["v"]:
        t = time.monotonic() - t0
        colors = eng.colors(t)
        kb.steady_zones({z: colors[z] for z in range(NUM_ZONES)})
        next_t = time.monotonic() + frame_min
        while not stop["v"] and time.monotonic() < next_t:
            time.sleep(0.004)
    kb.off()


if __name__ == "__main__":
    import argparse

    from .hid import MysticHid

    ap = argparse.ArgumentParser(prog="mystic-animate")
    ap.add_argument("engine", choices=["swave", "scycle", "spulse"])
    ap.add_argument("--speed", type=float, default=6.0)
    ap.add_argument("--dir", type=int, default=1)
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args()
    kb = MysticKeyboard()
    try:
        run(a.engine, kb, speed=a.speed, direction=a.dir, fps=a.fps)
    finally:
        kb.hid.close()
