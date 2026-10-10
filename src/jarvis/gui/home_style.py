"""Home's look: a navy palette and the style rules for its cards (Home only).

The rest of the app keeps the charcoal theme (gui/theme.py); Home sets this
stylesheet on its own page, so every rule here is scoped to Home's widgets.
Colour carries meaning only: status dots (red overdue, amber today or a
commitment, blue coming up, grey quiet), category pills (Study, Work,
Personal, Exam) and progress bars (green on track, amber at risk).
"""
from __future__ import annotations

from dataclasses import dataclass

from jarvis.gui.theme import mix


@dataclass(frozen=True)
class HomePalette:
    bg: str = "#0b1220"          # the page
    card: str = "#101a2c"        # cards
    card_hi: str = "#14203a"     # a card inside a card (top priority, recommendation)
    border: str = "#1c2a44"
    border_hi: str = "#26375a"
    text: str = "#e6ecf7"
    muted: str = "#94a1b8"
    faint: str = "#64718a"
    accent: str = "#5b9bff"      # links, the selected day, primary actions
    green: str = "#34d399"
    amber: str = "#f5b642"
    red: str = "#f2626b"
    blue: str = "#5b9bff"
    purple: str = "#a78bfa"
    grey: str = "#64718a"

    def tone(self, name: str) -> str:
        """A dot / text colour by meaning."""
        return {"red": self.red, "amber": self.amber, "blue": self.blue,
                "green": self.green, "good": self.green, "warn": self.amber,
                "bad": self.red, "purple": self.purple}.get(name, self.grey)

    def category(self, name: str) -> str:
        """A category pill's colour."""
        return {"Study": self.blue, "Work": self.purple, "Personal": self.green,
                "Exam": self.red, "Canvas": self.blue}.get(name, self.grey)

    def wash(self, color: str, k: float = 0.16) -> str:
        """``color`` faintly over a card (pill and badge backgrounds)."""
        return mix(self.card, color, k)


PALETTE = HomePalette()


def stylesheet(p: HomePalette = PALETTE) -> str:
    """The rules for Home's page; set on the page widget, so they apply to Home only."""
    return f"""
#homePage {{ background: {p.bg}; }}
#homePage QLabel {{ color: {p.text}; background: transparent; }}
#hCard {{ background: {p.card}; border: 1px solid {p.border}; border-radius: 12px; }}
#hInner {{ background: {p.card_hi}; border: 1px solid {p.border_hi}; border-radius: 10px; }}
#hTitle {{ color: {p.text}; font-weight: 600; }}
#hMuted, #hSub {{ color: {p.muted}; }}
#hFaint {{ color: {p.faint}; }}
#hBadge {{ color: {p.muted}; background: {p.wash(p.muted, 0.12)}; border-radius: 9px;
          padding: 0px 7px; }}
#hLink {{ color: {p.accent}; background: transparent; border: none; padding: 0; }}
#hLink:hover {{ color: {mix(p.accent, '#ffffff', 0.3)}; }}
#hButton {{ color: {p.text}; background: transparent; border: 1px solid {p.border_hi};
           border-radius: 7px; padding: 5px 12px; }}
#hButton:hover {{ background: {p.wash(p.accent, 0.14)}; border-color: {p.accent}; }}
#hAction {{ color: {p.text}; background: {p.card_hi}; border: 1px solid {p.border};
           border-radius: 10px; padding: 0px; text-align: left; }}
#hAction:hover {{ border-color: {p.accent}; background: {p.wash(p.accent, 0.1)}; }}
#hRow {{ background: transparent; border: none; border-radius: 8px; text-align: left; }}
#hRow:hover {{ background: {p.wash(p.accent, 0.08)}; }}
#hCheck {{ background: transparent; border: 1.5px solid {p.faint}; border-radius: 8px; }}
#hCheck:hover {{ border-color: {p.green}; background: {p.wash(p.green, 0.2)}; }}
#hDayBtn {{ color: {p.muted}; background: transparent; border: 1px solid {p.border};
           border-radius: 6px; padding: 0px; }}
#hDayBtn:hover {{ color: {p.text}; border-color: {p.accent}; }}
#hDayBtn:disabled {{ color: {p.border_hi}; border-color: {p.border}; }}
#hEmpty {{ color: {p.faint}; }}
#hError {{ color: {p.red}; }}
#hSep {{ background: {p.border}; }}
#homePage QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
#homePage QScrollBar::handle:vertical {{ background: {p.border_hi}; border-radius: 3px;
                                          min-height: 30px; }}
#homePage QScrollBar::add-line, #homePage QScrollBar::sub-line {{ height: 0; }}
#homePage QScrollBar::add-page, #homePage QScrollBar::sub-page {{ background: transparent; }}
"""
