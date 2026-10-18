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

    effect: str = "steady"
    zone_colors: dict[str, str] = field(default_factory=dict)  # zone index (str) -> hex
    color: str = "#FF7F00"  # main accent color (legacy alias of colors[0])
    colors: list[str] = field(default_factory=list)  # MSI colour list
    color_mode: str = "customized"  # MSI radio: rainbow | customized
    speed: float = 6.0  # seconds (extras + rainbow conveyor)
    speed_index: int = 1  # MSI speed radio: 0 slow, 1 medium, 2 fast
    direction: int = 1  # extras 0/1; wave 2/3; wheel 4/5 (MSI codes)
    level: int = 50  # legacy byte-7 value (kept for state compatibility)
    band_colors: list[str] = field(default_factory=list)  # rainbow band stream
    zone_styles: dict[str, dict] = field(default_factory=dict)  # Customize mode

    # MSI Center effect set for this keyboard (firmware-driven).
    MSI_EFFECTS = ("steady", "breath", "cycle", "wave", "dance", "wheel",
                   "bouncy")
    # Software-rendered extras (animate.py engines).
    SOFTWARE = ("scycle", "spulse", "rbreath", "rain", "fire", "radar",
                "lightning")
    ALL_EFFECTS = ("off",) + MSI_EFFECTS + ("custom", "rainbow", "gaming") \
        + SOFTWARE

    def validate(self) -> None:
        if self.effect not in self.ALL_EFFECTS:
            raise ValueError(f"bad effect: {self.effect}")
        if self.color_mode not in ("rainbow", "customized"):
            raise ValueError(f"bad color mode: {self.color_mode}")
        self.speed = min(12.0, max(3.0, float(self.speed)))
        self.speed_index = min(2, max(0, int(self.speed_index)))
        self.level = min(100, max(0, int(self.level)))
        effects.hex_to_rgb(self.color)
        if len(self.colors) > 5:
            raise ValueError("at most 5 colours per effect")
        for c in self.colors:
            effects.hex_to_rgb(c)
        if self.effect == "wave":
            if self.direction not in (effects.DIR_LEFT, effects.DIR_RIGHT):
                self.direction = effects.DIR_RIGHT
        elif self.effect == "wheel":
            if self.direction not in (effects.DIR_CW, effects.DIR_CCW):
                self.direction = effects.DIR_CW
        elif self.effect in ("rainbow", "radar"):
            if self.direction not in (0, 1):
                raise ValueError("direction must be 0 or 1")
        if self.effect == "rainbow":
            if len(self.band_colors) > effects.BAND_COUNT:
                raise ValueError("rainbow keeps at most "
                                 f"{effects.BAND_COUNT} band colours")
            for c in self.band_colors:
                effects.hex_to_rgb(c)
        if self.effect == "custom":
            for zs, st in self.zone_styles.items():
                z = int(zs)
                if not (0 <= z < 24):
                    raise ValueError(f"zone {z} out of range")
                if st.get("effect", "steady") not in (
                        "off", "steady", "breath", "cycle", "wave", "dance"):
                    raise ValueError(f"bad zone effect: {st.get('effect')}")
                for c in st.get("colors", []):
                    effects.hex_to_rgb(c)
                idx = int(st.get("speed_index", 1))
                if not (0 <= idx <= 2):
                    raise ValueError("zone speed index out of range")

    def effective_colors(self) -> list[str]:
        """MSI colour list, falling back to the legacy single accent colour."""
        colors = [c.lstrip("#").upper() for c in self.colors if c]
        if not colors:
            base = self.color.lstrip("#").upper()
            defaults = effects.DEFAULT_COLORS.get(self.effect)
            colors = list(defaults) if defaults else [base]
        return colors

    # ---- apply ------------------------------------------------------------------

    def apply_to(self, kb: MysticKeyboard, zone_target: list[int] | None = None) -> None:
        self.validate()
        speed = int(round(self.speed * 100))
        zones = zone_target  # None = all
        eff = self.effect

        # software engines (animation type written per frame over HID).
        if eff in self.SOFTWARE:
            from .animate import is_software
            from .daemon import start_detached, stop
            assert is_software(eff)
            stop()
            start_detached(eff, self.speed, self.direction)
            return

        if eff == "rainbow":
            # Firmware rainbow conveyor: one burst of per-zone colour cycles
            # phase-offset by band slot; the MCU animates it itself, nothing
            # on the host. Re-apply keeps the saved band colours.
            if not (2 <= len(self.band_colors) <= effects.BAND_COUNT):
                self.band_colors = effects.random_band_colors()
            kb.rainbow(self.band_colors, speed=int(round(self.speed * 100)),
                       direction=self.direction)
            return

        if eff == "gaming":
            colors = {z: GAMING_BASE for z in range(24)}
            for z in GAMING_RED_ZONES:
                colors[z] = GAMING_RED
            kb.steady_zones(colors)
            return

        kb.direction = 1 if self.direction not in (0, 1) else self.direction
        kb.level = self.level

        if eff == "off":
            kb.off(zones)
            return

        if eff in self.MSI_EFFECTS:
            self._apply_msi(kb, zones)
            return

        if eff == "custom":
            self._apply_custom(kb)
            return

    # ---- MSI Center firmware effects (same bytes as MSI Center) ------------------

    def _apply_msi(self, kb: MysticKeyboard, zones) -> None:
        eff = self.effect
        colors = self.effective_colors()

        if eff == "dance":
            kb.dance(speed_s=effects.SPEED_TABLE["dance"][self.speed_index])
            return

        speed = int(round(effects.SPEED_TABLE[eff][self.speed_index] * 100))
        mode = self.color_mode

        # Steady/Breath "Rainbow": MSI lights 9 fixed hue groups, one packet
        # per zone (its own group's two-ball solid list).
        if eff in ("steady", "breath") and mode == "rainbow":
            code = effects.EFFECT_STEADY if eff == "steady" \
                else effects.EFFECT_BREATH
            for group, group_zones in enumerate(effects.ZONE_RAINBOW_GROUPS):
                balls = effects.balls_zone_rainbow(group)
                for z in group_zones:
                    kb.msi_effect(code, balls, zones=[z], speed=speed)
            return

        # Legacy extension: steady with per-zone colours paints the picked
        # zones (the "Apply to selected zones" flow in this app).
        if eff == "steady" and mode == "customized" and self.zone_colors:
            zones_set = set(range(24)) if zones is None else set(zones)
            colmap = {int(z): c for z, c in self.zone_colors.items()
                      if int(z) in zones_set}
            for z in zones_set:
                colmap.setdefault(z, colors[0])
            kb.steady_zones(colmap)
            return

        balls = effects.balls_for_effect(eff, mode, colors)

        if eff == "wave":
            d = self.direction if self.direction in (effects.DIR_LEFT,
                                                     effects.DIR_RIGHT) \
                else effects.DIR_RIGHT
            kb.msi_effect(effects.EFFECT_WAVE, balls, zones=zones, speed=speed,
                          center=(0, 50), wavelength_tenths=effects.WAVELENGTH["wave"],
                          wave_mode=1 if mode == "rainbow" else 0,
                          direction=0 if d == effects.DIR_LEFT else 1)
            return
        if eff == "wheel":
            d = self.direction if self.direction in (effects.DIR_CW,
                                                     effects.DIR_CCW) \
                else effects.DIR_CW
            wl = effects.WAVELENGTH["wheel"] if mode == "rainbow" \
                else effects.WAVELENGTH["wheel_custom"]
            kb.msi_effect(effects.EFFECT_WHEEL, balls, zones=zones, speed=speed,
                          wavelength_tenths=wl,
                          wave_mode=1 if mode == "rainbow" else 0,
                          direction=0 if d == effects.DIR_CW else 1)
            return
        if eff == "bouncy":
            # MSI fixes the period at 1.0 s and the wavelength at 0.2 (byte 2)
            # for both rainbow and customized modes.
            kb.msi_effect(effects.EFFECT_BOUNCY, balls, zones=zones, speed=100,
                          wavelength_tenths=effects.WAVELENGTH["bouncy"],
                          wave_mode=1 if mode == "rainbow" else 0,
                          direction=1)
            return

        code = {"steady": effects.EFFECT_STEADY, "breath": effects.EFFECT_BREATH,
                "cycle": effects.EFFECT_CYCLE}[eff]
        kb.msi_effect(code, balls, zones=zones, speed=speed)

    # ---- Customize mode: per-zone effect styles ----------------------------------

    def _apply_custom(self, kb: MysticKeyboard) -> None:
        """Apply each zone's own style — MSI's Customize mode."""
        for zs in sorted(self.zone_styles, key=int):
            z = int(zs)
            st = self.zone_styles[zs]
            e = st.get("effect", "steady")
            colors = [c.lstrip("#").upper() for c in st.get("colors") or []]
            if not colors:
                colors = self.effective_colors()
            idx = min(2, max(0, int(st.get("speed_index", 1))))
            d = int(st.get("direction", effects.DIR_RIGHT))
            if e == "off":
                kb.msi_effect(effects.EFFECT_OFF, (), zones=[z], speed=100)
            elif e == "steady":
                kb.msi_effect(effects.EFFECT_STEADY,
                              effects.balls_default(colors), zones=[z],
                              speed=600)
            elif e == "breath":
                kb.msi_effect(effects.EFFECT_BREATH,
                              effects.balls_default(colors), zones=[z],
                              speed=int(round(effects.SPEED_TABLE["breath"][idx] * 100)))
            elif e == "cycle":
                kb.msi_effect(effects.EFFECT_CYCLE,
                              effects.balls_default(colors), zones=[z],
                              speed=int(round(effects.SPEED_TABLE["cycle"][idx] * 100)))
            elif e == "wave":
                kb.msi_effect(effects.EFFECT_WAVE,
                              effects.balls_default(colors), zones=[z],
                              speed=int(round(effects.SPEED_TABLE["wave"][idx] * 100)),
                              center=(0, 50),
                              wavelength_tenths=effects.WAVELENGTH["wave"],
                              wave_mode=0,
                              direction=0 if d == effects.DIR_LEFT else 1)
            elif e == "dance":
                kb.msi_effect(effects.EFFECT_DANCE, (), zones=[z],
                              speed=int(round(effects.SPEED_TABLE["dance"][idx] * 100)))


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
# profiles load as rainbow.  ("wave" is NOT migrated: it is now the real
# MSI Center Wave effect, type 4 on this keyboard.)
_LEGACY_EFFECT = {"swave": "rainbow", "rainbow-wave": "rainbow"}


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
