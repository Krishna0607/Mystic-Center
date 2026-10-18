"""Software-driven 24-zone animations over the HID feature path.

The firmware's hardware wave is coarse (2–3 visible bands), so MSI-style
effects are painted per frame from software: each engine computes the 24
zone colors and they are sent with steady zone packets (grouped by color).

Column geometry: the 24 zones are laid out row-major with 4 columns per row
(zone i → row i//4, column i%4), hardware-verified on this machine. MSI's
Wave runs across the *columns*, so every zone in one column shares a hue and
the pattern drifts left/right over time.
"""

from __future__ import annotations

import colorsys
import math
import random
import time

from .keyboard import NUM_ZONES, MysticKeyboard

N_COLS = 4  # zone columns per hardware row


def hsl(hue: float, sat: float = 1.0, light: float = 0.55) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, light, sat)
    return int(r * 255), int(g * 255), int(b * 255)


def hexc(hue: float, sat: float = 1.0, light: float = 0.55) -> str:
    return "%02X%02X%02X" % hsl(hue, sat, light)


def quantize(color: str, step: int = 16) -> str:
    """Round each channel to a multiple of `step` so similar colors merge
    into one packet group (big win on the feature-report bus)."""
    if len(color) != 6:
        return color
    out = []
    for i in (0, 2, 4):
        v = int(color[i:i + 2], 16)
        q = (v + step // 2) // step * step
        out.append(max(0, min(255, q)))
    return "%02X%02X%02X" % tuple(out)


class RainbowWave:
    """MSI-style Wave: vertical rainbow bands sweeping across the columns.

    All zones in a column share a hue; adjacent columns are 90° apart, so at
    any instant the whole board spans the full rainbow, and the phase drifts
    smoothly over time (direction selects the travel direction)."""

    name = "wave"

    def __init__(self, direction: int = 1, speed: float = 6.0,
                 col_span: float = 90.0, light: float = 0.62):
        self.direction = 1 if direction == 1 else -1
        self.period = max(1.0, float(speed))  # seconds per full hue loop
        self.span = col_span
        self.light = light

    def colors(self, t: float) -> list[str]:
        drift = self.direction * (t / self.period) * 360.0
        return [
            hexc((z % N_COLS) * self.span + drift, 1.0, self.light)
            for z in range(NUM_ZONES)
        ]


class RainbowCycle:
    """Whole keyboard cycles hue together (one color at a time)."""

    name = "scycle"

    def __init__(self, speed: float = 6.0, sat: float = 1.0, light: float = 0.62):
        self.period = max(1.0, float(speed))
        self.sat, self.light = sat, light

    def colors(self, t: float) -> list[str]:
        c = hexc((t / self.period) * 360.0 * -1, self.sat, self.light)
        return [c] * NUM_ZONES


class RainbowBreathPulse:
    """Legacy: breathing pulse with slowly rotating hue (fixed base color)."""

    name = "spulse"

    def __init__(self, speed: float = 6.0, hue0: float = 210.0):
        self.period = max(1.0, float(speed))
        self.hue0 = hue0

    def colors(self, t: float) -> list[str]:
        phase = (t / self.period) % 1.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * phase)  # 0..1 breathing
        r, g, b = hsl(self.hue0 + (t / (self.period * 8)) * 360.0)
        c = "%02X%02X%02X" % (int(r * glow), int(g * glow), int(b * glow))
        return [c] * NUM_ZONES


class RainbowBreath:
    """Rainbow Breathing — the board breathes in brightness while the hue
    slowly rotates through the whole spectrum. Keeps a visible floor so it
    never reads as "frozen dark"."""

    name = "rbreath"

    def __init__(self, speed: float = 6.0, light: float = 0.62):
        self.period = max(1.0, float(speed))
        self.light = light

    def colors(self, t: float) -> list[str]:
        phase = (t / self.period) % 1.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
        glow = 0.45 + 0.55 * glow  # brightness floor: never dark-locked
        hue = (t / (self.period * 2.5)) * 360.0
        return [hexc(hue, 1.0, self.light * glow)] * NUM_ZONES


