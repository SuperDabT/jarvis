"""Colour tokens, fonts and the stylesheet: Jarvis's "project brain" look.

Dark only. A neutral charcoal base (#161719) with near-white ink (#E8E8EA);
the rail and chat sit a shade darker, cards a shade lighter, separated by tone
rather than borders (Linear's layout, Apple's grouped surfaces). One mint (#5DDBA6) marks
everything live: graph nodes with recent activity, status pills, sparklines,
the orb while it listens or speaks, new arrivals, focus. A muted red is kept
for genuinely failing checks only.

Type: Adwaita Sans (Inter's design, every weight, installed with the desktop)
for headings, labels, values and reading, on one scale (``TYPE``, applied with
``typeset``); JetBrains Mono only for what is technical: issue numbers, hashes,
paths, label tags, the graph's labels, the ticker and keyboard hints.
Headings are sentence case; mono stays lower case.

Green (the mint) is for what is live, active or positive: selection, primary
actions, good status. Headings, borders and labels stay in the ink's tints.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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
    bg: str  # page background (green-black)
    ink: str  # the pearl: marks, rings, primary buttons
    side: str  # side columns / chat panel background
    text: str
    muted: str  # secondary text
    faint: str  # tertiary text (axis labels, hints)
    border: str  # hairlines
    border_strong: str  # inputs
    btn_line: str  # outline buttons
    fill: str  # panels
    fill2: str  # chart tracks, user bubble
    inset: str  # code / prompt boxes, the composer
    hover: str  # hovered ghost buttons
    accent: str  # primary buttons (the pearl)
    accent_hover: str
    on_accent: str
    signal: str  # the mint: live data only
    signal_text: str  # mint as readable text
    signal_soft: str  # mint wash behind live pills
    unsure: str  # "unconfirmed" text
    unsure_bg: str
    warn: str  # status-line problems
    red: str  # failing checks, limit violations
    green: str  # progress / closed (mint)
    amber: str  # "may be done" hatch (a mid tint of the pearl)
    shadow: str
    grid: tuple[str, str, str, str, str]  # activity levels 0..4 (towards mint)
    series: tuple[str, ...]  # categorical order
    tags: dict[str, tuple[str, str, str]] = field(default_factory=dict)  # bg, fg, border
    orb_core: str = "#f0fff7"
    orb_ring: str = "#dce8e2"
    watermark: float = 0.06
    panel: str = "#1d1e21"  # cockpit panels

    def tag(self, name: str) -> tuple[str, str, str]:
        return self.tags.get(name) or self.tags["open"]

    def tint(self, k: float) -> str:
        """The ink over the background at ``k`` (0..1)."""
        return mix(self.bg, self.ink, k)


# Neutral charcoal, a near-white ink and a mint.
BG = "#161719"
INK = "#e8e8ea"
PEARL = INK  # the ink's old name, kept for older imports
MINT = "#5ddba6"
RED = "#e5787a"


def _dark() -> Tokens:
    t = lambda k: mix(BG, INK, k)  # noqa: E731 - a tint of the pair
    m = lambda k: mix(BG, MINT, k)  # noqa: E731 - towards the mint
    return Tokens(
        name="dark", bg=BG, ink=INK, side="#111214", text=INK, muted=t(0.78),
        faint=t(0.6), border=t(0.085), border_strong=t(0.17), btn_line=t(0.28),
        fill=t(0.045), fill2=t(0.1), inset="#1f2023", hover=t(0.07), accent=INK,
        accent_hover="#f2fbf6", on_accent=BG, signal=MINT, signal_text=MINT,
        signal_soft=m(0.14), unsure=t(0.72), unsure_bg=t(0.1), warn=MINT, red=RED,
        green=MINT, amber=t(0.45), shadow="#08080a",
        grid=(t(0.07), m(0.3), m(0.5), m(0.72), MINT),
        series=(INK, MINT, t(0.55), m(0.55)),
        tags={
            "open": (t(0.08), t(0.74), "none"),
            "ok": (m(0.12), MINT, "none"),
            "electrical": (t(0.1), INK, "none"),
            "research": ("transparent", INK, f"1px solid {t(0.3)}"),
            "warning": (t(0.14), INK, f"1px solid {t(0.45)}"),
            "needs": ("transparent", t(0.74), f"1px dashed {t(0.4)}"),
            "new": ("transparent", MINT, f"1px solid {m(0.7)}"),
            "fail": (mix(BG, RED, 0.16), RED, f"1px solid {mix(BG, RED, 0.5)}"),
        },
        panel="#1d1e21",
    )


DARK = _dark()
LIGHT = DARK  # dark only; kept so older imports keep working

BODY_PX = 14
SMALL_PX = 12
TITLE_PX = 22

# The type scale: role -> (voice, px, weight). Mono only for technical text.
TYPE: dict[str, tuple[str, int, int]] = {
    "big": ("sans", 22, 650),      # a headline number (open issues)
    "title": ("sans", 15, 600),    # an item's name (inspector, active milestone)
    "head": ("sans", 13, 600),     # panel and section headings
    "value": ("sans", 13, 600),    # a vital's value
    "body": ("sans", 13, 400),     # running text
    "label": ("sans", 12, 400),    # a vital's key, a row's secondary text
    "meta": ("sans", 12, 400),     # hints, captions, timestamps
    "pill": ("sans", 11, 500),     # status pills
    "code": ("mono", 11, 400),     # #12, 02fb2d2, paths, model names
    "tag": ("mono", 10, 500),      # label tags (P1, blocker, phase-1)
}

# ---------------------------------------------------------------- fonts

FONT_DIR = Path(__file__).with_name("fonts")
FONT_FILES: dict[str, str] = {}  # bundled file -> voice (none: system fonts suffice)
SANS_FALLBACK = ("Adwaita Sans", "Inter", "Inter Variable", "Noto Sans", "Cantarell")
MONO_FALLBACK = ("JetBrains Mono", "JetBrainsMono Nerd Font", "JetBrainsMono NF",
                 "iA Writer Mono S", "Adwaita Mono", "Noto Sans Mono", "DejaVu Sans Mono",
                 "Liberation Mono")

_families: dict[str, str] | None = None


def load_fonts() -> dict[str, str]:
    """Find the reading sans and the mono font (once). Returns the family per
    voice: sans, display, mono, serif, button (display/serif/button = sans)."""
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
        "sans": sans, "display": sans, "serif": sans, "button": sans,
        "mono": first(MONO_FALLBACK, "monospace"),
    }
    _families["bundled"] = ",".join(sorted(found))  # what actually loaded (for reports)
    return _families


def family(voice: str) -> str:
    return load_fonts()[voice]


def display_font(px: int, weight: int = 650, width: int = 100, caps: bool = False,
                 spacing_em: float = -0.01) -> QFont:
    """The sans, semibold: headlines and big numbers. Never all caps (``caps`` and
    ``width`` are accepted for older callers and ignored)."""
    f = QFont(family("display"))
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(min(700, max(100, weight))))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if spacing_em:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing_em)
    return f


def mono_font(px: int = 11, caps: bool = False, spacing_em: float = 0.0,
              weight: int = 400) -> QFont:
    """JetBrains Mono: the cockpit voice. Lower case (``caps`` is ignored)."""
    f = QFont(family("mono"))
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(weight))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    if spacing_em:
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, px * spacing_em)
    return f


def type_font(role: str) -> QFont:
    """The font of a ``TYPE`` role."""
    voice, px, weight = TYPE[role]
    if voice == "mono":
        return mono_font(px, weight=weight)
    f = QFont(family("sans"))
    f.setPixelSize(px)
    f.setWeight(QFont.Weight(weight))
    f.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return f


def typeset(widget, role: str):
    """Give ``widget`` the font of a ``TYPE`` role (returns it, for chaining)."""
    widget.setFont(type_font(role))
    widget.setProperty("type", role)
    return widget


def pick_font() -> QFont:
    """The reading font (the sans)."""
    font = QFont(QGuiApplication.font())
    font.setFamily(family("sans"))
    font.setPixelSize(BODY_PX)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def mono_family() -> str:
    return family("mono")


# ---------------------------------------------------------------- scheme


def system_scheme() -> str:
    return "dark"


def tokens_for(scheme: str | None = None) -> Tokens:
    return DARK


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
        QPalette.ColorRole.Highlight: t.tint(0.3),
        QPalette.ColorRole.HighlightedText: t.text,
        QPalette.ColorRole.Link: t.signal,
        QPalette.ColorRole.LinkVisited: t.signal,
        QPalette.ColorRole.ToolTipBase: t.inset,
        QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.PlaceholderText: t.faint,
        QPalette.ColorRole.Mid: t.border,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p


def stylesheet(t: Tokens) -> str:
    fam = load_fonts()
    sans, mono = f'"{fam["sans"]}"', f'"{fam["mono"]}"'
    sel = t.tint(0.28)
    live_line = mix(t.bg, t.signal, 0.6)
    return f"""
