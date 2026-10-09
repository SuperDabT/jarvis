"""Colour tokens, fonts and the stylesheet: the visual identity of tal.dev.

The portfolio site is a two-colour system: a deep racing green and a warm cream
that swap roles between modes. Dark mode is cream ink on green; light mode is
green ink on cream paper. Every surface, hairline, muted text, tag and chart
ramp here is a tint or shade of that one pair (``mix(bg, ink, k)``), so the UI
reads as the duo rather than a palette. One third colour, the terracotta /
brick ``signal``, is kept for live data only (today, the watching dot, the
listening orb, failing checks, new arrivals) and never for clickable things.

Type follows the site's voices: Archivo condensed for display (page title,
numbers), IBM Plex Mono for small uppercase labels, Newsreader for reading
text (the briefing), IBM Plex Sans for the interface and Inter for buttons.
The fonts ship in ``gui/fonts`` (SIL OFL, licences beside them) and are
registered at startup; any that fail to load fall back to system fonts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette

# Tag roles (gui/data.py maps finding statuses and card types onto these).
TAG_NAMES = ("open", "ok", "warning", "needs", "new", "fail", "electrical", "research")


def mix(a: str, b: str, k: float) -> str:
    """``a`` moved towards ``b`` by ``k`` (0..1), as #rrggbb."""
    ca, cb = QColor(a), QColor(b)
    return "#{:02x}{:02x}{:02x}".format(
        round(ca.red() + (cb.red() - ca.red()) * k),
        round(ca.green() + (cb.green() - ca.green()) * k),
        round(ca.blue() + (cb.blue() - ca.blue()) * k))


@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str  # page background (the base colour of the pair)
    ink: str  # the other colour of the pair: accents, marks, rings
    side: str  # chat panel / peek background
    text: str
    muted: str  # secondary text
    faint: str  # tertiary text (axis labels, hints)
    border: str  # hairlines
    border_strong: str  # inputs
    btn_line: str  # outline buttons
    fill: str  # cards: callout, KPI tiles
    fill2: str  # chart tracks, user bubble
    inset: str  # code / prompt boxes (below the cards)
    hover: str  # hovered ghost buttons
    accent: str  # primary buttons (the ink)
    accent_hover: str
    on_accent: str
    signal: str  # live data only, never clickable
    signal_text: str  # signal as readable text on signal_soft
    signal_soft: str
    unsure: str  # "unconfirmed" text
    unsure_bg: str
    warn: str  # status-line problems
    red: str  # today line, limit violations (the signal)
    green: str  # progress / closed (the ink)
    amber: str  # "may be done" hatch (a mid tint of the ink)
    shadow: str
    grid: tuple[str, str, str, str, str]  # contribution grid levels 0..4
    series: tuple[str, ...]  # categorical order: lightness steps of the ink
    tags: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # bg, fg, border
    orb_core: str = "#fff3d6"
    orb_ring: str = "#efe3c6"
    watermark: float = 0.08  # alpha of the oversized section word

    def tag(self, name: str) -> tuple[str, str, str]:
        return self.tags.get(name) or self.tags["open"]

    def tint(self, k: float) -> str:
        """The ink over the page at ``k`` (0..1)."""
        return mix(self.bg, self.ink, k)


def _tokens(name: str, *, bg: str, ink: str, text: str, muted: str, faint: str, side: str,
            fill: str, fill2: str, inset: str, border: str, border_strong: str,
            btn_line: str, hover: str, accent_hover: str, on_accent: str, signal: str,
            signal_text: str, orb_core: str, orb_ring: str, watermark: float,
            series_k: tuple[float, ...], grid_k: tuple[float, ...], shadow: str) -> Tokens:
    t = lambda k: mix(bg, ink, k)  # noqa: E731 - a tint of the pair
    soft = mix(bg, signal, 0.16)
    return Tokens(
        name=name, bg=bg, ink=ink, side=side, text=text, muted=muted, faint=faint,
        border=border, border_strong=border_strong, btn_line=btn_line, fill=fill,
        fill2=fill2, inset=inset, hover=hover, accent=ink, accent_hover=accent_hover,
        on_accent=on_accent, signal=signal, signal_text=signal_text, signal_soft=soft,
        unsure=mix(bg, text, 0.86), unsure_bg=t(0.16), warn=signal_text, red=signal,
        green=ink, amber=t(0.55), shadow=shadow,
        grid=tuple(t(k) for k in grid_k),  # type: ignore[arg-type]
        series=tuple(t(k) for k in series_k),
        tags={
            "open": (t(0.08), muted, "none"),
            "ok": (t(0.13), text, "none"),
            "electrical": (t(0.13), text, "none"),
            "research": ("transparent", text, f"1px solid {t(0.38)}"),
            "warning": (t(0.24), text, f"1px solid {t(0.6)}"),
            "needs": ("transparent", muted, f"1px dashed {t(0.5)}"),
            "new": ("transparent", signal_text, f"1px solid {signal}"),
            "fail": (soft, signal_text, f"1px solid {mix(bg, signal, 0.5)}"),
        },
        orb_core=orb_core, orb_ring=orb_ring, watermark=watermark,
    )


