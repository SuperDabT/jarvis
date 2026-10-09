"""Colour tokens for light and dark, and the one stylesheet built from them.

One accent colour, used sparingly (the primary button, focus, links on hover).
Everything else is ink, muted ink, surfaces and hairline borders.
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette


@dataclass(frozen=True)
class Tokens:
    name: str
    bg: str  # window background
    surface: str  # cards, briefing panel
    surface_alt: str  # hovered ghost buttons, pills
    text: str
    muted: str
    faint: str  # least important text (evidence links, hints)
    border: str
    accent: str
    accent_hover: str
    on_accent: str
    unsure: str  # left rule + pill text for unconfirmed sentences
    unsure_bg: str
    warn: str  # status-line problems: calm, not alarm red


LIGHT = Tokens(
    name="light",
    bg="#f6f5f2",
    surface="#ffffff",
    surface_alt="#efeee9",
    text="#1d1f23",
    muted="#5d626b",
    faint="#8a8f98",
    border="#e3e1db",
    accent="#2f6db5",
    accent_hover="#255a96",
    on_accent="#ffffff",
    unsure="#a8792a",
    unsure_bg="#f7efdf",
    warn="#9a5b1e",
)

DARK = Tokens(
    name="dark",
    bg="#141518",
    surface="#1c1e22",
    surface_alt="#26292e",
    text="#e7e6e2",
    muted="#a3a7ae",
    faint="#757a83",
    border="#2c2f35",
    accent="#7eaee6",
    accent_hover="#9cc2ee",
    on_accent="#0e1622",
    unsure="#d4a95e",
    unsure_bg="#2d2719",
    warn="#e0a56a",
)

BODY_PX = 16
SMALL_PX = 13
PREFERRED_FAMILIES = ("Inter", "Adwaita Sans", "Cantarell", "Noto Sans", "IBM Plex Sans")


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


def pick_font() -> QFont:
    """The system UI font, unless it is a fallback ("Sans Serif") and a nicer one exists."""
    from PySide6.QtGui import QFontDatabase

    font = QFont(QGuiApplication.font())
    available = set(QFontDatabase.families())
    if font.family() not in PREFERRED_FAMILIES or font.family() not in available:
        for fam in PREFERRED_FAMILIES:
            if fam in available:
                font.setFamily(fam)
                break
    font.setPixelSize(BODY_PX)
    font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
    return font


def palette(t: Tokens) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t.bg,
        QPalette.ColorRole.WindowText: t.text,
        QPalette.ColorRole.Base: t.surface,
        QPalette.ColorRole.AlternateBase: t.surface_alt,
        QPalette.ColorRole.Text: t.text,
        QPalette.ColorRole.Button: t.surface,
        QPalette.ColorRole.ButtonText: t.text,
        QPalette.ColorRole.Highlight: t.accent,
        QPalette.ColorRole.HighlightedText: t.on_accent,
        QPalette.ColorRole.Link: t.muted,
        QPalette.ColorRole.LinkVisited: t.muted,
        QPalette.ColorRole.ToolTipBase: t.surface,
        QPalette.ColorRole.ToolTipText: t.text,
        QPalette.ColorRole.PlaceholderText: t.faint,
    }
    for role, color in roles.items():
        p.setColor(role, QColor(color))
    return p


def stylesheet(t: Tokens) -> str:
    return f"""
* {{ outline: none; }}
QMainWindow, #page {{ background: {t.bg}; }}
QScrollArea, QScrollArea > QWidget > QWidget {{ background: {t.bg}; border: none; }}
QLabel {{ color: {t.text}; background: transparent; }}
QToolTip {{
    background: {t.surface}; color: {t.text}; border: 1px solid {t.border};
    padding: 6px 8px; font-size: {SMALL_PX}px;
}}

#appTitle {{ font-size: 22px; font-weight: 700; }}
#repoName {{ color: {t.muted}; font-size: 15px; }}
#meta {{ color: {t.faint}; font-size: {SMALL_PX}px; }}

#briefingPanel {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 14px;
}}
#headline {{ font-size: 23px; font-weight: 600; }}
#sentence {{ font-size: {BODY_PX}px; color: {t.text}; }}
#ruleUnsure {{ background: {t.unsure}; border: none; border-radius: 1px; }}
#ruleNone {{ background: transparent; border: none; }}
#evidence {{ color: {t.faint}; font-size: {SMALL_PX}px; }}
#pill {{
    color: {t.unsure}; background: {t.unsure_bg}; border-radius: 6px;
    padding: 2px 8px; font-size: 12px; font-weight: 600;
}}
#pillNew {{
    color: {t.accent}; background: {t.surface_alt}; border-radius: 6px;
    padding: 2px 8px; font-size: 12px; font-weight: 600;
}}
#emptyText {{ color: {t.muted}; font-size: 15px; }}

#sectionTitle {{ font-size: 18px; font-weight: 600; }}
#sectionCount {{ color: {t.faint}; font-size: 15px; }}
#groupTitle {{
    color: {t.muted}; font-size: 12px; font-weight: 700; letter-spacing: 1px;
}}

#card {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 12px;
}}
#cardTitle {{ font-size: 16px; font-weight: 600; }}
#cardWhy {{ color: {t.muted}; font-size: 15px; }}
#cardSource {{ color: {t.faint}; font-size: {SMALL_PX}px; }}

QPushButton {{
    color: {t.text}; background: transparent; border: 1px solid {t.border};
    border-radius: 8px; padding: 6px 12px; font-size: 14px;
}}
QPushButton:hover {{ background: {t.surface_alt}; }}
QPushButton:pressed {{ background: {t.border}; }}
QPushButton:disabled {{ color: {t.faint}; }}
QPushButton:focus {{ border-color: {t.accent}; }}
QPushButton::menu-indicator {{ width: 0; image: none; }}
QPushButton#primary {{
    color: {t.on_accent}; background: {t.accent}; border: 1px solid {t.accent};
    font-weight: 600;
}}
QPushButton#primary:hover {{ background: {t.accent_hover}; border-color: {t.accent_hover}; }}
QPushButton#primary[flash="true"] {{
    color: {t.accent}; background: {t.surface_alt}; border-color: {t.border};
}}
QPushButton#refresh {{ padding: 6px 14px; }}

QMenu {{
    background: {t.surface}; color: {t.text}; border: 1px solid {t.border};
    border-radius: 8px; padding: 4px;
}}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; font-size: 14px; }}
QMenu::item:selected {{ background: {t.surface_alt}; }}
QMenu::item:disabled {{ color: {t.faint}; }}
QMenu::separator {{ height: 1px; background: {t.border}; margin: 4px 6px; }}

#statusBar {{ background: {t.bg}; border-top: 1px solid {t.border}; }}
#status {{ color: {t.faint}; font-size: {SMALL_PX}px; }}
#status[level="warn"] {{ color: {t.warn}; }}
#status[level="ok"] {{ color: {t.muted}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {t.faint}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}
"""


def apply(app: QGuiApplication, scheme: str | None = None) -> Tokens:
    """Apply palette + stylesheet for ``scheme`` (None = follow the system)."""
    t = tokens_for(scheme)
    app.setPalette(palette(t))
    if hasattr(app, "setStyleSheet"):
        app.setStyleSheet(stylesheet(t))
    return t