* {{ outline: none; }}
QMainWindow, #page {{ background: {t.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ color: {t.text}; background: transparent; }}
QToolTip {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 5px 8px; font-size: {SMALL_PX}px;
}}

/* cockpit chrome */
#topBar, #statusRow, #ticker {{ background: {t.bg}; }}
#topBar {{ border-bottom: 1px solid {t.tint(0.07)}; }}
#ticker {{ border-top: 1px solid {t.tint(0.06)}; }}
#logo {{ color: {t.text}; }}
#crumb {{ color: {t.faint}; }}
#clock, #pulseText, #statusText, #tickerHint {{ color: {t.faint}; }}
#tickerText {{ color: {t.muted}; }}
#tickerText[level="warn"] {{ color: {t.red}; }}
#tickerText[level="ok"] {{ color: {t.signal}; }}
#pill {{
    border-radius: 9px; padding: 2px 9px; color: {t.muted}; border: none;
    background: {t.fill2};
}}
#pill[live="true"] {{ color: {t.signal_text}; background: {t.signal_soft}; }}
#pill[level="fail"] {{ color: {t.red}; background: {mix(t.bg, t.red, 0.16)}; }}

/* panels */
#panel {{ background: {t.panel}; border: 1px solid {t.tint(0.045)}; border-radius: 10px; }}
#graphFrame {{ background: transparent; border: none; }}
#panelHead {{ color: {t.text}; }}
#panelHint {{ color: {t.faint}; }}
#vKey {{ color: {t.faint}; }}
#vVal {{ color: {t.text}; }}
#vVal[live="true"] {{ color: {t.signal}; }}
#vVal[level="fail"] {{ color: {t.red}; }}
#vNote {{ color: {t.faint}; }}
#vSub {{ color: {t.muted}; }}
#vVal[tone="good"] {{ color: {t.signal}; }}
#vVal[tone="warn"] {{ color: {t.unsure}; }}
#vVal[tone="bad"] {{ color: {t.red}; }}
#vVal[tone="muted"] {{ color: {t.faint}; }}
#issueCount {{ color: {t.text}; }}
QPushButton#issueRow, QPushButton#msRow {{
    background: transparent; border: 1px solid transparent; border-radius: 6px; text-align: left;
}}
QPushButton#issueRow:hover, QPushButton#msRow:hover {{ background: {t.hover}; border-color: {t.border}; }}
#issueNum {{ color: {t.faint}; }}
#issueTitle {{ color: {t.text}; }}
#issueTag {{ color: {t.muted}; background: {t.fill2}; border-radius: 4px; padding: 1px 5px; }}
#issueTag[kind="blocker"] {{ color: {t.red}; background: {mix(t.bg, t.red, 0.16)}; }}
#issueTag[kind="priority"] {{ color: {t.text}; }}
#issueTag[kind="blocked"] {{ color: {t.faint}; }}
#issueGo {{ color: {t.faint}; }}
#msTitle {{ color: {t.text}; }}
#hairline {{ background: {t.border}; border: none; }}