# Dark: the site's primary look, cream ink on racing green.
DARK = _tokens(
    "dark", bg="#00160e", ink="#efe3c6", text="#fff7e6", muted="#cdd0bf", faint="#8fa294",
    side="#031b12", fill="#072419", fill2="#113628", inset="#010d08", border="#16392b",
    border_strong="#24493a", btn_line="#4a6a5a", hover="#0c2a1f", accent_hover="#fff7e6",
    on_accent="#00160e", signal="#d27a5a", signal_text="#e3906f", orb_core="#fff3d6",
    orb_ring="#efe3c6", watermark=0.085, series_k=(0.96, 0.72, 0.52, 0.38),
    grid_k=(0.07, 0.26, 0.46, 0.70, 0.96), shadow="#000000",
)

# Light: the same pair inverted, green ink on cream paper.
LIGHT = _tokens(
    "light", bg="#fff7e6", ink="#00311e", text="#00311e", muted="#4a5d53", faint="#5f6f65",
    side="#faf0d9", fill="#faf0d9", fill2="#efe0c0", inset="#f1e2c1", border="#e4d6ba",
    border_strong="#d8c9a8", btn_line="#978a76", hover="#f5e9cd", accent_hover="#204e3a",
    on_accent="#fffaf0", signal="#9c3320", signal_text="#9c3320", orb_core="#1d5a3f",
    orb_ring="#00311e", watermark=0.07, series_k=(0.96, 0.74, 0.56, 0.42),
    grid_k=(0.08, 0.28, 0.48, 0.70, 0.95), shadow="#3c2d14",
)

BODY_PX = 14
SMALL_PX = 12
TITLE_PX = 46

# ---------------------------------------------------------------- fonts

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES = {  # file -> voice
    "archivo.woff2": "display",
    "ibm-plex-mono-500.woff2": "mono",
    "ibm-plex-sans-400.woff2": "sans",
    "ibm-plex-sans-500.woff2": "sans",
    "newsreader.woff2": "serif",
    "inter.woff2": "button",
}
# Fallbacks when a bundled font cannot be loaded (Qt without WOFF2 support).
SANS_FALLBACK = ("IBM Plex Sans", "Inter", "Inter Variable", "Adwaita Sans", "Cantarell",
                 "Noto Sans")
MONO_FALLBACK = ("IBM Plex Mono", "JetBrains Mono", "Adwaita Mono", "DejaVu Sans Mono",
                 "Liberation Mono")
SERIF_FALLBACK = ("Newsreader", "Noto Serif", "DejaVu Serif", "Liberation Serif")

_families: dict[str, str] | None = None


def load_fonts() -> dict[str, str]:
    """Register the bundled fonts (once) and return the family per voice:
    display, mono, sans, serif, button. Missing ones fall back to system fonts."""
    global _families
    if _families is not None:
        return _families
    from PySide6.QtGui import QFontDatabase

    found: dict[str, str] = {}
    for fname, voice in FONT_FILES.items():
        path = FONT_DIR / fname
        if not path.exists():
            continue
        fid = QFontDatabase.addApplicationFont(str(path))
        fams = QFontDatabase.applicationFontFamilies(fid) if fid >= 0 else []
        if fams:
            found.setdefault(voice, fams[0])
    available = set(QFontDatabase.families())

    def first(names: tuple[str, ...], default: str) -> str:
        return next((n for n in names if n in available), default)

    sans = found.get("sans") or first(SANS_FALLBACK, QGuiApplication.font().family())
    _families = {
        "sans": sans,
        "display": found.get("display") or sans,
        "mono": found.get("mono") or first(MONO_FALLBACK, "monospace"),
        "serif": found.get("serif") or first(SERIF_FALLBACK, sans),
        "button": found.get("button") or sans,
    }
    _families["bundled"] = ",".join(sorted(found))  # what actually loaded (for reports)
    return _families


