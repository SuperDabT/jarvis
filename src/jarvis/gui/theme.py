"""Colour tokens (Notion-inspired, light and dark) and the stylesheet built from them.

The page is white (or near-black), text is warm ink, structure comes from soft
fills and hairline borders rather than boxes. Colour is reserved for meaning:
Notion's tag colours for types and statuses, a green ramp for activity, a fixed
categorical order for chart series, and one accent blue for actions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette

TAG_NAMES = ("gray", "brown", "orange", "yellow", "green", "blue", "purple", "pink", "red")


@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str  # page background
    side: str  # chat panel / peek background
    text: str
    muted: str  # secondary text
    faint: str  # tertiary text (axis labels, hints)
    border: str  # hairlines
    border_strong: str  # inputs, buttons
    fill: str  # callout, KPI tiles
    fill2: str  # chart tracks, hovered rows
    hover: str  # hovered ghost buttons
    accent: str
    accent_hover: str
    on_accent: str
    unsure: str  # amber text: "unconfirmed", notes
    unsure_bg: str
    warn: str  # status-line problems
    red: str  # today line, violations
    green: str  # progress, "watching" dot
    amber: str  # "may be done" hatch
    shadow: str
    grid: tuple[str, str, str, str, str]  # contribution grid levels 0..4
    series: tuple[str, ...]  # categorical chart order (validated for CVD)
    tags: dict[str, tuple[str, str]] = field(default_factory=dict)  # name -> (bg, fg)
    orb_core: str = "#3d9df0"  # the arc-reactor orb's core (cyan in dark)
    orb_ring: str = "#2383e2"  # its rings (the accent)

    def tag(self, name: str) -> tuple[str, str]:
        return self.tags.get(name) or self.tags["gray"]


LIGHT = Tokens(
    name="light",
    bg="#ffffff",
    side="#fbfbfa",
    text="#37352f",
    muted="#787774",
    faint="#a3a29e",
    border="#ededec",
    border_strong="#dfdedb",
    fill="#f7f6f3",
    fill2="#f1f1ef",
    hover="#efefed",
    accent="#2383e2",
    accent_hover="#0b6fcf",
    on_accent="#ffffff",
    unsure="#b5761a",
    unsure_bg="#fbf3db",
    warn="#b5651d",
    red="#e03e3e",
    green="#2f9e6e",
    amber="#d9a440",
    shadow="#0f0f0f",
    grid=("#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39"),
    series=("#2383e2", "#e07b2a", "#8f5fc2", "#2f9e6e"),
    tags={
        "gray": ("#e3e2e0", "#32302c"),
        "brown": ("#eee0da", "#442a1e"),
        "orange": ("#fadec9", "#49290e"),
        "yellow": ("#fdecc8", "#402c1b"),
        "green": ("#dbeddb", "#1c3829"),
        "blue": ("#d3e5ef", "#183347"),
        "purple": ("#e8deee", "#412454"),
        "pink": ("#f5e0e9", "#4c2337"),
        "red": ("#ffe2dd", "#5d1715"),
    },
    orb_core="#3d9df0",
    orb_ring="#2383e2",
)

DARK = Tokens(
    name="dark",
    bg="#191919",
    side="#202020",
    text="#d4d4d4",
    muted="#9b9b9b",
    faint="#6f6f6f",
    border="#2a2a2a",
    border_strong="#3a3a3a",
    fill="#252525",
    fill2="#2f2f2f",
    hover="#2c2c2c",
    accent="#2383e2",
    accent_hover="#3b93ea",
    on_accent="#ffffff",
    unsure="#d9a54f",
    unsure_bg="#3a301c",
    warn="#e0a56a",
    red="#ff6b62",
    green="#4fae84",
    amber="#c9963d",
    shadow="#000000",
    grid=("#2a2a2a", "#0e4429", "#006d32", "#26a641", "#39d353"),
    series=("#3d8fe0", "#cf7430", "#9469cc", "#36996f"),
    tags={
        "gray": ("#373737", "#d4d4d4"),
        "brown": ("#4a3228", "#e3cfc6"),
        "orange": ("#5c3b23", "#f0d2b8"),
        "yellow": ("#564328", "#f2deb0"),
        "green": ("#243d30", "#c3e3cf"),
        "blue": ("#143a4e", "#bcdcef"),
        "purple": ("#3c2d49", "#dccbee"),
        "pink": ("#4e2c3c", "#f0c9db"),
        "red": ("#522e2a", "#f6c7c1"),
    },
    orb_core="#7ee8ff",
    orb_ring="#3d8fe0",
)

BODY_PX = 14
SMALL_PX = 12
TITLE_PX = 32
PREFERRED_FAMILIES = ("Inter", "Inter Variable", "Adwaita Sans", "Cantarell", "Noto Sans",
                      "IBM Plex Sans")
MONO_FAMILIES = ("JetBrains Mono", "JetBrainsMono Nerd Font", "Adwaita Mono", "iA Writer Mono S",
                 "DejaVu Sans Mono", "Liberation Mono")


def system_scheme() -> str:
    """"dark" or "light" from Qt 6.5+ style hints; light when unknown."""
    app = QGuiApplication.instance()
    if app is None:
        return "light"
    try:
        scheme = app.styleHints().colorScheme()
    except AttributeError:  # Qt < 6.5
        return "light"
    return "dark" if scheme == Qt.ColorScheme.Dark else "light"


def tokens_for(scheme: str | None = None) -> Tokens:
    return DARK if (scheme or system_scheme()) == "dark" else LIGHT


def _first_available(families: tuple[str, ...]) -> str | None:
    from PySide6.QtGui import QFontDatabase

    available = set(QFontDatabase.families())
    return next((f for f in families if f in available), None)


def pick_font() -> QFont:
    """Inter if installed, else Adwaita Sans (Inter-based), else the system UI font."""
    font = QFont(QGuiApplication.font())
    fam = _first_available(PREFERRED_FAMILIES)
    if fam:
        font.setFamily(fam)
    font.setPixelSize(BODY_PX)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def mono_family() -> str:
    return _first_available(MONO_FAMILIES) or "monospace"


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
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.on_accent,
        QPalette.ColorRole.Link: t.muted,
        QPalette.ColorRole.LinkVisited: t.muted,
        QPalette.ColorRole.ToolTipBase: t.text if t.name == "light" else t.fill2,
        QPalette.ColorRole.ToolTipText: t.bg if t.name == "light" else t.text,
        QPalette.ColorRole.PlaceholderText: t.faint,
        QPalette.ColorRole.Mid: t.border,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p


def stylesheet(t: Tokens) -> str:
    tip_bg, tip_fg = (t.text, t.bg) if t.name == "light" else (t.fill2, t.text)
    yb, yf = t.tag("yellow")
    return f"""