/* briefing HUD */
#hud {{
    background: {t.panel}; border: 1px solid {t.tint(0.07)};
    border-radius: 10px;
}}
#hudHead {{ color: {t.muted}; }}
#headline {{ color: {t.text}; }}
#sentence {{ font-family: {sans}; font-size: 13px; font-weight: 400; color: {t.text}; }}
#bullet {{ color: {t.signal}; font-size: 13px; }}
#evidence {{ color: {t.faint}; font-size: 11px; }}
#emptyText {{ color: {t.muted}; font-size: 13px; }}
#unsureTag {{ color: {t.unsure}; background: {t.unsure_bg}; }}

/* inspector + stream */
#inspTitle {{ color: {t.text}; font-family: {sans}; font-size: 15px; font-weight: 600; }}
#inspText {{ color: {t.muted}; font-family: {sans}; font-size: 13px; font-weight: 400; }}
#inspKind {{ color: {t.muted}; }}
#inspAi {{ color: {t.muted}; font-style: italic; }}
#inspSource {{ color: {t.text}; font-family: {mono}; font-size: 11px; }}
#inspAttention {{ color: {t.unsure}; }}
#relName {{ color: {t.text}; }}
#relNote {{ color: {t.faint}; }}
#relNote[uncertain="true"] {{ color: {t.unsure}; font-style: italic; }}
#promptBox {{
    background: {t.inset}; color: {t.muted}; border: 1px solid {t.border};
    border-radius: 8px; padding: 8px 10px; font-size: 11px;
    selection-background-color: {sel};
}}
#streamText {{ color: {t.text}; }}
#streamMeta {{ color: {t.faint}; }}
#tag {{ border-radius: 7px; padding: 0px 7px; font-size: 11px; font-family: {mono}; }}