def family(voice: str) -> str:
    return load_fonts()[voice]


def _axes(f: QFont, weight: int, width: int) -> None:
    """Variable-font axes where Qt supports them (6.7+); else plain weight/stretch."""
    try:
        f.setVariableAxis(QFont.Tag("wght"), float(weight))
        f.setVariableAxis(QFont.Tag("wdth"), float(width))
    except (AttributeError, TypeError):
        f.setWeight(QFont.Weight(min(900, max(100, weight))))
        f.setStretch(width)


def display_font(px: int, weight: int = 800, width: int = 82, caps: bool = False,
                 spacing_em: float = -0.01) -> QFont:
    """Archivo: condensed, heavy (the site's headings and stat numbers)."""
    f = QFont(family("display"))
    f.setPixelSize(px)
    _axes(f, weight, width)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    if spacing_em:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing_em)
    return f


def mono_font(px: int = 11, caps: bool = True, spacing_em: float = 0.12) -> QFont:
    """IBM Plex Mono: the site's small uppercase, letter-spaced labels."""
    f = QFont(family("mono"))
    f.setPixelSize(px)
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if caps:
        f.setCapitalization(QFont.Capitalization.AllUppercase)
    if spacing_em:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing_em)
    return f


def pick_font() -> QFont:
    """The interface font: IBM Plex Sans (bundled), else a system sans."""
    font = QFont(QGuiApplication.font())
    font.setFamily(family("sans"))
    font.setPixelSize(BODY_PX)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def mono_family() -> str:
    return family("mono")


# ---------------------------------------------------------------- scheme


def system_scheme() -> str:
    """"dark" or "light" from Qt 6.5+ style hints; dark (the site's primary
    look) when the system does not say."""
    app = QGuiApplication.instance()
    if app is None:
        return "dark"
    try:
        scheme = app.styleHints().colorScheme()
    except AttributeError:  # Qt < 6.5
        return "dark"
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


def tokens_for(scheme: str | None = None) -> Tokens:
    return DARK if (scheme or system_scheme()) == "dark" else LIGHT


def palette(t: Tokens) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.bg,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.bg,
        QPalette.ColorRole.AlternateBase: t.fill,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.bg,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.Highlight: t.tint(0.35),
        QPalette.ColorRole.HighlightedText: t.text,
        QPalette.ColorRole.Link: t.muted,
        QPalette.ColorRole.LinkVisited: t.muted,
        QPalette.ColorRole.ToolTipBase: t.ink,  # tooltips invert the pair
        QPalette.ColorRole.ToolTipText: t.bg,
        QPalette.ColorRole.PlaceholderText: t.faint,
        QPalette.ColorRole.Mid: t.border,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p


