"""Profile persistence: ~/.config/mystic-control/{profiles.json,state.json}."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from . import effects
from .keyboard import MysticKeyboard

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")),
    "mystic-control",
)
STATE_PATH = os.path.join(CONFIG_DIR, "state.json")
PROFILES_PATH = os.path.join(CONFIG_DIR, "profiles.json")

# "Gaming" highlight: WASD + arrow-key zones flash red over a dim board
# (zone index = row*4 + column; W lives in zone 8, A/S/D in 12, arrows 22/23)
GAMING_RED_ZONES = (8, 12, 22, 23)
GAMING_RED = "#FF2030"
GAMING_BASE = "#26262B"


@dataclass
class LightingState:
    """Everything needed to reproduce the current lighting."""

    effect: str = "steady"  # off | steady | breath | cycle | rainbow
    zone_colors: dict[str, str] = field(default_factory=dict)  # zone index (str) -> hex
    color: str = "#FF7F00"  # main accent color
    speed: float = 6.0  # seconds (effect-specific period)per animation cycle (3..12)
    direction: int = 1  # 1 = left->right, 0 = right->left
    level: int = 50  # byte7, MSI Center default 50
    band_colors: list[str] = field(default_factory=list)  # rainbow band stream

    def validate(self) -> None:
        if self.effect not in {"off", "steady", "breath", "cycle", "rainbow",
                               "scycle", "spulse", "rbreath", "rain", "fire",
                               "radar", "lightning", "gaming"}:
            raise ValueError(f"bad effect: {self.effect}")
        if self.direction not in (0, 1):
            raise ValueError("direction must be 0 or 1")
        self.speed = min(12.0, max(3.0, float(self.speed)))
        self.level = min(100, max(0, int(self.level)))
        effects.hex_to_rgb(self.color)
        if self.effect == "rainbow":
            if len(self.band_colors) > effects.BAND_COUNT:
                raise ValueError("rainbow keeps at most "
                                 f"{effects.BAND_COUNT} band colours")
            for c in self.band_colors:
                effects.hex_to_rgb(c)

    # ---- apply ------------------------------------------------------------------

    def apply_to(self, kb: MysticKeyboard, zone_target: list[int] | None = None) -> None:
        self.validate()
        speed = int(round(self.speed * 100))
        zones = zone_target  # None = all

        # software engines (animation type written per frame over HID).
        if self.effect in ("scycle", "spulse", "rbreath", "rain",
                           "fire", "radar", "lightning"):
            from .animate import is_software
            from .daemon import start_detached, stop
            assert is_software(self.effect)
            stop()
            start_detached(self.effect, self.speed, self.direction)
            return

        if self.effect == "rainbow":
            # Firmware rainbow conveyor: one burst of per-zone colour cycles
            # phase-offset by band slot; the MCU animates it itself, nothing
            # on the host. Re-apply keeps the saved band colours.
            if not (2 <= len(self.band_colors) <= effects.BAND_COUNT):
                self.band_colors = effects.random_band_colors()
            kb.rainbow(self.band_colors, speed=int(round(self.speed * 100)),
                       direction=self.direction)
            return

        if self.effect == "gaming":
            colors = {z: GAMING_BASE for z in range(24)}
            for z in GAMING_RED_ZONES:
                colors[z] = GAMING_RED
            kb.steady_zones(colors)
            return

        kb.direction = self.direction
        kb.level = self.level
        if self.effect == "off":
            kb.off(zones)
        elif self.effect == "steady":
            if zone_target is None:
                zones_set = set(range(24))
            else:
                zones_set = set(zone_target)
            colors = {int(z): c for z, c in self.zone_colors.items() if int(z) in zones_set}
            # zones without explicit color → keep main accent
            for z in zones_set:
                colors.setdefault(z, self.color)
            kb.steady_zones(colors)
        elif self.effect == "breath":
            kb.breath(self.color, zones=zones, speed=speed)
        elif self.effect == "cycle":
            kb.cycle(zones=zones, speed=speed)


# --- JSON storage -----------------------------------------------------------------


def _read_json(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def _write_json(path: str, data: Any) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# Effects that existed before the rainbow conveyor; old saved states and
# profiles load as rainbow.
_LEGACY_EFFECT = {"wave": "rainbow", "swave": "rainbow",
                  "rainbow-wave": "rainbow"}


def load_state() -> LightingState:
    data = _read_json(STATE_PATH, None)
    try:
        st = LightingState(**data)
        st.effect = _LEGACY_EFFECT.get(st.effect, st.effect)
        st.validate()
        return st
    except (TypeError, ValueError):
        return LightingState()


def save_state(state: LightingState) -> None:
    _write_json(STATE_PATH, asdict(state))


def load_profiles() -> dict[str, dict]:
    data = _read_json(PROFILES_PATH, {})
    return data if isinstance(data, dict) else {}


def save_profiles(profiles: dict[str, dict]) -> None:
    _write_json(PROFILES_PATH, profiles)


def save_profile(name: str, state: LightingState) -> None:
    profiles = load_profiles()
    profiles[name] = asdict(state)
    save_profiles(profiles)


def delete_profile(name: str) -> None:
    profiles = load_profiles()
    profiles.pop(name, None)
    save_profiles(profiles)


def get_profile(name: str) -> LightingState | None:
    profiles = load_profiles()
    if name not in profiles:
        return None
    try:
        st = LightingState(**profiles[name])
        st.effect = _LEGACY_EFFECT.get(st.effect, st.effect)
        st.validate()
        return st
    except (TypeError, ValueError):
        return None