QPushButton {{
    color: {t.text}; background: {t.tint(0.07)}; border: 1px solid {t.tint(0.1)};
    border-radius: 7px; padding: 5px 12px; font-family: {sans}; font-size: 13px;
}}
QPushButton:hover {{ border-color: {t.tint(0.16)}; background: {t.tint(0.11)}; }}
QPushButton:pressed {{ background: {t.tint(0.15)}; border-color: {t.tint(0.22)}; }}
QPushButton:disabled {{ color: {t.faint}; background: transparent; border-color: {t.border}; }}
QPushButton:focus {{ border-color: {t.tint(0.32)}; }}
QPushButton#primary:disabled {{ color: {t.faint}; background: {t.fill2}; border-color: {t.fill2}; }}
QPushButton#primary:focus {{ border-color: {t.signal}; }}
QPushButton#linkBtn:focus, QPushButton#filter:focus, QPushButton#ghost:focus {{
    background: {t.hover};
}}
QPushButton#linkBtn:disabled {{ color: {t.faint}; }}
QComboBox:hover {{ border-color: {t.btn_line}; }}
QComboBox:focus {{ border-color: {t.muted}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
QPushButton#ghost {{ border: none; background: transparent; color: {t.muted}; padding: 4px 8px; }}
QPushButton#ghost:hover {{ background: {t.hover}; color: {t.text}; }}
QPushButton#ghost[active="true"] {{ background: {t.hover}; color: {t.signal}; }}
QPushButton#primary {{ color: {t.on_accent}; background: {t.accent}; border: 1px solid {t.accent}; }}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary:pressed {{ background: {t.tint(0.8)}; border-color: {t.tint(0.8)}; }}
QPushButton#primary[flash="true"] {{ color: {t.signal}; background: transparent; border-color: {t.signal}; }}
QPushButton#filter {{ border: none; background: transparent; color: {t.faint}; padding: 2px 5px; font-size: 12px; }}
QPushButton#filter:checked {{ color: {t.text}; }}
QPushButton#filter:hover {{ color: {t.text}; background: {t.hover}; }}
QPushButton#segment {{
    border: 1px solid {t.border_strong}; border-radius: 0; padding: 2px 10px; font-size: 12px;
    color: {t.muted}; background: transparent;
}}
QPushButton#segment:first {{ border-top-left-radius: 6px; border-bottom-left-radius: 6px; }}
QPushButton#segment:last {{ border-top-right-radius: 6px; border-bottom-right-radius: 6px; }}
QPushButton#segment:hover {{ color: {t.text}; background: {t.hover}; }}
QPushButton#segment:checked {{ color: {t.signal_text}; background: {t.signal_soft}; }}
QPushButton#chip {{
    border: 1px solid {t.btn_line}; border-radius: 8px; padding: 6px 12px;
    color: {t.text}; background: transparent; font-family: {sans}; font-size: 13px;
    text-align: left;
}}
QPushButton#chip:hover {{ border-color: {t.signal}; color: {t.signal}; }}
QPushButton#srcChip {{
    border: 1px solid {t.border_strong}; border-radius: 6px; padding: 1px 7px;
    font-family: {mono}; font-size: 11px; color: {t.muted}; background: {t.fill};
}}
QPushButton#srcChip:hover {{ color: {t.signal}; border-color: {live_line}; }}
QPushButton#srcChip[more="true"] {{ background: transparent; border-style: dashed; }}
QPushButton#thinkToggle {{
    border: 1px solid {t.btn_line}; border-radius: 9px; padding: 2px 9px;
    font-family: {sans}; font-size: 12px; color: {t.muted}; background: transparent;
}}
QPushButton#thinkToggle:hover {{ color: {t.text}; border-color: {t.text}; }}
QPushButton#thinkToggle:checked {{ color: {t.signal}; background: {t.signal_soft}; border-color: {live_line}; }}
QPushButton#send {{
    border: none; border-radius: 14px; background: {t.accent}; color: {t.on_accent};
    font-size: 15px; font-weight: 700; padding: 0;
}}
QPushButton#send:hover {{ background: {t.accent_hover}; }}
QPushButton#send:disabled {{ background: {t.fill2}; color: {t.faint}; }}

