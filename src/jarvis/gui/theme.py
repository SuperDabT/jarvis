"""Colour tokens, fonts and the stylesheet: Jarvis's "project brain" look.

Dark only. A forest-green near-black base (#031211) with grey-green ink text
(#DCE8E2); panels are the base lifted a little. One teal mint (#52E3A4) marks
everything live: graph nodes with recent activity, status pills, sparklines,
the orb while it listens or speaks, new arrivals, focus. A muted red is kept
for genuinely failing checks only.

Type: JetBrains Mono for the cockpit (labels, numbers, the graph, the stream,
pills, the ticker); Adwaita Sans (Inter's design, every weight, installed
with the desktop) for reading (the briefing, chat answers).
Labels are lower case: the look is a terminal, not a poster.
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
    panel: str = "#061917"  # cockpit panels

    def tag(self, name: str) -> tuple[str, str, str]:
        return self.tags.get(name) or self.tags["open"]

    def tint(self, k: float) -> str:
        """The ink over the background at ``k`` (0..1)."""
        return mix(self.bg, self.ink, k)


# Forest-green near-black, a grey-green ink and a teal mint (the Life OS mockup).
BG = "#031211"
INK = "#dce8e2"
PEARL = INK  # the ink's old name, kept for older imports
MINT = "#52e3a4"
RED = "#e5787a"


def _dark() -> Tokens:
    t = lambda k: mix(BG, INK, k)  # noqa: E731 - a tint of the pair
    m = lambda k: mix(BG, MINT, k)  # noqa: E731 - towards the mint
    return Tokens(
        name="dark", bg=BG, ink=INK, side="#041614", text=INK, muted=t(0.72),
        faint=t(0.5), border=t(0.11), border_strong=t(0.18), btn_line=t(0.3),
        fill=t(0.045), fill2=t(0.1), inset="#061a18", hover=t(0.07), accent=INK,
        accent_hover="#f2fbf6", on_accent=BG, signal=MINT, signal_text=MINT,
        signal_soft=m(0.14), unsure=t(0.72), unsure_bg=t(0.1), warn=MINT, red=RED,
        green=MINT, amber=t(0.45), shadow="#010807",
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
        panel="#061917",
    )


DARK = _dark()
LIGHT = DARK  # dark only; kept so older imports keep working

BODY_PX = 14
SMALL_PX = 12
TITLE_PX = 22

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
#topBar {{ border-bottom: 1px solid {t.border}; }}
#ticker {{ border-top: 1px solid {t.border}; }}
#logo {{ color: {t.text}; }}
#crumb {{ color: {t.muted}; }}
#clock, #pulseText, #statusText, #tickerHint {{ color: {t.faint}; }}
#tickerText {{ color: {t.muted}; }}
#tickerText[level="warn"] {{ color: {t.red}; }}
#tickerText[level="ok"] {{ color: {t.signal}; }}
#pill {{
    border-radius: 9px; padding: 1px 8px; color: {t.muted};
    border: 1px solid {t.border_strong}; background: transparent;
}}
#pill[live="true"] {{ color: {t.signal}; border-color: {live_line}; background: {t.signal_soft}; }}
#pill[level="fail"] {{ color: {t.red}; border-color: {mix(t.bg, t.red, 0.5)}; }}

/* panels */
#panel {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 8px; }}
#panel[live="true"] {{ border-color: {mix(t.bg, t.signal, 0.45)}; }}
#panelHead {{ color: {t.muted}; }}
#panelHint {{ color: {t.faint}; }}
#vKey {{ color: {t.faint}; }}
#vVal {{ color: {t.text}; }}
#vVal[live="true"] {{ color: {t.signal}; }}
#vVal[level="fail"] {{ color: {t.red}; }}
#vNote {{ color: {t.faint}; }}
#hairline {{ background: {t.border}; border: none; }}

/* briefing HUD */
#hud {{
    background: {mix(t.bg, t.inset, 0.6)}; border: 1px solid {t.border_strong};
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
#inspKind {{ color: {t.signal}; }}
#promptBox {{
    background: {t.inset}; color: {t.muted}; border: 1px solid {t.border};
    border-radius: 8px; padding: 8px 10px; font-size: 11px;
    selection-background-color: {sel};
}}
#streamText {{ color: {t.text}; }}
#streamMeta {{ color: {t.faint}; }}
#tag {{ border-radius: 7px; padding: 0px 7px; font-size: 11px; font-family: {mono}; }}

QPushButton {{
    color: {t.text}; background: transparent; border: 1px solid {t.btn_line};
    border-radius: 6px; padding: 4px 10px; font-family: {mono}; font-size: 12px;
}}
QPushButton:hover {{ border-color: {t.text}; }}
QPushButton:pressed {{ background: {t.hover}; }}
QPushButton:disabled {{ color: {t.faint}; border-color: {t.border_strong}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
QPushButton#ghost {{ border: none; color: {t.muted}; padding: 4px 8px; }}
QPushButton#ghost:hover {{ background: {t.hover}; color: {t.text}; }}
QPushButton#ghost[active="true"] {{ background: {t.hover}; color: {t.signal}; }}
QPushButton#primary {{ color: {t.on_accent}; background: {t.accent}; border: 1px solid {t.accent}; }}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary[flash="true"] {{ color: {t.signal}; background: transparent; border-color: {t.signal}; }}
QPushButton#filter {{ border: none; color: {t.faint}; padding: 2px 5px; font-size: 11px; }}
QPushButton#filter:checked {{ color: {t.text}; }}
QPushButton#filter:hover {{ color: {t.signal}; }}
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
    font-family: {mono}; font-size: 11px; color: {t.muted}; background: transparent;
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
    font-family: {mono}; font-size: 12px;
}}
QMenu::item {{ padding: 5px 16px; border-radius: 5px; }}
QMenu::item:selected {{ background: {t.hover}; color: {t.signal}; }}
QMenu::item:disabled {{ color: {t.faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

/* shell: navigation rail + placeholder workspaces */
#rail {{ background: {t.side}; border-right: 1px solid {t.border}; }}
#railHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#placeholder {{ background: {t.panel}; border: 1px solid {t.border}; border-radius: 14px; }}
#placeholderTitle {{ color: {t.text}; font-family: {sans}; font-size: 22px; font-weight: 600; }}
#placeholderLead {{ color: {t.muted}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#placeholderBadge {{
    color: {t.faint}; font-family: {mono}; font-size: 10px; border: 1px dashed {t.btn_line};
    border-radius: 8px; padding: 1px 8px;
}}
#placeholderText {{ color: {t.text}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#placeholderWhen {{ color: {t.signal_text}; font-family: {mono}; font-size: 11px; }}

/* quick capture (Ctrl+N) */
#capture {{
    background: {mix(t.bg, t.inset, 0.6)}; border: 1px solid {live_line}; border-radius: 12px;
}}
#captureHead {{ color: {t.signal}; font-family: {mono}; font-size: 11px; }}
#captureHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#captureOriginal {{ color: {t.muted}; font-family: {sans}; font-size: 12px; font-style: italic; }}
#captureQuestion {{ color: {t.text}; font-family: {sans}; font-size: 14px; font-weight: 400; }}
#captureEdit {{
    background: {t.inset}; color: {t.text}; border: 1px solid {t.border_strong};
    border-radius: 8px; padding: 8px 10px; font-family: {sans}; font-size: 14px;
    selection-background-color: {sel};
}}
#captureEdit:focus {{ border-color: {live_line}; }}
#captureStatus {{ color: {t.muted}; font-family: {mono}; font-size: 12px; }}
#captureStatus[level="ok"] {{ color: {t.signal}; }}
#captureStatus[level="error"] {{ color: {t.red}; }}

/* chat panel */
#chat {{ background: {t.side}; border-left: 1px solid {t.border}; }}
#chatTitle {{ color: {t.text}; }}
#chatSub {{ color: {t.faint}; }}
#emptyTitle {{ color: {t.text}; }}
#emptyHint {{ color: {t.muted}; font-size: 13px; font-weight: 400; }}
#userBubble {{ background: {t.fill2}; border-radius: 10px; }}
#userText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botText {{ color: {t.text}; font-size: {BODY_PX}px; font-weight: 400; }}
#botName {{ color: {t.signal}; }}
#botMeta {{ color: {t.faint}; }}
#thinking {{ color: {t.muted}; font-family: {mono}; font-size: 12px; }}
#botNote {{ color: {t.unsure}; font-size: 12px; font-style: italic; }}
#botError {{ color: {t.muted}; font-size: 13px; font-style: italic; }}
#composer {{ background: {t.inset}; border: 1px solid {t.border_strong}; border-radius: 10px; }}
#composer[focus="true"] {{ border-color: {live_line}; }}
#composerEdit {{
    background: transparent; border: none; color: {t.text}; font-size: {BODY_PX}px;
    selection-background-color: {sel};
}}
#composerHint {{ color: {t.faint}; font-family: {mono}; font-size: 10px; }}
#voiceCaption {{ color: {t.muted}; font-family: {mono}; font-size: 11px; }}
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
        app.setStyleSheet(stylesheet(t))
    return t
