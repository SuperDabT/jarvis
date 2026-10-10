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


def tasks_stylesheet(p: HomePalette = PALETTE) -> str:
    """Tasks' rules (the board, its cards, the editor, the move and create
    dialogs), on top of Home's: the same palette, scoped to the Tasks page and
    its dialogs."""
    field = (f"background: {p.card_hi}; color: {p.text}; border: 1px solid {p.border_hi}; "
             f"border-radius: 7px; padding: 6px 9px; selection-background-color: "
             f"{p.wash(p.accent, 0.5)};")
    return stylesheet(p).replace("#homePage", "#tasksPage") + f"""
#tasksPage, #tkDialog {{ background: {p.bg}; }}
#tkDialog {{ background: {p.card}; }}
#tkDialog QLabel {{ color: {p.text}; background: transparent; }}
#tkColumn {{ background: {p.card}; border: 1px solid {p.border}; border-radius: 12px; }}
#tkColumn[drop="true"] {{ border: 1px solid {p.accent}; background: {p.wash(p.accent, 0.08)}; }}
#tkColumnBody {{ background: transparent; }}
#tkCard {{ background: {p.card_hi}; border: 1px solid {p.border}; border-radius: 9px; }}
#tkCard:hover {{ border-color: {p.border_hi}; background: {mix(p.card_hi, '#ffffff', 0.03)}; }}
#tkCard[selected="true"] {{ border: 1px solid {p.accent}; }}
#tkTitle[done="true"] {{ color: {p.faint}; text-decoration: line-through; }}
#tkChip {{ color: {p.muted}; background: transparent; border: 1px solid {p.border_hi};
          border-radius: 5px; padding: 0px 5px; }}
#tkAction {{ color: {p.faint}; background: transparent; border: none; padding: 0 2px; }}
#tkAction:hover {{ color: {p.red}; }}
#tkPlus {{ color: {p.muted}; background: transparent; border: 1px solid {p.border};
          border-radius: 6px; padding: 0; }}
#tkPlus:hover {{ color: {p.text}; border-color: {p.accent}; }}
#tkTab {{ color: {p.muted}; background: transparent; border: 1px solid transparent;
         border-radius: 7px; padding: 4px 11px; }}
#tkTab:hover {{ color: {p.text}; }}
#tkTab:checked {{ color: {p.text}; background: {p.card_hi}; border-color: {p.border_hi}; }}
#tkToggle {{ color: {p.muted}; background: transparent; border: 1px solid {p.border};
            border-radius: 7px; padding: 4px 11px; }}
#tkToggle:checked {{ color: {p.text}; border-color: {p.accent}; background: {p.wash(p.accent, 0.12)}; }}
#tkPrimary {{ color: #ffffff; background: {p.accent}; border: none; border-radius: 7px;
             padding: 6px 14px; font-weight: 600; }}
#tkPrimary:hover {{ background: {mix(p.accent, '#ffffff', 0.15)}; }}
#tkPrimary:disabled {{ background: {p.border_hi}; color: {p.faint}; }}
#tkInput, #tkSearch {{ {field} }}
#tkSearch {{ padding-left: 10px; min-width: 200px; }}
#tkInput:focus, #tkSearch:focus, #tkTitleInput:focus {{ border-color: {p.accent}; }}
#tkTitleInput {{ background: transparent; color: {p.text}; border: none;
                border-bottom: 1px solid {p.border_hi}; padding: 6px 2px; font-size: 18px;
                font-weight: 600; }}
#tkNotes {{ {field} }}
#tkPropChip {{ color: {p.muted}; background: transparent; border: 1px dashed {p.border_hi};
              border-radius: 7px; padding: 4px 10px; }}
#tkPropChip[set="true"] {{ color: {p.text}; border: 1px solid {p.border_hi};
                          background: {p.card_hi}; }}
#tkPropChip:hover {{ border-color: {p.accent}; }}
#tkFound {{ color: {p.accent}; background: {p.wash(p.accent, 0.12)}; border: none;
           border-radius: 6px; padding: 3px 9px; }}
#tkDay {{ color: {p.muted}; background: {p.card_hi}; border: 1px solid {p.border};
         border-radius: 8px; }}
#tkDay:checked {{ color: #ffffff; background: {p.accent}; border-color: {p.accent}; }}
#tkMenu {{ background: {p.card_hi}; color: {p.text}; border: 1px solid {p.border_hi};
          padding: 4px; }}
#tkMenu::item {{ padding: 5px 16px; border-radius: 5px; }}
#tkMenu::item:selected {{ background: {p.wash(p.accent, 0.25)}; }}
#tkUndo {{ background: {p.card_hi}; border: 1px solid {p.border_hi}; border-radius: 9px; }}
#tasksPage #panel {{ background: {p.card}; border: 1px solid {p.border}; border-radius: 12px; }}
#tasksPage #panel QLabel {{ color: {p.text}; }}
#tasksPage #fieldLabel {{ color: {p.muted}; }}
#tasksPage #field, #tasksPage #fieldBody, #tasksPage #panel QComboBox {{ {field} }}
#tasksPage #panel QComboBox QAbstractItemView {{ background: {p.card_hi}; color: {p.text};
    selection-background-color: {p.wash(p.accent, 0.3)}; }}
#tasksPage #fieldError {{ color: {p.red}; }}
#tasksPage #primary {{ color: #ffffff; background: {p.accent}; border: none; border-radius: 7px;
                      padding: 5px 12px; }}
#tasksPage #panel QPushButton {{ color: {p.text}; background: transparent;
    border: 1px solid {p.border_hi}; border-radius: 7px; padding: 5px 12px; }}
#tasksPage #panel #primary {{ background: {p.accent}; border: none; color: #ffffff; }}
#tasksPage QComboBox#tkProject {{ color: {p.muted}; background: transparent;
    border: 1px solid {p.border}; border-radius: 7px; padding: 4px 10px; }}
#tasksPage QComboBox#tkProject:hover {{ color: {p.text}; border-color: {p.border_hi}; }}
#tasksPage QComboBox#tkProject::drop-down {{ border: none; width: 16px; }}
#tasksPage QComboBox#tkProject QAbstractItemView {{ background: {p.card_hi}; color: {p.text};
    selection-background-color: {p.wash(p.accent, 0.3)}; }}
"""


