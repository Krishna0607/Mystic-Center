"""Mystic Center visual language — a premium, control-center style theme.

Everything here is GTK4/libadwaita friendly: no custom widgets beyond thin
composition helpers, and all CSS uses properties GTK actually supports. Class
names are prefixed ``mc-`` so they never collide with the lighting page's own
provider (which loads at application priority).

Two schemes are provided. :func:`install` is idempotent and :func:`set_scheme`
swaps the palette at runtime, so the Settings page can flip dark/light without
restarting.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gtk  # noqa: E402

DARK = {
    "bg": "#0a0c11",
    "bg2": "#0b0e14",
    "card": "#12151d",
    "card2": "#171b24",
    "border": "rgba(255,255,255,0.07)",
    "border_strong": "rgba(255,255,255,0.14)",
    "text": "#e8edf7",
    "text_strong": "#f5f8ff",
    "dim": "#93a0b8",
    "dim2": "#76839c",
    "accent": "#4c8dff",
    "accent2": "#22d3ee",
    "accent_soft": "rgba(76,141,255,0.16)",
    "hero_a": "#16203a",
    "hero_b": "#101725",
    "hero_c": "#0e121a",
    "hero_border": "rgba(110,160,255,0.24)",
    "shadow": "rgba(0,0,0,0.45)",
    "trough": "rgba(255,255,255,0.08)",
    "hover": "rgba(255,255,255,0.05)",
}

LIGHT = {
    "bg": "#eef1f6",
    "bg2": "#e6eaf1",
    "card": "#ffffff",
    "card2": "#f7f9fc",
    "border": "rgba(15,23,42,0.08)",
    "border_strong": "rgba(15,23,42,0.16)",
    "text": "#1b2434",
    "text_strong": "#0b1220",
    "dim": "#5d6b83",
    "dim2": "#7a879c",
    "accent": "#2563eb",
    "accent2": "#0891b2",
    "accent_soft": "rgba(37,99,235,0.10)",
    "hero_a": "#dfe9fb",
    "hero_b": "#eaf1fc",
    "hero_c": "#f6f9ff",
    "hero_border": "rgba(37,99,235,0.20)",
    "shadow": "rgba(15,23,42,0.12)",
    "trough": "rgba(15,23,42,0.08)",
    "hover": "rgba(15,23,42,0.04)",
}

_CSS = r"""
window.mc-window {
    background-color: %BG%;
    color: %TEXT%;
    font-family: "Adwaita Sans", "Cantarell", "Inter", "Segoe UI", sans-serif;
}
window.mc-window .background,
window.mc-window scrolledwindow,
window.mc-window viewport,
window.mc-window preferencespage,
window.mc-window preferencesgroup,
window.mc-window clamp {
    background-color: transparent;
}
window.mc-window scrolledwindow undershoot.top,
window.mc-window scrolledwindow undershoot.bottom { background: none; }

headerbar.mc-header {
    background-color: %BG2%;
    background-image: none;
    border-bottom: 1px solid %BORDER%;
    box-shadow: none;
    min-height: 56px;
    padding: 0 10px;
}
headerbar.mc-header windowtitle { color: %TEXT%; }