* {{ outline: none; }}
QMainWindow, #page, #dash {{ background: {t.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; border: none; }}
QLabel {{ color: {t.text}; background: transparent; }}
QToolTip {{
    background: {tip_bg}; color: {tip_fg}; border: none; border-radius: 6px;
    padding: 5px 8px; font-size: {SMALL_PX}px;
}}

/* top bar */
#topBar {{ background: {t.bg}; border-bottom: 1px solid {t.border}; }}
#crumb {{ color: {t.muted}; font-size: 13px; }}
#crumbStrong {{ color: {t.text}; font-size: 13px; }}

/* page head */
#pageTitle {{ font-size: {TITLE_PX}px; font-weight: 700; color: {t.text}; }}
#meta, #metaDot {{ color: {t.muted}; font-size: 13px; }}
#metaSep {{ color: {t.faint}; font-size: 13px; }}

/* section headers */
#sectionHead {{
    color: {t.muted}; font-size: 12px; font-weight: 600; letter-spacing: 0.6px;
}}
#sectionHint {{ color: {t.faint}; font-size: 12px; }}
#hairline {{ background: {t.border}; border: none; }}

/* briefing callout */
#callout {{ background: {t.fill}; border-radius: 6px; }}
#calloutIcon {{ font-size: 20px; }}
#headline {{ font-size: 16px; font-weight: 600; color: {t.text}; }}
#sentence {{ font-size: {BODY_PX + 1}px; color: {t.text}; }}
#bullet {{ color: {t.text}; font-size: {BODY_PX + 1}px; }}
#evidence {{ color: {t.faint}; font-size: {SMALL_PX}px; }}
#pill {{
    color: {t.unsure}; background: {t.unsure_bg}; border-radius: 4px;
    padding: 1px 6px; font-size: 11px; font-weight: 600;
}}
#emptyText {{ color: {t.muted}; font-size: {BODY_PX}px; }}
#calloutFoot {{ color: {t.faint}; font-size: {SMALL_PX}px; }}