def school_stylesheet(p: HomePalette = PALETTE) -> str:
    """School's rules (course tabs, the tutor's cards, the stage card), on top of
    Home's: the same palette, scoped to the School page."""
    return stylesheet(p).replace("#homePage", "#schoolPage") + f"""
#schoolPage QLabel#scTitle {{ color: {p.text}; font-size: 20px; font-weight: 600; }}
#scTabs {{ background: transparent; }}
#scTab {{ color: {p.muted}; background: transparent; border: none; border-radius: 7px;
         padding: 5px 10px; font-weight: 500; }}
#scTab:hover {{ color: {p.text}; background: {p.wash(p.accent, 0.08)}; }}
#scTab:checked {{ color: {p.text}; }}
#scUnderline {{ background: {p.accent}; border-radius: 1px; }}
#schoolPage QLabel#scExam {{ color: {p.amber}; background: {p.wash(p.amber, 0.14)}; border-radius: 9px; font-size: 13px;
          padding: 3px 10px; }}
#schoolPage QComboBox#scModule {{ color: {p.muted}; background: transparent;
    border: 1px solid {p.border}; border-radius: 7px; padding: 4px 10px; }}
#schoolPage QComboBox#scModule:hover {{ color: {p.text}; border-color: {p.border_hi}; }}
#schoolPage QComboBox#scModule::drop-down {{ border: none; width: 16px; }}
#schoolPage QComboBox#scModule QAbstractItemView {{ background: {p.card_hi}; color: {p.text};
    selection-background-color: {p.wash(p.accent, 0.3)}; }}
#scScroll, #scFeed {{ background: transparent; }}
#schoolPage QLabel#scEmptyTitle {{ color: {p.text}; font-size: 17px; font-weight: 600; }}
#scStarter {{ color: {p.text}; background: {p.card_hi}; border: 1px solid {p.border};
             border-radius: 14px; padding: 5px 12px; }}
#scStarter:hover {{ border-color: {p.accent}; background: {p.wash(p.accent, 0.12)}; }}
#schoolPage QLabel#scName {{ color: {p.accent}; font-size: 12px; font-weight: 600; }}
#scCheck {{ background: {p.card_hi}; border: 1px solid {p.border_hi};
           border-left: 3px solid {p.accent}; border-radius: 9px; }}
#schoolPage QLabel#scKicker {{ color: {p.accent}; font-size: 10px; font-weight: 700; letter-spacing: 1px; }}
QTextBrowser#scAnswer {{ color: {p.muted}; background: transparent; border: none; }}
#scLink {{ color: {p.accent}; background: transparent; border: none; padding: 2px 0; }}
#scLink:hover {{ color: {mix(p.accent, '#ffffff', 0.3)}; }}
#scIcon {{ color: {p.muted}; background: transparent; border: none; border-radius: 7px;
          font-size: 14px; }}
#scIcon:hover {{ background: {p.wash(p.accent, 0.14)}; }}
#scGhostIcon {{ color: {p.faint}; background: transparent; border: none; padding: 0 4px; }}
#scGhostIcon:hover {{ color: {p.red}; }}
#schoolPage QLabel#scAttach {{ color: {p.accent}; font-size: 12px; }}
#schoolPage QLabel#scStageTitle {{ color: {p.muted}; font-size: 12px; font-weight: 600; }}
"""