class Fire:
    """Warm flame flicker (OpenRGB-style): every zone is a small flame with
    its own phase; hot moments shift yellow, calm moments go deep red."""

    name = "fire"

    def __init__(self, speed: float = 6.0):
        self.rate = max(0.3, float(speed) / 6.0)
        rnd = random.Random(0x51DE)
        self.phase = [rnd.random() * 7.0 for _ in range(NUM_ZONES)]
        self.freq = [rnd.uniform(1.1, 2.4) for _ in range(NUM_ZONES)]

    def colors(self, t: float) -> list[str]:
        out = []
        for z in range(NUM_ZONES):
            w = 0.5 + 0.5 * math.sin(t * self.freq[z] * self.rate + self.phase[z])
            hue = 45.0 - 32.0 * w  # 45° (amber) hot → 13° (red) calm
            out.append(quantize(hexc(hue, 1.0, 0.42 + 0.42 * w), 20))
        return out


class Rain:
    """Digital rain (OpenRGB-style): random zones light up in a random hue
    and fade away. Colors are quantized so packet groups stay small."""

    name = "rain"

    def __init__(self, speed: float = 6.0):
        self.rate = max(0.3, float(speed) / 6.0)
        rnd = random.Random()
        self.val = [1.0 if rnd.random() < 0.30 else (0.4 if rnd.random() < 0.2
                                                     else 0.0)
                    for _ in range(NUM_ZONES)]
        self.hue = [rnd.randrange(360) for _ in range(NUM_ZONES)]
        self.prev_t = 0.0

    def _pix(self, z: int) -> str:
        if self.val[z] <= 0.0:
            return quantize("000000", 24)
        return quantize(hexc(self.hue[z], 1.0, 0.22 + 0.68 * self.val[z]), 20)

    def colors(self, t: float) -> list[str]:
        dt = min(0.35, max(0.0, t - self.prev_t))
        self.prev_t = t
        for z in range(NUM_ZONES):
            self.val[z] *= math.exp(-dt * 1.5 * self.rate)
            if self.val[z] < 0.03:
                self.val[z] = 0.0
            if random.random() < dt * 1.3 * self.rate:
                self.val[z] = 1.0
                self.hue[z] = random.randrange(360)
        return [self._pix(z) for z in range(NUM_ZONES)]


class Radar:
    """Radar sweep — a beam with a fading trail crossing left↔right."""

    name = "radar"

    def __init__(self, direction: int = 1, speed: float = 6.0, hue: float = 140.0):
        self.dir = 1 if direction == 1 else -1
        self.period = max(1.5, float(speed) * 1.2)
        self.hue = hue

    def colors(self, t: float) -> list[str]:
        x = ((t / self.period) % 1.0) * (N_COLS + 2.0)  # head incl. offscreen
        head = x if self.dir == 1 else (N_COLS + 2.0 - x)
        out = []
        for z in range(NUM_ZONES):
            col = z % N_COLS
            d = head - col
            val = 1.0 - d / 2.6 if 0.0 <= d <= 2.6 else 0.0
            if val <= 0.01:
                out.append(quantize("000000", 24))
            else:
                out.append(quantize(
                    hexc(self.hue + 25.0 * val, 1.0, 0.18 + 0.62 * val), 20))
        return out