/* KPI tiles */
#kpi {{ background: {t.fill}; border-radius: 6px; }}
#kpi:hover {{ background: {t.fill2}; }}
#kpiLabel {{ color: {t.muted}; font-size: 12px; }}
#kpiValue {{ color: {t.text}; font-size: 26px; font-weight: 600; }}
#kpiValue[tone="bad"] {{ color: {t.red}; }}
#kpiValue[tone="warn"] {{ color: {t.warn}; }}
#kpiValue[tone="good"] {{ color: {t.green}; }}
#kpiValue[tone="muted"] {{ color: {t.faint}; }}
#kpiSub {{ color: {t.muted}; font-size: 12px; }}
#kpiSub[unsure="true"] {{ color: {t.unsure}; }}

/* chart blocks */
#caption {{ color: {t.muted}; font-size: 12px; }}
#railHead {{ color: {t.text}; font-size: 13px; font-weight: 600; }}
#railSub {{ color: {t.faint}; font-size: 12px; }}

/* suggestions table */
#table {{ background: transparent; }}
#tableHead {{ border-top: 1px solid {t.border}; border-bottom: 1px solid {t.border}; }}
#colHead {{ color: {t.muted}; font-size: 12px; }}
#row {{ border-bottom: 1px solid {t.border}; background: transparent; }}
/* row hover / selected tint is painted by TableRow (animated) */
#rowTitle {{ color: {t.text}; font-size: {BODY_PX}px; }}
#rowWhy {{ color: {t.faint}; font-size: 12px; }}
#tag {{ border-radius: 3px; padding: 1px 6px; font-size: 12px; }}

/* buttons */
QPushButton {{
    color: {t.text}; background: transparent; border: 1px solid {t.border_strong};
    border-radius: 6px; padding: 5px 10px; font-size: 13px;
}}
QPushButton:hover {{ background: {t.hover}; }}
QPushButton:pressed {{ background: {t.fill2}; }}
QPushButton:disabled {{ color: {t.faint}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
QPushButton#ghost {{ border: none; color: {t.muted}; padding: 5px 9px; }}
QPushButton#ghost:hover {{ background: {t.hover}; color: {t.text}; }}
QPushButton#ghost[active="true"] {{ background: {t.fill2}; color: {t.text}; }}
QPushButton#primary {{
    color: {t.on_accent}; background: {t.accent}; border: 1px solid {t.accent};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary[flash="true"] {{
    color: {t.green}; background: {t.fill2}; border-color: {t.border_strong};
}}
QPushButton#rowCopy {{
    border: 1px solid {t.border_strong}; color: {t.muted}; padding: 2px 8px; font-size: 12px;
    background: {t.bg};
}}
QPushButton#rowCopy:hover {{ color: {t.text}; background: {t.hover}; }}
QPushButton#rowCopy[flash="true"] {{ color: {t.green}; }}
QPushButton#chip {{
    border: 1px solid {t.border_strong}; border-radius: 14px; padding: 6px 12px;
    color: {t.text}; background: {t.bg}; font-size: 13px; text-align: left;
}}
QPushButton#chip:hover {{ background: {t.hover}; }}
QPushButton#srcChip {{
    border: none; border-radius: 4px; padding: 1px 6px; font-size: 12px;
    color: {t.muted}; background: {t.fill2};
}}
QPushButton#srcChip:hover {{ color: {t.text}; background: {t.border_strong}; }}
QPushButton#send {{
    border: none; border-radius: 14px; background: {t.accent}; color: {t.on_accent};
    font-size: 15px; font-weight: 700; padding: 0;
}}
QPushButton#send:hover {{ background: {t.accent_hover}; }}
QPushButton#send:disabled {{ background: {t.fill2}; color: {t.faint}; }}