QMenu {{
    background: {t.inset}; color: {t.text};
    border: 1px solid {t.border_strong}; border-radius: 8px; padding: 4px;
    font-family: {sans}; font-size: 13px;
}}
QMenu::item {{ padding: 5px 16px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t.hover}; color: {t.signal}; }}
QMenu::item:disabled {{ color: {t.faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

/* shell: navigation rail + placeholder workspaces */
#rail {{ background: {t.side}; border-right: 1px solid {t.tint(0.05)}; }}
#railHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#placeholder {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 14px; }}
#placeholderTitle {{ color: {t.text}; font-family: {sans}; font-size: 22px; font-weight: 600; }}
#placeholderLead {{ color: {t.muted}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#placeholderBadge {{
    color: {t.faint}; font-family: {sans}; font-size: 11px; border: 1px dashed {t.btn_line};
    border-radius: 8px; padding: 1px 8px;
}}
#placeholderText {{ color: {t.text}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#placeholderWhen {{ color: {t.signal_text}; font-family: {sans}; font-size: 12px; }}

/* workspaces: Tasks + Home */
#wsTitle {{ color: {t.text}; font-family: {sans}; font-size: 22px; font-weight: 600; }}
#wsSub {{ color: {t.faint}; font-family: {sans}; font-size: 12px; }}
#homeAttention {{ color: {t.muted}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#taskSection {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 10px; }}
#sectionHead {{ color: {t.muted}; font-family: {sans}; font-size: 12px; }}
#sectionHead[group="overdue"] {{ color: {t.red}; }}
#sectionCount {{ color: {t.faint}; font-family: {sans}; font-size: 12px; }}
#taskRow {{ background: transparent; border: 1px solid transparent; border-radius: 8px; }}
#taskRow:hover {{ background: {t.hover}; }}
#taskRow[selected="true"] {{
    background: {mix(t.bg, t.signal, 0.08)}; border-color: {mix(t.bg, t.signal, 0.35)};
}}
#taskTitle {{ color: {t.text}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#taskTitle[done="true"] {{ color: {t.faint}; text-decoration: line-through; }}
#taskMeta {{ color: {t.muted}; font-family: {sans}; font-size: 12px; }}
#taskBody {{ color: {t.faint}; font-family: {sans}; font-size: 12px; font-weight: 400; }}
#taskPrio {{ color: {t.signal}; font-family: {mono}; font-size: 12px; font-weight: 700; }}
QPushButton#rowAction {{
    border: none; color: {t.faint}; font-size: 15px; padding: 0 6px; background: transparent;
}}
QPushButton#rowAction:hover {{ color: {t.red}; }}
QPushButton#filterChip {{
    border: 1px solid {t.border_strong}; border-radius: 11px; padding: 2px 10px;
    font-family: {sans}; font-size: 12px; color: {t.muted}; background: transparent;
}}
QPushButton#filterChip:hover {{ color: {t.text}; border-color: {t.btn_line}; }}
QPushButton#filterChip:checked {{
    color: {t.signal}; border-color: {live_line}; background: {t.signal_soft};
}}
QPushButton#linkBtn {{
    border: none; color: {t.signal_text}; font-family: {sans}; font-size: 12px;
    font-weight: 500; padding: 1px 3px; background: transparent; border-radius: 4px;
}}
QPushButton#linkBtn:hover {{ color: {t.text}; background: {t.hover}; }}
QPushButton#linkBtn:pressed {{ background: {t.fill2}; }}
QComboBox {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 3px 8px; font-family: {sans}; font-size: 12px;
}}
QComboBox:hover {{ border-color: {t.btn_line}; }}
QComboBox::drop-down {{ border: none; width: 16px; }}
QComboBox QAbstractItemView {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    selection-background-color: {t.hover}; selection-color: {t.signal}; outline: none;
}}
QLineEdit#field, QPlainTextEdit#fieldBody {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 5px 8px; font-family: {sans}; font-size: 13px;
    selection-background-color: {sel};
}}
QLineEdit#field:focus, QPlainTextEdit#fieldBody:focus {{ border-color: {live_line}; }}
#fieldLabel {{ color: {t.faint}; font-family: {sans}; font-size: 11px; }}
#fieldError {{ color: {t.red}; font-family: {sans}; font-size: 12px; font-weight: 400; }}
#undoBar {{ background: {t.fill2}; border: 1px solid {t.border_strong}; border-radius: 8px; }}
#undoText {{ color: {t.text}; font-family: {sans}; font-size: 13px; font-weight: 400; }}
#emptyState {{ background: {t.panel}; border: 1px dashed {t.border_strong}; border-radius: 10px; }}
#emptyTitleSm {{ color: {t.text}; font-family: {sans}; font-size: 15px; font-weight: 600; }}
#homeHeadline {{ color: {t.text}; font-family: {sans}; font-size: 15px; font-weight: 600; }}
#panel[emphasis="true"] {{ background: {t.tint(0.07)}; border-color: {t.tint(0.08)}; }}
#homeGroup {{ color: {t.faint}; padding: 6px 2px 2px 2px; }}
#homeGroup[level="late"] {{ color: {t.red}; }}
#projName {{ color: {t.text}; }}
#projNext {{ color: {t.muted}; }}
#statNum {{ color: {t.text}; }}
#statNum[tone="good"] {{ color: {t.signal}; }}
#statNum[tone="bad"] {{ color: {t.red}; }}
#deadlineWhen {{ color: {t.muted}; }}
#deadlineTitle {{ color: {t.text}; font-family: {sans}; font-size: 13px; font-weight: 400; }}
#deadlineTag {{ color: {t.faint}; }}
#deadlineTag[kind="milestone"] {{ color: {t.signal_text}; }}
#stepNum {{ color: {t.signal}; font-family: {sans}; font-size: 13px; font-weight: 600; }}
#stepText {{ color: {t.text}; font-family: {sans}; font-size: 13px; font-weight: 400; }}
#stepReason {{ color: {t.faint}; font-family: {sans}; font-size: 12px; font-weight: 400; }}
#homeEmpty {{ color: {t.muted}; font-family: {sans}; font-size: 13px; font-weight: 400; }}
#homeEmptyHint {{ color: {t.faint}; font-family: {sans}; font-size: 12px; font-weight: 400; }}

