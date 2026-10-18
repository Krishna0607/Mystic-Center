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


@dataclass
class LightingState:
    """Everything needed to reproduce the current lighting."""

    effect: str = "steady"  # off | steady | breath | cycle | wave
    zone_colors: dict[str, str] = field(default_factory=dict)  # zone index (str) -> hex
    color: str = "#FF7F00"  # main accent color
    speed: float = 6.0  # seconds per animation cycle (3..12)
    direction: int = 1  # 1 = left->right, 0 = right->left
    level: int = 50  # byte7, MSI Center default 50

    def validate(self) -> None:
        if self.effect not in {"off", "steady", "breath", "cycle", "wave",
                               "swave", "scycle", "spulse"}:
            raise ValueError(f"bad effect: {self.effect}")
        if self.direction not in (0, 1):
            raise ValueError("direction must be 0 or 1")
        self.speed = min(12.0, max(3.0, float(self.speed)))
        self.level = min(100, max(0, int(self.level)))
        effects.hex_to_rgb(self.color)

    # ---- apply ------------------------------------------------------------------

    def apply_to(self, kb: MysticKeyboard, zone_target: list[int] | None = None) -> None:
        self.validate()
        kb.direction = self.direction
        kb.level = self.level
        speed = int(round(self.speed * 100))
        zones = zone_target  # None = all

        if self.effect in ("swave", "scycle", "spulse"):
            from .daemon import start_detached, stop
            stop()
            start_detached(self.effect, self.speed, self.direction)
            return

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
        elif self.effect == "wave":
            kb.wave(zones=zones, speed=speed, direction=self.direction)


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


def load_state() -> LightingState:
    data = _read_json(STATE_PATH, None)
    try:
        st = LightingState(**data)
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
        st.validate()
        return st
    except (TypeError, ValueError):
        return None


# --- built-in presets (merged with user profiles) ----------------------------------


def builtins() -> dict[str, dict]:
    def mk(effect, color, speed=6.0, direction=1, zone_colors=None, level=50):
        return {
            "effect": effect,
            "color": color,
            "speed": speed,
            "direction": direction,
            "level": level,
            "zone_colors": zone_colors or {},
        }

    return {
        "Rainbow Cycle (MSI default)": mk("cycle", "#FFFFFF"),
        "Rainbow Wave ←→": mk("wave", "#FFFFFF", direction=1),
        "Rainbow Wave →←": mk("wave", "#FFFFFF", direction=0),
        "True 24-zone Wave ←→": mk("swave", "#FFFFFF", direction=1),
        "True 24-zone Wave →←": mk("swave", "#FFFFFF", direction=0),
        "True 24-zone Cycle": mk("scycle", "#FFFFFF"),
        "Breathing Red": mk("breath", "#FF2020"),
        "Breathing Amber": mk("breath", "#FF7F20"),
        "All Red": mk("steady", "#FF0000"),
        "All Green": mk("steady", "#00FF00"),
        "All Blue": mk("steady", "#0070FF"),
        "All White (low)": mk("steady", "#404040"),
        "Matrix Green": mk("steady", "#00FF55"),
        "Cyberpunk Railgun": mk("steady", "#FF2AA0"),
        "Off": mk("off", "#000000"),
    }
