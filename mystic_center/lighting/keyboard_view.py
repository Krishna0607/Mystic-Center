"""Proportional drawing of the Crosshair 16 HX deck, coloured per lighting zone.

`draw_keyboard()` is plain Cairo (no GTK) so it can be unit-tested and render
to PNG headlessly; `KeyboardView` wraps it in a GTK4 DrawingArea for the GUI.

Key labels carry the deck-art mark-up: "main^tiny" draws the tiny legend
above the glyph, "main#tiny" below it, and "\\n" splits the label into lines.
"""

from __future__ import annotations

import math

from . import layout
from .layout import HEIGHT, KEYS, NUM_ZONES, WASD_HUE, WIDTH, Key

PAD = 0.35           # padding around the keys, in key units (the bezel)
ZONE_STRIP_H = 0.34  # height of the zone bar under the board (units)
ZONE_STRIP_GAP = 0.28


def _rgb(hexstr: str) -> tuple[float, float, float]:
    s = hexstr.strip().lstrip("#")
    return int(s[0:2], 16) / 255, int(s[2:4], 16) / 255, int(s[4:6], 16) / 255


def _round_rect(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def geometry(width: float, height: float) -> tuple[float, float, float]:
    """-> (scale px/unit, offset_x, offset_y) fitting board + zone bar."""
    total_w = WIDTH + 2 * PAD
    total_h = HEIGHT + 2 * PAD + ZONE_STRIP_GAP + ZONE_STRIP_H
    scale = min(width / total_w, height / total_h)
    ox = (width - total_w * scale) / 2 + PAD * scale
    oy = (height - total_h * scale) / 2 + PAD * scale
    return scale, ox, oy


def _nearest_key(x: float, y: float) -> Key:
    return min(KEYS, key=lambda k: (k.cx - x) ** 2 + (k.cy - y) ** 2)


def key_at(px: float, py: float, width: float, height: float) -> int | None:
    """Zone under a pixel position: keys, the zone bar, or board padding."""
    scale, ox, oy = geometry(width, height)
    x = (px - ox) / scale
    y = (py - oy) / scale
    strip_y = HEIGHT + ZONE_STRIP_GAP
    if strip_y - 0.05 <= y <= strip_y + ZONE_STRIP_H + 0.1 and 0 <= x <= WIDTH:
        # zone bar: one cell per zone, left → right in hardware order 0..23
        return max(0, min(NUM_ZONES - 1, int(x / WIDTH * NUM_ZONES)))
    for k in KEYS:
        if k.x <= x <= k.x + k.w and k.y <= y <= k.y + k.h:
            return k.zone
    # board padding and intra-row gaps map to the nearest key's zone
    if -0.2 <= x <= WIDTH + 0.2 and -0.2 <= y <= HEIGHT + 0.2:
        return _nearest_key(min(max(x, 0), WIDTH),
                            min(max(y, 0), HEIGHT)).zone
    return None


def _split_label(text: str) -> tuple[list[str], str | None, bool]:
    """-> (lines, tiny_legend, tiny_below). "^" = tiny above, "#" = below."""
    tiny, below = None, True
    main = text
    if "^" in text:
        main, tiny = text.split("^", 1)
        below = False
    elif "#" in text:
        main, tiny = text.split("#", 1)
    return main.split("\n"), tiny, below


def _draw_label(cr, k: Key, r: float, g: float, b: float) -> None:
    text = k.label
    if not text.strip():
        return
    if text == "⏻":  # power glyph — draw it (missing from most fonts)
        cr.set_source_rgb(min(1, r * 1.15), min(1, g * 1.15), min(1, b * 1.15))
        cr.set_line_width(0.05)
        cr.set_line_cap(1)
        rad = 0.13
        cr.new_sub_path()
        cr.arc(k.cx, k.cy + 0.02, rad, -math.pi / 2 + 0.6, -math.pi / 2 - 0.6 + 2 * math.pi)
        cr.stroke()
        cr.new_path()
        cr.move_to(k.cx, k.cy - rad - 0.05)
        cr.line_to(k.cx, k.cy + 0.02)
        cr.stroke()
        return
    lines, tiny, below = _split_label(text)
    n = len(text)
    main_size = 0.30 if n <= 3 else (0.21 if n <= 6 else 0.17)
    if k.h < 0.6:                      # half-height function row
        main_size = 0.28 if n <= 3 else (0.19 if n <= 6 else 0.15)
    if k.w < 0.7 and n > 2:            # narrow numpad cells
        main_size = min(main_size, 0.15)
    tiny_size = 0.13
    # cairo has no glyph fallback: non-Latin glyphs (⊞ ✻ ⇧ ⏎ ⇥ arrows) only
    # exist in DejaVu — switch the whole label run to it when one appears
    if any(ord(c) > 127 for c in text):
        cr.select_font_face("DejaVu Sans Condensed", 0, 1)
    else:
        cr.select_font_face("Open Sans Condensed", 0, 1)
    cr.set_font_size(main_size)
    cx, cy = k.cx, k.cy
    if tiny is None:
        if len(lines) == 1:
            ext = cr.text_extents(lines[0])
            cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                       cy - ext.height / 2 - ext.y_bearing)
            cr.show_text(lines[0])
        else:  # two stacked lines (NUM LOCK)
            ext0 = cr.text_extents(lines[0])
            ext1 = cr.text_extents(lines[1])
            dy = ext0.height / 2 + ext1.height / 2 + 0.03
            cr.move_to(cx - ext0.width / 2 - ext0.x_bearing,
                       cy - dy / 2 - ext0.y_bearing)
            cr.show_text(lines[0])
            cr.move_to(cx - ext1.width / 2 - ext1.x_bearing,
                       cy + dy / 2 - ext1.y_bearing)
            cr.show_text(lines[1])
        return
    # main glyph shifted to make room for the tiny legend
    main_dy = -(tiny_size / 2 + main_size / 2) * 0.6 if below else \
              +(tiny_size / 2 + main_size / 2) * 0.6
    for j, line in enumerate(lines):
        ext = cr.text_extents(line)
        dy = (j - (len(lines) - 1) / 2) * (main_size + 0.04)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing,
                   cy + main_dy + dy - ext.height / 2 - ext.y_bearing)
        cr.show_text(line)
    cr.set_font_size(tiny_size)
    ext = cr.text_extents(tiny)
    tiny_dy = +(main_size / 2 + tiny_size / 2) * 0.9 if below else \
              -(main_size / 2 + tiny_size / 2) * 0.9
    cr.move_to(cx - ext.width / 2 - ext.x_bearing, cy + tiny_dy - ext.y_bearing)
    cr.show_text(tiny)