/* quick capture (Ctrl+N) */
#capture {{
    background: {mix(t.bg, t.inset, 0.75)}; border: 1px solid {t.border_strong};
    border-radius: 14px;
}}
#captureHead {{ color: {t.signal}; font-family: {sans}; font-size: 12px; }}
#captureHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#captureOriginal {{ color: {t.muted}; font-family: {sans}; font-size: 12px; font-style: italic; }}
#captureQuestion {{ color: {t.text}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#captureEdit {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 8px; padding: 8px 10px; font-family: {sans}; font-size: 14px;
    selection-background-color: {sel};
}}
#captureEdit:focus {{ border-color: {live_line}; }}
#captureStatus {{ color: {t.muted}; font-family: {sans}; font-size: 12px; }}
#captureStatus[level="ok"] {{ color: {t.signal}; }}
#captureStatus[level="error"] {{ color: {t.red}; }}

/* chat panel */
#chat {{ background: {t.side}; border-left: 1px solid {t.tint(0.05)}; }}
#chatTitle {{ color: {t.text}; }}
#chatSub {{ color: {t.faint}; }}
#emptyTitle {{ color: {t.text}; }}
#emptyHint {{ color: {t.muted}; font-size: 13px; font-weight: 400; }}
#userBubble {{ background: {t.fill2}; border-radius: 10px; }}
#userText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botText {{ color: {t.text}; font-size: {BODY_PX}px; font-weight: 400; }}
#botName {{ color: {t.signal}; }}
#botMeta {{ color: {t.faint}; }}
#thinking {{ color: {t.muted}; font-family: {sans}; font-size: 12px; }}
#botNote {{ color: {t.unsure}; font-size: 12px; font-style: italic; }}
#botError {{ color: {t.muted}; font-size: 13px; font-style: italic; }}
QTextBrowser#userText, QTextBrowser#botText, QTextBrowser#botNote, QTextBrowser#botError {{
    background: transparent; border: none; padding: 0; selection-background-color: {sel};
}}
#composer {{ background: {t.inset}; border: 1px solid {t.border_strong}; border-radius: 10px; }}
#composer[focus="true"] {{ border-color: {live_line}; }}
#composerEdit {{
    background: transparent; border: none; color: {t.text}; font-size: {BODY_PX}px;
    selection-background-color: {sel};
}}
#composerHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#focusChip {{ background: {t.fill2}; border-radius: 8px; }}
QPushButton#focusLabel {{
    border: none; background: transparent; color: {t.text}; text-align: left; padding: 2px 0;
    font-family: {sans}; font-size: 12px; font-weight: 500;
}}
QPushButton#focusLabel:hover {{ color: {t.signal_text}; }}
#voiceCaption {{ color: {t.muted}; font-family: {sans}; font-size: 12px; }}
#voiceNote {{ color: {t.faint}; font-size: 12px; font-style: italic; }}
#voiceNote[level="error"] {{
    color: {t.muted}; font-style: normal; background: {t.fill}; border-radius: 8px;
    padding: 8px 10px;
}}

QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t.btn_line}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


def apply(app: QGuiApplication, scheme: str | None = None) -> Tokens:
    """Apply palette + stylesheet (``scheme`` is ignored: dark only)."""
    t = DARK
    load_fonts()
    app.setPalette(palette(t))
    if hasattr(app, "setStyleSheet"):
        sheet = stylesheet(t)
        if app.styleSheet() != sheet:  # re-applying restyles every live widget
            app.setStyleSheet(sheet)
    return t
