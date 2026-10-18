"""Physical key layout of the Crosshair 16 HX (MS-15P2) 24-zone deck.

Geometry is proportional — 1U = one standard key pitch, origin at the
top-left of Esc — and matches the official "Strike with precision" deck art:
half-height function row (Esc, F1–F12, PRT SCR, INS, DEL, ✻, ⁄, ⏻), full
numpad with nav legends (HOME/↑/PGUP, END/↓/PGDN, INS/DEL/ENTER), backslash
after } plus the bottom-row backslash, plain wide ENTER on the home row, the
single Fn bottom-right, the arrows cluster, and the wide gradient spacebar.

Key labels follow the mark-up the GUI already used: "main^tiny" renders a
tiny legend above the main glyph (shift symbols), "main#tiny" a tiny legend
below (numpad nav keys), and "\n" splits the label into two lines (NUM LOCK).

The 24 lighting zones are row-major — 4 zones per row × 6 rows (verified on
this hardware with the zone sweep) — so every key cluster carries its zone
number exactly as the deck art groups it: a zone is a quarter of a deck row,
with the numpad folding into those same quarters per its own row heights.
"""

from __future__ import annotations

from dataclasses import dataclass

NUM_ZONES = 24


@dataclass(frozen=True)
class Key:
    label: str
    x: float
    y: float
    w: float
    h: float
    zone: int
    # where the label is centred; defaults to the box centre
    label_at: tuple[float, float] | None = None

    @property
    def cx(self) -> float:
        return self.x + self.w / 2

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


# shift legends above the number-row glyphs (deck art)
_NUM_LEGENDS = ["^!", "^@", "^#", "^$", "^%", "^^", "^&", "^*",
                "^(", "^)", "^_", "^+"]


def _row(labels, zone, x0, y, w, h, pitch):
    return [Key(lb, round(x0 + i * pitch, 3), y, w, h, zone)
            for i, lb in enumerate(labels)]


def _build() -> list[Key]:
    keys: list[Key] = []

    # --- function row (half height) --------------------------------------------
    keys.append(Key("ESC", 0.0, 0.0, 0.81, 0.50, 0))
    fn = ["F1", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9", "F10", "F11", "F12"]
    for i, lb in enumerate(fn):
        zgroup = 0 if i < 4 else (1 if i < 8 else 2)
        keys.append(Key(lb, round(0.90 + i * 0.9033, 3), 0.0, 0.79, 0.50, zgroup))
    keys += _row(["PRT SCR", "INS", "DEL"], 3, 11.74, 0.0, 0.79, 0.50, 0.9)
    keys += _row(["✻", "/", "⏻"], 3, 14.45, 0.0, 0.67, 0.50, 0.80)

    # --- number row ---------------------------------------------------------------
    ky, kh = 0.60, 0.91
    keys.append(Key("`^~", 0.0, ky, 0.62, kh, 4))
    for i, main in enumerate(list("1234567890-=")):
        zgroup = 4 if i < 4 else (5 if i < 8 else 6)
        keys.append(Key(main + _NUM_LEGENDS[i],
                        round(0.73 + i * 0.9985, 3), ky, 0.88, kh, zgroup))
    keys.append(Key("←^BACKSPACE", 12.70, ky, 1.64, kh, 6))
    keys += _row(["+", "−", "NUM\nLOCK"], 7, 14.45, ky, 0.68, 0.74, 0.80)

    # --- QWERTY row ---------------------------------------------------------------
    ky = 1.62
    keys.append(Key("TAB^⇥", 0.0, ky, 1.12, kh, 8))
    for i, main in enumerate(list("QWERTYUIOP[]")):
        zgroup = 8 if i < 5 else (9 if i < 9 else 10)
        keys.append(Key(main, round(1.22 + i * 1.0, 3), ky, 0.88, kh, zgroup))
    keys.append(Key("\\^|", 13.22, ky, 0.88, kh, 10))
    # numpad row: 7 8 9 (sits slightly higher than the deck row, per the deck art)
    keys += _row(["7#HOME", "8#↑", "9#PGUP"], 11, 14.45, 1.45, 0.68, 0.74, 0.80)

    # --- home row -----------------------------------------------------------------
    ky = 2.645
    keys.append(Key("CAPS LOCK", 0.0, ky, 1.36, kh, 12))
    for i, main in enumerate(list("ASDFGHJKL;'")):
        zgroup = 12 if i < 4 else (13 if i < 8 else 14)
        lb = {";": ";^:", "'": "'^\""}.get(main, main)
        keys.append(Key(lb, round(1.47 + i * 0.998, 3), ky, 0.88, kh, zgroup))
    keys.append(Key("⏎ ENTER", 12.44, ky, 1.90, kh, 15))
    keys += _row(["4#←", "5", "6#→"], 15, 14.45, 2.30, 0.68, 0.74, 0.80)

    # --- shift row ----------------------------------------------------------------
    ky = 3.655
    keys.append(Key("⇧ SHIFT", 0.0, ky, 1.85, kh, 16))
    for i, main in enumerate(list("ZXCVBNM,./")):
        zgroup = 16 if i < 4 else (17 if i < 8 else 18)
        lb = {",": ",^<", ".": ".^>", "/": "/^?"}.get(main, main)
        keys.append(Key(lb, round(1.96 + i * 0.999, 3), ky, 0.88, kh, zgroup))
    keys.append(Key("⇧ SHIFT", 11.95, ky, 1.39, kh, 18))   # right shift, per deck art
    keys.append(Key("↑", 13.52, ky, 0.74, kh, 19))         # directly above ↓
    keys += _row(["1#END", "2#↓", "3#PGDN"], 19, 14.45, 3.14, 0.68, 0.74, 0.80)
    # numpad bottom band sits between the shift row and the bottom row
    keys.append(Key("0#INS", 14.45, 3.99, 1.48, 0.74, 23))
    keys.append(Key(".#DEL", 16.04, 3.99, 0.68, 0.74, 23))

    # --- bottom row ---------------------------------------------------------------
    ky = 4.67
    keys.append(Key("CTRL", 0.0, ky, 1.85, kh, 20))
    keys.append(Key("⊞", 1.96, ky, 0.88, kh, 20))
    keys.append(Key("ALT", 2.96, ky, 0.88, kh, 20))
    keys.append(Key("", 3.96, ky, 4.88, kh, 21))          # space
    keys.append(Key("ALT GR", 8.96, ky, 0.88, kh, 21))
    keys.append(Key("\\^|", 9.95, ky, 0.88, kh, 21))
    keys.append(Key("CTRL", 10.95, ky, 0.74, kh, 22))
    keys.append(Key("FN", 11.80, ky, 0.75, kh, 22))
    keys.append(Key("←", 12.65, ky, 0.75, kh, 22))
    keys.append(Key("↓", 13.52, ky, 0.74, kh, 23))
    keys.append(Key("→", 14.38, ky, 0.74, kh, 23))
    keys.append(Key("⏎ ENTER", 15.25, 4.84, 1.47, 0.74, 23))
    return keys


KEYS: list[Key] = _build()

WIDTH = max(k.x + k.w for k in KEYS)    # ~16.72 U
HEIGHT = max(k.y + k.h for k in KEYS)   # ~5.58 U

# WASD hero accents (deck art): W blue, A purple, S red, D orange
WASD_HUE = {"W": "#6f6fff", "A": "#a266ff", "S": "#ff4d5e", "D": "#ff6a3c"}


def keys_in_zone(z: int) -> list[Key]:
    return [k for k in KEYS if k.zone == z]


def zone_of_key(k: Key) -> int:
    return k.zone