def stylesheet(t: Tokens) -> str:
    fam = load_fonts()
    sans, mono, serif, btn = (f'"{fam[k]}"' for k in ("sans", "mono", "serif", "button"))
    ring = t.tint(0.35)  # the cream (or green) hover ring
    sel = t.tint(0.3)
    return f"""
* {{ outline: none; }}
QMainWindow, #page, #dash {{ background: {t.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ color: {t.text}; background: transparent; }}
QToolTip {{
    background: {t.ink}; color: {t.bg}; border: none; border-radius: 6px;
    padding: 5px 8px; font-size: {SMALL_PX}px;
}}

/* top bar: the site's nav */
#topBar {{ background: {t.bg}; border-bottom: 1px solid {t.border}; }}
#crumb {{ color: {t.muted}; }}
#crumbStrong {{ color: {t.muted}; }}
#logo {{ color: {t.text}; }}

/* page head */
#pageTitle {{ color: {t.text}; }}
#meta {{ color: {t.muted}; }}
#metaSep {{ color: {t.btn_line}; }}

/* section headers: "02 MILESTONES" in mono, a short rule under it */
#sectionNum {{ color: {t.faint}; }}
#sectionHead {{ color: {t.muted}; }}
#sectionRule {{ background: {t.tint(0.4)}; border: none; border-radius: 1px; }}
#sectionHint {{ color: {t.faint}; font-size: 12px; }}
#subHead {{ color: {t.muted}; }}
#hairline {{ background: {t.border}; border: none; }}

/* briefing callout: a site card */
#callout {{ background: {t.fill}; border: 1px solid {t.border}; border-radius: 14px; }}
#headline {{ color: {t.text}; }}
#sentence {{ font-family: {serif}; font-size: 17px; color: {t.text}; }}
#bullet {{ font-family: {serif}; color: {t.faint}; font-size: 17px; }}
#evidence {{ color: {t.faint}; font-size: {SMALL_PX}px; }}
#emptyText {{ color: {t.muted}; font-size: {BODY_PX}px; }}
#calloutFoot {{ color: {t.faint}; font-size: {SMALL_PX}px; }}

/* KPI tiles: the site's stat cells */
#kpi {{ background: {t.fill}; border: 1px solid {t.border}; border-radius: 14px; }}
#kpi:hover {{ border-color: {ring}; }}
#kpiLabel {{ color: {t.faint}; }}
#kpiValue {{ color: {t.text}; }}
#kpiValue[tone="bad"] {{ color: {t.signal_text}; }}
#kpiValue[tone="warn"] {{ color: {t.text}; }}
#kpiValue[tone="good"] {{ color: {t.text}; }}
#kpiValue[tone="muted"] {{ color: {t.faint}; }}
#kpiSub {{ color: {t.muted}; font-size: 12px; }}
#kpiSub[unsure="true"] {{ color: {t.unsure}; font-style: italic; }}

/* chart blocks */
#caption {{ color: {t.muted}; font-size: 12px; }}
#railHead {{ color: {t.text}; }}
#railSub {{ color: {t.faint}; }}

/* suggestions table */
#table {{ background: transparent; }}
#tableHead {{ border-top: 1px solid {t.border}; border-bottom: 1px solid {t.border}; }}
#colHead {{ color: {t.faint}; }}
#row {{ border-bottom: 1px solid {t.border}; background: transparent; }}
/* row hover / selected ring is painted by TableRow (animated) */
#rowTitle {{ color: {t.text}; font-size: {BODY_PX}px; }}
#rowWhy {{ color: {t.faint}; font-size: 12px; }}
#tag {{ border-radius: 8px; padding: 1px 8px; font-size: 12px; }}

/* buttons: outline pills (secondary), cream / green pills (primary) */
QPushButton {{
    color: {t.text}; background: transparent; border: 1px solid {t.btn_line};
    border-radius: 14px; padding: 6px 14px; font-family: {btn}; font-size: 13px;
    font-weight: 600;
}}
QPushButton:hover {{ border-color: {t.text}; }}
QPushButton:pressed {{ background: {t.hover}; }}
QPushButton:disabled {{ color: {t.faint}; border-color: {t.border_strong}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
QPushButton#ghost {{
    border: none; color: {t.muted}; padding: 6px 10px; font-weight: 600;
}}
QPushButton#ghost:hover {{ background: {t.hover}; color: {t.text}; }}
QPushButton#ghost[active="true"] {{ background: {t.hover}; color: {t.text}; }}
QPushButton#primary {{
    color: {t.on_accent}; background: {t.accent}; border: 1px solid {t.accent};
}}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary[flash="true"] {{
    color: {t.text}; background: transparent; border-color: {t.text};
}}
QPushButton#rowCopy {{
    border: 1px solid {t.btn_line}; color: {t.muted}; padding: 3px 8px; font-size: 12px;
    border-radius: 10px; background: transparent;
}}
QPushButton#rowCopy:hover {{ color: {t.text}; border-color: {t.text}; }}
QPushButton#rowCopy[flash="true"] {{ color: {t.on_accent}; background: {t.accent};
    border-color: {t.accent}; }}
QPushButton#chip {{
    border: 1px solid {t.btn_line}; border-radius: 16px; padding: 7px 14px;
    color: {t.text}; background: transparent; font-size: 13px; text-align: left;
}}
QPushButton#chip:hover {{ border-color: {t.text}; background: {t.hover}; }}
QPushButton#srcChip {{
    border: 1px solid {t.border}; border-radius: 8px; padding: 2px 8px;
    font-family: {mono}; font-size: 11px; font-weight: 500; color: {t.muted};
    background: {t.fill};
}}
QPushButton#srcChip:hover {{ color: {t.text}; border-color: {ring}; }}
QPushButton#srcChip[more="true"] {{ background: transparent; border-style: dashed; }}
QPushButton#thinkToggle {{
    border: 1px solid {t.btn_line}; border-radius: 11px; padding: 3px 10px;
    font-family: {mono}; font-size: 11px; font-weight: 600; color: {t.muted};
    background: transparent;
}}
QPushButton#thinkToggle:hover {{ color: {t.text}; border-color: {t.text}; }}
QPushButton#thinkToggle:checked {{
    color: {t.on_accent}; background: {t.accent}; border-color: {t.accent};
}}
QPushButton#send {{
    border: none; border-radius: 14px; background: {t.accent}; color: {t.on_accent};
    font-size: 15px; font-weight: 700; padding: 0;
}}
QPushButton#send:hover {{ background: {t.accent_hover}; }}
QPushButton#send:disabled {{ background: {t.fill2}; color: {t.faint}; }}

QMenu {{
    background: {t.fill}; color: {t.text};
    border: 1px solid {t.border_strong}; border-radius: 10px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; font-size: 13px; }}
QMenu::item:selected {{ background: {t.hover}; }}
QMenu::item:disabled {{ color: {t.faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

/* peek drawer */
#peek {{ background: {t.side}; border-left: 1px solid {t.border_strong}; }}
#peekTitle {{ color: {t.text}; }}
#propKey {{ color: {t.faint}; }}
#propVal {{ color: {t.text}; font-size: 13px; }}
#peekWhy {{ font-family: {serif}; color: {t.text}; font-size: 17px; }}
#promptBox {{
    background: {t.inset}; color: {t.muted}; border: 1px solid {t.border};
    border-radius: 10px; padding: 10px 12px; font-size: 12px;
    selection-background-color: {sel};
}}

/* chat panel */
#chat {{ background: {t.side}; border-left: 1px solid {t.border}; }}
#chatTitle {{ color: {t.text}; }}
#chatSub {{ color: {t.faint}; }}
#emptyTitle {{ color: {t.text}; }}
#emptyHint {{ font-family: {serif}; color: {t.muted}; font-size: 15px; }}
#userBubble {{ background: {t.fill2}; border-radius: 14px; }}
#userText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botName {{ color: {t.muted}; }}
#botMeta {{ color: {t.faint}; }}
#thinking {{ color: {t.muted}; font-size: 13px; }}
#botNote {{ color: {t.unsure}; font-size: 12px; font-style: italic; }}
#botError {{ color: {t.muted}; font-size: 13px; font-style: italic; }}
#composer {{
    background: {t.inset}; border: 1px solid {t.border_strong}; border-radius: 14px;
}}
#composer[focus="true"] {{ border-color: {ring}; }}
#composerEdit {{
    background: transparent; border: none; color: {t.text}; font-size: {BODY_PX}px;
    selection-background-color: {sel};
}}
#composerHint {{ color: {t.faint}; font-size: 11px; }}
#voiceCaption {{ color: {t.muted}; font-size: 12px; }}
#voiceNote {{ color: {t.faint}; font-size: 12px; font-style: italic; }}
#voiceNote[level="error"] {{
    color: {t.muted}; font-style: normal; background: {t.fill}; border-radius: 10px;
    padding: 8px 10px;
}}

#statusBar {{ background: {t.bg}; border-top: 1px solid {t.border}; }}
#status {{ color: {t.faint}; font-family: {mono}; font-size: 11px; font-weight: 500; }}
#status[level="warn"] {{ color: {t.warn}; }}
#status[level="ok"] {{ color: {t.muted}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t.btn_line}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QPlainTextEdit QScrollBar:vertical {{ width: 8px; }}
#unsureTag {{ color: {t.unsure}; background: {t.unsure_bg}; }}
"""


def apply(app: QGuiApplication, scheme: str | None = None) -> Tokens:
    """Apply palette + stylesheet for ``scheme`` (None = follow the system)."""
    t = tokens_for(scheme)
    load_fonts()
    app.setPalette(palette(t))
    if hasattr(app, "setStyleSheet"):
        app.setStyleSheet(stylesheet(t))
    return t
