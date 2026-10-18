"""Software-driven 24-zone animations over the HID feature path.

The firmware's hardware wave is coarse (2–3 visible bands), so MSI-style
effects are painted per frame from software: each engine computes the 24
zone colors and they are sent with steady zone packets.

Packet contract (validated on this machine's firmware): the device refreshes
zone colors reliably when every frame carries *distinct* colors on
single-zone masks; large shared masks with repeated effect-config packets
are cached by the MCU and get dropped, which stalls animation. Every engine
therefore produces 24 distinct per-zone colors per frame — the invisible
`hexc_unique` folds the zone index into the low bits of red/blue so even
uniform or dark frames keep every zone on its own mask.

Frame loop: each frame, the engine's 24 colors go out as one
MysticKeyboard.steady_zones call (a packet per zone).
"""

from __future__ import annotations

import colorsys
import math
import random
import time

from .keyboard import NUM_ZONES, MysticKeyboard

N_COLS = 4  # zone columns per hardware row (row-major: zone i → row i//4)


def hsl(hue: float, sat: float = 1.0, light: float = 0.60) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb((hue % 360.0) / 360.0, light, sat)
    return int(r * 255), int(g * 255), int(b * 255)


def hexc(hue: float, sat: float = 1.0, light: float = 0.60) -> str:
    return "%02X%02X%02X" % hsl(hue, sat, light)


def hexc_unique(hue: float, sat: float, light: float, z: int) -> str:
    """Per-zone color that never collides across the 24 zones.

    Folds the zone index into the low bits of red/blue (≤7/255 = ~3% delta,
    invisible on a lit LED). With a shared (hue, sat, light), every zone
    yields a different byte pair — the packet contract for animation."""
    r, g, b = hsl(hue, sat, light)
    r = (r & 0b11111100) | (z & 0x03)
    b = (b & 0b11111000) | ((z >> 2) & 0x07)
    return "%02X%02X%02X" % (r, g, b)


class RainbowWave:
    """Wave: full rainbow across all 24 zones (row-major), sliding smoothly
    over time. The original validated geometry. Hue span covers the MSI
    palette: red → orange → yellow → green → cyan → blue → violet/magenta."""

    name = "wave"

    def __init__(self, direction: int = 1, speed: float = 6.0,
                 light: float = 0.60):
        self.direction = 1 if direction == 1 else -1
        self.period = max(1.0, float(speed))  # seconds per full hue lap
        self.light = light

    def colors(self, t: float) -> list[str]:
        shift = 360.0 * self.direction * (t / self.period)
        return [
            hexc_unique((z / NUM_ZONES) * 360.0 + shift, 1.0, self.light, z)
            for z in range(NUM_ZONES)
        ]


class RainbowCycle:
    """Whole keyboard cycles hue through the spectrum together (the subtle
    per-zone low-bit fold keeps every packet single-zone)."""

    name = "scycle"

    def __init__(self, speed: float = 6.0, sat: float = 1.0, light: float = 0.60):
        self.period = max(1.0, float(speed))
        self.sat, self.light = sat, light

    def colors(self, t: float) -> list[str]:
        base = (t / self.period) * 360.0 * -1
        return [hexc_unique(base, self.sat, self.light, z)
                for z in range(NUM_ZONES)]


class RainbowBreathPulse:
    """Legacy: breathing pulse with slowly rotating hue (fixed base color)."""

    name = "spulse"

    def __init__(self, speed: float = 6.0, hue0: float = 210.0):
        self.period = max(1.0, float(speed))
        self.hue0 = hue0

    def colors(self, t: float) -> list[str]:
        phase = (t / self.period) % 1.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
        glow = 0.45 + 0.55 * glow
        hue0 = self.hue0 + (t / (self.period * 8)) * 360.0
        return [hexc_unique(hue0, 1.0, 0.60 * glow, z)
                for z in range(NUM_ZONES)]


class RainbowBreath:
    """Rainbow Breathing — brightness breathes while the hue rotates; keeps
    a visible floor so it never reads as dark/locked."""

    name = "rbreath"

    def __init__(self, speed: float = 6.0, light: float = 0.62):
        self.period = max(1.0, float(speed))
        self.light = light

    def colors(self, t: float) -> list[str]:
        phase = (t / self.period) % 1.0
        glow = 0.5 - 0.5 * math.cos(2 * math.pi * phase)
        glow = 0.45 + 0.55 * glow
        hue = (t / (self.period * 2.5)) * 360.0
        return [hexc_unique(hue, 1.0, self.light * glow, z)
                for z in range(NUM_ZONES)]


class Fire:
    """Warm flame flicker (OpenRGB-style): every zone is a flame with its
    own phase; hot moments shift yellow, calm moments go deep red."""

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
            out.append(hexc_unique(hue, 1.0, 0.42 + 0.42 * w, z))
        return out


class Rain:
    """Digital rain (OpenRGB-style): random zones light up in a random hue
    and fade away."""

    name = "rain"

    def __init__(self, speed: float = 6.0):
        self.rate = max(0.3, float(speed) / 6.0)
        rnd = random.Random()
        self.val = [1.0 if rnd.random() < 0.30 else (0.4 if rnd.random() < 0.2
                                                     else 0.0)
                    for _ in range(NUM_ZONES)]
        self.hue = [rnd.randrange(360) for _ in range(NUM_ZONES)]
        self.prev_t = 0.0

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

    def _pix(self, z: int) -> str:
        if self.val[z] <= 0.0:
            return hexc_unique(0.0, 0.0, 0.012, z)
        return hexc_unique(self.hue[z], 1.0,
                           0.22 + 0.68 * self.val[z], z)


class Radar:
    """Radar sweep — a beam with a fading trail crossing left↔right."""

    name = "radar"

    def __init__(self, direction: int = 1, speed: float = 6.0, hue: float = 140.0):
        self.dir = 1 if direction == 1 else -1
        self.period = max(1.5, float(speed) * 1.2)
        self.hue = hue

    def colors(self, t: float) -> list[str]:
        x = ((t / self.period) % 1.0) * (N_COLS + 2.0)
        head = x if self.dir == 1 else (N_COLS + 2.0 - x)
        out = []
        for z in range(NUM_ZONES):
            col = z % N_COLS
            d = head - col
            val = 1.0 - d / 2.6 if 0.0 <= d <= 2.6 else 0.0
            if val <= 0.01:
                out.append(hexc_unique(0.0, 0.0, 0.012, z))
            else:
                out.append(hexc_unique(self.hue + 25.0 * val, 1.0,
                                       0.18 + 0.62 * val, z))
        return out


class Lightning:
    """Storm — mostly darkness; short warm-white flashes strike random
    zones and decay quickly."""

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
                out.append(hexc_unique(0.0, 0.0, 0.012, z))
            else:
                out.append(hexc_unique(self.FLASH_HUE, self.FLASH_SAT,
                                       0.45 + 0.45 * val, z))
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
    frame_min = max(0.08, 1.0 / max(1, fps))
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