class Lightning:
    """Storm — mostly darkness; short warm-white flashes strike random
    zones and decay quickly. If a zone is struck twice the flash stacks."""

    name = "lightning"

    FLASH_HUE = 46.0
    FLASH_SAT = 0.35

    def __init__(self, speed: float = 6.0):
        self.freq = max(0.4, float(speed) / 6.0)
        self.next_strike = 0.5
        self.flashes: list[tuple[int, float, float]] = []  # zone, start, dur
        self.prev_t = 0.0

    def _decay(self, zone: int, t: float) -> float:
        best = 0.0
        for (fz, start, dur) in self.flashes:
            if fz != zone:
                continue
            age = t - start
            if age < 0:
                continue
            if age <= dur:
                best = max(best, 1.0 - (age / dur) * 0.30)
            else:
                rem = (age - dur) / 0.40  # tail decay over ~0.4 s
                best = max(best, max(0.0, 0.70 * (1.0 - rem)))
        return best

    def colors(self, t: float) -> list[str]:
        dt = min(0.35, max(0.0, t - self.prev_t))
        self.prev_t = t
        if t >= self.next_strike:
            self.flashes.append(
                (random.randrange(NUM_ZONES), t, random.uniform(0.10, 0.28)))
            self.next_strike = t + random.uniform(1.1, 3.2) / self.freq
        self.flashes = [f for f in self.flashes if t - f[1] < f[2] + 0.55]
        out = []
        for z in range(NUM_ZONES):
            val = self._decay(z, t)
            if val <= 0.02:
                out.append(quantize("000000", 24))
            else:
                out.append(quantize(
                    hexc(self.FLASH_HUE, self.FLASH_SAT,
                         0.45 + 0.45 * val), 20))
        return out


_ENGINES: dict[str, type] = {
    "wave": RainbowWave, "swave": RainbowWave, "rainbow-wave": RainbowWave,
    "scycle": RainbowCycle, "rainbow-cycle": RainbowCycle,
    "spulse": RainbowBreathPulse, "rbreath": RainbowBreath,
    "rain": Rain, "fire": Fire, "radar": Radar, "lightning": Lightning,
}

_SOFTWARE_EFFECTS = ("wave", "swave", "scycle", "spulse",
                     "rbreath", "rain", "fire", "radar", "lightning")


def is_software(effect: str) -> bool:
    return effect in _SOFTWARE_EFFECTS


def get_engine(name: str, *, speed: float = 6.0, direction: int = 1):
    cls = _ENGINES.get(name)
    if cls is None:
        raise KeyError(f"no software engine named {name!r}")
    if cls in (RainbowWave, Radar):
        return cls(direction=direction, speed=speed)
    if cls is RainbowBreathPulse:
        return cls(speed=speed, hue0=210.0)
    return cls(speed=speed)


def run(engine_name: str, kb: MysticKeyboard, *, speed: float = 6.0,
        direction: int = 1, fps: int = 12) -> None:
    """Block forever painting frames. Exits cleanly on SIGTERM/SIGINT.

    Robust against device hiccups: transient write failures never kill the
    animator mid-packet (which would leave the MCU half-painted); only after
    40 consecutive failed frames it gives up and closes the device."""
    import signal

    eng = get_engine(engine_name, speed=speed, direction=direction)
    stop = {"v": False}

    def _term(*_a):
        stop["v"] = True

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    frame_min = max(0.08, 1.0 / max(1, fps))  # see below: pace + floor
    t0 = time.monotonic()
    consecutive_fail = 0
    while not stop["v"]:
        t = time.monotonic() - t0
        try:
            colors = eng.colors(t)
            kb.steady_zones({z: colors[z] for z in range(NUM_ZONES)})
            consecutive_fail = 0
        except Exception:
            consecutive_fail += 1
            time.sleep(0.05)
            if consecutive_fail >= 40:
                break  # device genuinely unhealthy; exit with the flag up
            continue
        next_t = time.monotonic() + frame_min
        # paced wait, resumable by SIGTERM (signal wakes the sleep)
        while not stop["v"] and time.monotonic() < next_t:
            time.sleep(min(0.04, max(0.004, next_t - time.monotonic())))
    kb.off()


if __name__ == "__main__":
    import argparse

    from .hid import MysticHid

    ap = argparse.ArgumentParser(prog="mystic-animate")
    ap.add_argument("engine", choices=sorted(set(_SOFTWARE_EFFECTS)))
    ap.add_argument("--speed", type=float, default=6.0)
    ap.add_argument("--dir", type=int, default=1)
    ap.add_argument("--fps", type=int, default=12)
    a = ap.parse_args()
    kb = MysticKeyboard()
    try:
        run(a.engine, kb, speed=a.speed, direction=a.dir, fps=a.fps)
    finally:
        kb.hid.close()