/* ---------------------------------------------------------------- sidebar */
.mc-sidebar {
    background-color: %BG2%;
    border-right: 1px solid %BORDER%;
}
.mc-brand-name {
    font-size: 16px;
    font-weight: 800;
    color: %TEXT_STRONG%;
}
.mc-brand-sub {
    font-size: 10.5px;
    font-weight: 600;
    color: %DIM2%;
}
.mc-nav {
    background: transparent;
    background-image: none;
    border: none;
    box-shadow: none;
    border-radius: 12px;
    padding: 9px 12px;
    color: %DIM%;
    font-size: 13.5px;
    font-weight: 650;
    transition: 120ms;
}
.mc-nav:hover {
    background-color: %HOVER%;
    color: %TEXT%;
}
.mc-nav:checked {
    background-color: %ACCENT_SOFT%;
    background-image: linear-gradient(90deg, %ACCENT_SOFT%, transparent);
    color: %TEXT_STRONG%;
    box-shadow: inset 3px 0 0 0 %ACCENT%;
}
.mc-nav:checked image { color: %ACCENT%; }
.mc-nav image { color: %DIM%; }
.mc-nav-section {
    font-size: 10px;
    font-weight: 800;
    color: %DIM2%;
    margin: 12px 10px 2px 12px;
}
.mc-sidebar-footer {
    border-top: 1px solid %BORDER%;
    padding: 12px;
}
.mc-dot-ok { color: #34d399; }
.mc-dot-err { color: #f87171; }

/* ----------------------------------------------------------------- header */
.mc-header-title {
    font-size: 14px;
    font-weight: 750;
    color: %TEXT%;
}
.mc-chip {
    background-color: %HOVER%;
    border: 1px solid %BORDER%;
    border-radius: 999px;
    padding: 4px 11px;
    color: %DIM%;
    font-size: 11.5px;
    font-weight: 650;
}
.mc-chip image { color: %DIM%; }
.mc-chip-value {
    color: %TEXT_STRONG%;
    font-weight: 800;
}
.mc-chip-accent { border-color: %HERO_BORDER%; }
.mc-chip-accent image { color: %ACCENT%; }

/* ------------------------------------------------------------------- page */
.mc-page-head { margin: 4px 0 2px 0; }
.mc-page-title {
    font-size: 27px;
    font-weight: 850;
    color: %TEXT_STRONG%;
}
.mc-page-sub {
    font-size: 13px;
    color: %DIM%;
}
.mc-sub-title {
    font-size: 17px;
    font-weight: 800;
    color: %TEXT_STRONG%;
}

/* ------------------------------------------------------------------ cards */
.mc-card {
    background-color: %CARD%;
    border: 1px solid %BORDER%;
    border-radius: 18px;
    padding: 18px;
}
.mc-card-flat {
    background-color: %CARD2%;
    border: 1px solid %BORDER%;
    border-radius: 16px;
    padding: 14px;
}
.mc-card-title {
    font-size: 15.5px;
    font-weight: 750;
    color: %TEXT_STRONG%;
}
.mc-card-sub {
    font-size: 12px;
    color: %DIM%;
}
.mc-card-icon { color: %ACCENT%; }

.mc-hero {
    background-image:
        linear-gradient(120deg, %ACCENT_SOFT% 0%, rgba(34,211,238,0.06) 42%, transparent 78%),
        linear-gradient(180deg, %HERO_A% 0%, %HERO_B% 58%, %HERO_C% 100%);
    border: 1px solid %HERO_BORDER%;
    border-radius: 22px;
    padding: 24px;
    box-shadow: 0 20px 48px %SHADOW%;
}
.mc-hero-eyebrow {
    font-size: 11px;
    font-weight: 800;
    color: %ACCENT%;
}
.mc-hero-title {
    font-size: 30px;
    font-weight: 850;
    color: %TEXT_STRONG%;
}
.mc-hero-sub {
    font-size: 13px;
    color: %DIM%;
}
.mc-hero-big {
    font-size: 46px;
    font-weight: 850;
    color: %TEXT_STRONG%;
}

/* ------------------------------------------------------------- stat tiles */
.mc-stat {
    background-color: %CARD%;
    border: 1px solid %BORDER%;
    border-radius: 16px;
    padding: 15px 16px;
}
.mc-stat-label {
    font-size: 10.5px;
    font-weight: 800;
    color: %DIM2%;
}
.mc-stat-value {
    font-size: 25px;
    font-weight: 850;
    color: %TEXT_STRONG%;
}
.mc-stat-caption {
    font-size: 11px;
    color: %DIM%;
}
.mc-stat-icon { color: %ACCENT%; }
.mc-tone-ok { color: #34d399; }
.mc-tone-warn { color: #fbbf24; }
.mc-tone-err { color: #f87171; }

levelbar.mc-meter trough {
    background-color: %TROUGH%;
    border: none;
    border-radius: 99px;
    min-height: 6px;
}
levelbar.mc-meter block.filled {
    background-image: linear-gradient(90deg, %ACCENT%, %ACCENT2%);
    border: none;
    border-radius: 99px;
    min-height: 6px;
}
levelbar.mc-meter block.empty {
    background-color: transparent;
    border: none;
}

/* --------------------------------------------------------------- mode card */
.mc-mode {
    background-color: %CARD%;
    border: 1.5px solid %BORDER%;
    border-radius: 18px;
    padding: 18px;
    transition: 140ms;
}
.mc-mode:hover {
    background-color: %CARD2%;
    border-color: %BORDER_STRONG%;
}
.mc-mode:checked {
    border-color: %ACCENT%;
    background-image: linear-gradient(160deg, %ACCENT_SOFT%, rgba(34,211,238,0.05) 60%, transparent);
    box-shadow: 0 0 0 3px %ACCENT_SOFT%;
}
.mc-mode-icon { color: %ACCENT%; }
.mc-mode-name {
    font-size: 16.5px;
    font-weight: 800;
    color: %TEXT_STRONG%;
}
.mc-mode-desc {
    font-size: 11.5px;
    color: %DIM%;
}

/* ----------------------------------------------------------------- badges */
.mc-badge {
    border-radius: 999px;
    padding: 3px 10px;
    font-size: 10px;
    font-weight: 800;
}
.mc-badge-accent { background-color: %ACCENT_SOFT%; color: %ACCENT%; }
.mc-badge-ok { background-color: rgba(52,211,153,0.16); color: #34d399; }
.mc-badge-warn { background-color: rgba(251,191,36,0.16); color: #fbbf24; }
.mc-badge-err { background-color: rgba(248,113,113,0.16); color: #f87171; }
.mc-badge-muted { background-color: %TROUGH%; color: %DIM%; }

/* ---------------------------------------------------------------- banners */
.mc-banner {
    border-radius: 16px;
    padding: 14px 16px;
}
.mc-banner-title {
    font-size: 14px;
    font-weight: 800;
}
.mc-banner-sub {
    font-size: 12px;
}
.mc-banner-ok {
    background-color: rgba(52,211,153,0.10);
    border: 1px solid rgba(52,211,153,0.28);
}
.mc-banner-ok .mc-banner-title { color: #34d399; }
.mc-banner-warn {
    background-color: rgba(251,191,36,0.10);
    border: 1px solid rgba(251,191,36,0.30);
}
.mc-banner-warn .mc-banner-title { color: #fbbf24; }
.mc-banner-err {
    background-color: rgba(248,113,113,0.10);
    border: 1px solid rgba(248,113,113,0.30);
}
.mc-banner-err .mc-banner-title { color: #f87171; }
.mc-banner-accent {
    background-color: %ACCENT_SOFT%;
    border: 1px solid %HERO_BORDER%;
}
.mc-banner-accent .mc-banner-title { color: %ACCENT%; }
.mc-banner-sub { color: %DIM%; }

/* ---------------------------------------------------------------- buttons */
button.mc-primary {
    background-image: linear-gradient(135deg, #3b82f6, #22b8f0);
    background-color: #3b82f6;
    color: #ffffff;
    border: none;
    border-radius: 12px;
    padding: 10px 22px;
    font-weight: 800;
    box-shadow: 0 10px 24px rgba(59,130,246,0.28);
}
button.mc-primary:hover { background-image: linear-gradient(135deg, #4f92ff, #35c7f5); }
button.mc-primary:disabled {
    background-image: none;
    background-color: %TROUGH%;
    color: %DIM2%;
    box-shadow: none;
}
button.mc-ghost {
    background-color: %HOVER%;
    background-image: none;
    border: 1px solid %BORDER_STRONG%;
    border-radius: 12px;
    padding: 10px 18px;
    color: %TEXT%;
    font-weight: 700;
    box-shadow: none;
}
button.mc-ghost:hover { background-color: %ACCENT_SOFT%; }
button.mc-danger {
    background-color: rgba(248,113,113,0.12);
    background-image: none;
    border: 1px solid rgba(248,113,113,0.32);
    border-radius: 12px;
    padding: 10px 18px;
    color: #f87171;
    font-weight: 750;
    box-shadow: none;
}
button.mc-danger:hover { background-color: rgba(248,113,113,0.20); }

.mc-seg button {
    background-image: none;
    background-color: %HOVER%;
    border: 1px solid %BORDER%;
    border-radius: 10px;
    padding: 5px 15px;
    min-height: 24px;
    color: %DIM%;
    font-weight: 700;
    font-size: 12.5px;
    box-shadow: none;
}
.mc-seg button:hover { color: %TEXT%; }
.mc-seg button:checked {
    background-image: linear-gradient(135deg, %ACCENT%, %ACCENT2%);
    background-color: %ACCENT%;
    border-color: transparent;
    color: #ffffff;
}

/* ------------------------------------------------------- libadwaita cards */
.boxed-list {
    background-color: %CARD%;
    border: 1px solid %BORDER%;
    border-radius: 16px;
    color: %TEXT%;
}
.boxed-list row { background-color: transparent; }
.boxed-list row:hover { background-color: %HOVER%; }
.mc-content actionbar > revealer > box {
    background-color: %BG%;
    border-top: 1px solid %BORDER%;
}

.mc-kv {
    font-family: "Adwaita Mono", "JetBrains Mono", monospace;
    font-size: 12px;
    color: %TEXT%;
}
.mc-kv-dim {
    font-family: "Adwaita Mono", "JetBrains Mono", monospace;
    font-size: 12px;
    color: %DIM%;
}
.mc-sep { background-color: %BORDER%; min-height: 1px; }

/* ------------------------------------------------ lighting page surfaces */
.mc-window .kb-card {
    background-color: %CARD%;
    border: 1px solid %BORDER%;
    border-radius: 18px;
    box-shadow: 0 14px 34px %SHADOW%;
}
.mc-window .effect-tile {
    background-color: %CARD2%;
    background-image: none;
    border: 1px solid %BORDER%;
    border-radius: 14px;
    padding: 8px 10px;
    color: %TEXT%;
}
.mc-window .effect-tile:hover {
    background-color: %CARD%;
    border-color: %BORDER_STRONG%;
}
.mc-window .effect-tile.active {
    background-image: linear-gradient(160deg, %ACCENT_SOFT%, rgba(34,211,238,0.05));
    border: 1.5px solid %ACCENT%;
    box-shadow: 0 0 0 3px %ACCENT_SOFT%;
}
.mc-window .swatch { border-radius: 7px; }
.mc-window button.suggested-action,
.mc-window button.pill {
    border-radius: 11px;
    font-weight: 700;
}

scrollbar slider {
    background-color: %BORDER_STRONG%;
    border-radius: 99px;
    min-width: 8px;
    min-height: 8px;
}
scrollbar slider:hover { background-color: %DIM2%; }
""".strip()


def css_for(palette: dict) -> str:
    out = _CSS
    for key, value in palette.items():
        out = out.replace("%" + key.upper() + "%", value)
    return out


class _Theme:
    def __init__(self) -> None:
        self.provider: Gtk.CssProvider | None = None
        self.dark = True

    def install(self, dark: bool = True) -> None:
        display = Gdk.Display.get_default()
        if display is None:
            return
        if self.provider is None:
            self.provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                display, self.provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)
        self.set_scheme(dark)

    def set_scheme(self, dark: bool) -> None:
        self.dark = bool(dark)
        scheme = Adw.ColorScheme.FORCE_DARK if dark else Adw.ColorScheme.FORCE_LIGHT
        try:
            Adw.StyleManager.get_default().set_color_scheme(scheme)
        except Exception:
            pass
        if self.provider is not None:
            self.provider.load_from_string(css_for(DARK if dark else LIGHT))


_theme = _Theme()


def install(dark: bool = True) -> None:
    _theme.install(dark)


def set_scheme(dark: bool) -> None:
    _theme.set_scheme(dark)


def is_dark() -> bool:
    return _theme.dark


# --------------------------------------------------------------- helpers


def label(text: str, css: str = "", xalign: float = 0.0, wrap: bool = False) -> Gtk.Label:
    lbl = Gtk.Label(label=text, xalign=xalign, wrap=wrap)
    if css:
        for name in css.split():
            lbl.add_css_class(name)
    return lbl


def icon(name: str, size: int = 18, css: str = "") -> Gtk.Image:
    img = Gtk.Image.new_from_icon_name(name)
    img.set_pixel_size(size)
    if css:
        for cls in css.split():
            img.add_css_class(cls)
    return img


class Badge(Gtk.Label):
    TONES = {
        "accent": "mc-badge-accent",
        "ok": "mc-badge-ok",
        "warn": "mc-badge-warn",
        "err": "mc-badge-err",
        "muted": "mc-badge-muted",
    }

    def __init__(self, text: str = "", tone: str = "muted") -> None:
        super().__init__(label=text)
        self.add_css_class("mc-badge")
        self.tone = None
        self.set_tone(tone)

    def set_tone(self, tone: str) -> None:
        if self.tone:
            self.remove_css_class(self.TONES.get(self.tone, "mc-badge-muted"))
        self.tone = tone
        self.add_css_class(self.TONES.get(tone, "mc-badge-muted"))

    def update(self, text: str, tone: str | None = None) -> None:
        self.set_label(text)
        if tone:
            self.set_tone(tone)


class StatTile(Gtk.Box):
    """A compact metric card: icon, label, big value, meter, caption."""

    def __init__(self, icon_name: str, title: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.add_css_class("mc-stat")

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        head.append(icon(icon_name, 15, "mc-stat-icon"))
        head.append(label(title.upper(), "mc-stat-label"))
        self.append(head)

        self.value = label("—", "mc-stat-value")
        self.append(self.value)

        self.meter = Gtk.LevelBar(min_value=0.0, max_value=1.0)
        self.meter.add_css_class("mc-meter")
        self.meter.set_size_request(-1, 6)
        self.append(self.meter)

        self.caption = label("waiting for data", "mc-stat-caption", wrap=True)
        self.append(self.caption)

    def update(self, value: str, caption: str = "", fraction: float | None = None,
               tone: str | None = None) -> None:
        self.value.set_label(value)
        self.caption.set_label(caption)
        if fraction is None:
            self.meter.set_visible(False)
        else:
            self.meter.set_visible(True)
            self.meter.set_value(max(0.0, min(1.0, fraction)))
        for cls in ("mc-tone-ok", "mc-tone-warn", "mc-tone-err"):
            self.value.remove_css_class(cls)
        if tone:
            self.value.add_css_class(f"mc-tone-{tone}")


def card(title: str | None = None, subtitle: str | None = None,
         icon_name: str | None = None, css: str = "mc-card",
         spacing: int = 10) -> Gtk.Box:
    """Card container. The returned widget exposes ``.body`` for content."""
    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
    for cls in css.split():
        outer.add_css_class(cls)
    if title:
        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=11)
        if icon_name:
            head.append(icon(icon_name, 18, "mc-card-icon"))
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        text.append(label(title, "mc-card-title"))
        if subtitle:
            text.append(label(subtitle, "mc-card-sub", wrap=True))
        head.append(text)
        outer.append(head)
    body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=spacing)
    outer.append(body)
    outer.body = body  # type: ignore[attr-defined]
    return outer


def section(title: str, subtitle: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    box.add_css_class("mc-page-head")
    box.append(label(title, "mc-page-title"))
    if subtitle:
        box.append(label(subtitle, "mc-page-sub", wrap=True))
    return box


def subsection(title: str, subtitle: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
    box.set_margin_top(4)
    box.append(label(title, "mc-sub-title"))
    if subtitle:
        box.append(label(subtitle, "mc-card-sub", wrap=True))
    return box


def banner(title: str, subtitle: str = "", tone: str = "accent",
           icon_name: str | None = None) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=13)
    box.add_css_class("mc-banner")
    box.add_css_class(f"mc-banner-{tone}")
    if icon_name:
        box.append(icon(icon_name, 22, "mc-card-icon"))
    text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    text.append(label(title, "mc-banner-title", wrap=True))
    if subtitle:
        text.append(label(subtitle, "mc-banner-sub", wrap=True))
    box.append(text)
    box.title_label = text.get_first_child()  # type: ignore[attr-defined]
    sub = label(subtitle or "", "mc-banner-sub", wrap=True)
    sub.set_visible(bool(subtitle))
    text.append(sub)
    box.subtitle_label = sub  # type: ignore[attr-defined]
    return box


def key_value(key: str, value: str = "—") -> tuple[Gtk.Box, Gtk.Label]:
    row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
    row.append(label(key, "mc-card-sub"))
    spacer = Gtk.Box()
    spacer.set_hexpand(True)
    row.append(spacer)
    val = label(value, "mc-kv")
    val.set_xalign(1.0)
    val.set_selectable(True)
    row.append(val)
    return row, val


class ModeCard(Gtk.ToggleButton):
    """Large selectable card used for graphics/performance modes."""

    def __init__(self, key: str, name: str, description: str,
                 icon_name: str) -> None:
        super().__init__()
        self.key = key
        self.add_css_class("mc-mode")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(2)
        box.set_margin_bottom(2)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        top.append(icon(icon_name, 26, "mc-mode-icon"))
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        top.append(spacer)
        self.badge = Badge("", "muted")
        self.badge.set_visible(False)
        top.append(self.badge)
        box.append(top)

        box.append(label(name, "mc-mode-name"))
        desc = label(description, "mc-mode-desc", wrap=True)
        desc.set_lines(3)
        box.append(desc)
        self.set_child(box)

    def set_status(self, text: str = "", tone: str = "muted") -> None:
        self.badge.update(text, tone)
        self.badge.set_visible(bool(text))