QMenu {{
    background: {t.bg if t.name == 'light' else t.fill}; color: {t.text};
    border: 1px solid {t.border_strong}; border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 4px; font-size: 13px; }}
QMenu::item:selected {{ background: {t.hover}; }}
QMenu::item:disabled {{ color: {t.faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

/* peek drawer */
#peek {{ background: {t.bg}; border-left: 1px solid {t.border_strong}; }}
#peekTitle {{ font-size: 22px; font-weight: 700; color: {t.text}; }}
#propKey {{ color: {t.muted}; font-size: 13px; }}
#propVal {{ color: {t.text}; font-size: 13px; }}
#peekWhy {{ color: {t.text}; font-size: {BODY_PX + 1}px; }}
#promptBox {{
    background: {t.fill}; color: {t.text}; border: none; border-radius: 6px;
    padding: 10px 12px; font-size: 12px; selection-background-color: {t.accent};
}}

/* chat panel */
#chat {{ background: {t.side}; border-left: 1px solid {t.border}; }}
#chatTitle {{ font-size: 14px; font-weight: 600; color: {t.text}; }}
#chatSub {{ color: {t.faint}; font-size: 12px; }}
#emptyTitle {{ font-size: 16px; font-weight: 600; color: {t.text}; }}
#emptyHint {{ color: {t.muted}; font-size: 13px; }}
#userBubble {{ background: {t.fill2}; border-radius: 10px; }}
#userText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botText {{ color: {t.text}; font-size: {BODY_PX}px; }}
#botName {{ color: {t.text}; font-size: 12px; font-weight: 600; }}
#botMeta {{ color: {t.faint}; font-size: 11px; }}
#thinking {{ color: {t.muted}; font-size: 13px; }}
#botNote {{ color: {t.unsure}; font-size: 12px; }}
#botError {{ color: {t.muted}; font-size: 13px; font-style: italic; }}
#composer {{
    background: {t.bg}; border: 1px solid {t.border_strong}; border-radius: 10px;
}}
#composer[focus="true"] {{ border-color: {t.accent}; }}
#composerEdit {{
    background: transparent; border: none; color: {t.text}; font-size: {BODY_PX}px;
    selection-background-color: {t.accent};
}}
#composerHint {{ color: {t.faint}; font-size: 11px; }}

#statusBar {{ background: {t.bg}; border-top: 1px solid {t.border}; }}
#status {{ color: {t.faint}; font-size: 12px; }}
#status[level="warn"] {{ color: {t.warn}; }}
#status[level="ok"] {{ color: {t.muted}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border_strong}; border-radius: 3px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t.faint}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
QPlainTextEdit QScrollBar:vertical {{ width: 8px; }}
/* tags sit on their own colour; set per widget */
#unsureTag {{ color: {yf}; background: {yb}; }}
"""


def apply(app: QGuiApplication, scheme: str | None = None) -> Tokens:
    """Apply palette + stylesheet for ``scheme`` (None = follow the system)."""
    t = tokens_for(scheme)
    app.setPalette(palette(t))
    if hasattr(app, "setStyleSheet"):
        app.setStyleSheet(stylesheet(t))
    return t