def draw_keyboard(cr, width: float, height: float, colors: list[str],
                  selected: set[int] | None = None, *, background=True) -> None:
    """Paint the deck. colors[z] is the hex colour of lighting zone z."""
    selected = selected or set()
    scale, ox, oy = geometry(width, height)
    if background:
        cr.set_source_rgb(0.035, 0.035, 0.05)
        cr.paint()
    cr.save()
    cr.translate(ox, oy)
    cr.scale(scale, scale)

    cr.set_source_rgb(0.07, 0.07, 0.09)
    _round_rect(cr, -PAD * 0.6, -PAD * 0.6, WIDTH + PAD * 1.2,
                HEIGHT + PAD * 1.2, 0.22)
    cr.fill()

    for k in KEYS:
        z = k.zone
        r, g, b = _rgb(colors[z])
        radius = 0.04 if k.h > 0.6 else 0.03   # squarish keycaps, hint of a bevel

        # key face: near-black with a hint of the zone colour
        _round_rect(cr, k.x, k.y, k.w, k.h, radius)
        cr.set_source_rgb(0.04 + r * 0.10, 0.04 + g * 0.10, 0.05 + b * 0.10)
        cr.fill_preserve()
        # soft glow
        cr.set_line_join(1)
        cr.set_line_width(0.13)
        cr.set_source_rgba(r, g, b, 0.16)
        cr.stroke_preserve()
        # hero ring for WASD (deck art) else crisp zone-coloured outline
        if k.label in WASD_HUE and len(k.label) == 1:
            hr, hg, hb = _rgb(WASD_HUE[k.label])
            cr.set_line_width(0.075)
            cr.set_source_rgb(hr, hg, hb)
            cr.stroke_preserve()
            cr.set_source_rgba(hr, hg, hb, 0.28)
            cr.set_line_width(0.19)
            cr.stroke_preserve()
        cr.set_line_width(0.045)
        cr.set_source_rgb(r, g, b)
        cr.stroke()

        if z in selected:
            _round_rect(cr, k.x, k.y, k.w, k.h, radius)
            cr.set_source_rgba(1, 1, 1, 0.20)
            cr.fill_preserve()
            cr.set_line_width(0.05)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.stroke()

        _draw_label(cr, k, r, g, b)

    # zone bar: one cell per lighting zone, hardware order 0..23 left → right
    sy = HEIGHT + ZONE_STRIP_GAP
    cw = WIDTH / NUM_ZONES
    for z in range(NUM_ZONES):
        r, g, b = _rgb(colors[z])
        _round_rect(cr, z * cw + 0.03, sy, cw - 0.06, ZONE_STRIP_H, 0.04)
        cr.set_source_rgb(r, g, b)
        cr.fill_preserve()
        if z in selected:
            cr.set_line_width(0.06)
            cr.set_source_rgb(1, 1, 1)
            cr.stroke()
        else:
            cr.new_path()
    cr.restore()


# --- GTK widget -------------------------------------------------------------------

try:  # GTK is optional so the drawing code stays testable without it
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
except Exception:  # pragma: no cover
    Gtk = None  # type: ignore

if Gtk is not None:

    class KeyboardView(Gtk.DrawingArea):
        """Clickable deck preview. Clicking a key toggles its whole zone."""

        def __init__(self, on_zone_clicked=None):
            super().__init__()
            self.colors = ["16161C"] * NUM_ZONES
            self.selected: set[int] = set()
            self._cb = on_zone_clicked
            total_w = WIDTH + 2 * PAD
            total_h = HEIGHT + 2 * PAD + ZONE_STRIP_GAP + ZONE_STRIP_H
            self.set_content_width(900)
            self.set_content_height(int(900 * total_h / total_w))
            self.set_hexpand(True)
            self.set_draw_func(self._draw)
            click = Gtk.GestureClick()
            click.connect("pressed", self._pressed)
            self.add_controller(click)

        def _draw(self, _area, cr, w, h):
            draw_keyboard(cr, w, h, self.colors, self.selected,
                          background=False)

        def _pressed(self, _g, _n, x, y):
            z = key_at(x, y, self.get_width(), self.get_height())
            if z is not None and self._cb:
                self._cb(z)

        def set_colors(self, colors):
            self.colors = [c.lstrip("#") for c in colors]
            self.queue_draw()

        def set_zone_selected(self, zone: int, selected: bool) -> None:
            if selected:
                self.selected.add(zone)
            else:
                self.selected.discard(zone)
            self.queue_draw()

        def set_selected(self, selected):
            self.selected = set(selected)
            self.queue_draw()
