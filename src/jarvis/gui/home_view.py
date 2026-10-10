"""The Home workspace: a dashboard of what needs the user, on their real data.

Layout (wide): a banner (greeting, the date, the quote of the day), then four
cards (Today's Focus, Upcoming Deadlines, Projects Health, Quick Stats), Today
at a glance (counts, the top priority, one recommendation), and a bottom row
(Weekly Overview, Recent Activity, Projects Snapshot); beside them a right
column (Today's Schedule, Upcoming, Quick Actions, a quote card). Narrower, the
right column moves under the main cards (``columns`` 1) and the rows wrap.

Everything shown comes from home_data.build_home: attention signals and the
study check's suggestion (attention.py), the user's items and Canvas entries,
projects and their milestone risk. What Jarvis has no source for (focus time,
sleep, training, weather) says so instead of showing a number. Home ticks items
off and opens things only when asked (through the window); "Plan it", "Ask
Jarvis" and "Before I study" open the chat with a request (``ask``).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from jarvis.gui import home_cards as hc
from jarvis.gui import motion
from jarvis.gui.home_data import HomeData, salutation
from jarvis.gui.home_style import PALETTE, stylesheet
from jarvis.gui.theme import Tokens

RIGHT_W = 320           # the right column
BESIDE_FROM = 1180      # Home's width from which the right column sits beside the cards
FOUR_FROM = 940         # main width for four cards in a row (else two)
THREE_FROM = 860        # main width for the bottom row's three cards (else one)
GLANCE_ROW_FROM = 720   # main width for the glance card's three parts side by side
GAP = 16
STUDY_ASK = "I'm about to study, what should I focus on?"


def _rows_box(card: hc.Card) -> QVBoxLayout:
    return card.body


class HomeView(QWidget):
    open_workspace = Signal(str)
    capture_requested = Signal()
    complete = Signal(int)        # item id
    open_item = Signal(int)       # item id: edit it in Tasks
    open_project = Signal(str)    # project slug: its items in Tasks
    open_node = Signal(str)       # a graph node (kept for the window's wiring)
    ask = Signal(str)             # open the chat and ask this ("" = just open it)

    def __init__(self, tokens: Tokens, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.tokens = tokens
        self.pal = PALETTE
        self.data: HomeData | None = None
        self.columns = 0
        self.narrow: bool | None = None
        self.mode: tuple | None = None
        self.day_index = 0
        self.setObjectName("homePage")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(stylesheet(self.pal))
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; }")
        outer.addWidget(self.scroll)
        page = QWidget()
        page.setObjectName("homePage")
        self.scroll.setWidget(page)
        self.grid = QGridLayout(page)
        self.grid.setContentsMargins(22, 18, 22, 22)
        self.grid.setHorizontalSpacing(GAP)
        self.grid.setVerticalSpacing(GAP)

        self._build_banner()
        self._build_cards()
        self._build_glance()
        self._build_bottom()
        self._build_right()
        self.errors = hc.label("", "hError", "meta", wrap=True)
        self.errors.hide()

        # the main column and the right column, each a vertical stack of rows
        self.main = QWidget()
        self.main_col = QVBoxLayout(self.main)
        self.main_col.setContentsMargins(0, 0, 0, 0)
        self.main_col.setSpacing(GAP)
        self.row1 = QGridLayout()
        self.row1.setSpacing(GAP)
        self.row3 = QGridLayout()
        self.row3.setSpacing(GAP)
        self.main_col.addWidget(self.banner)
        self.main_col.addLayout(self.row1)
        self.main_col.addWidget(self.glance)
        self.main_col.addLayout(self.row3)
        self.main_col.addWidget(self.errors)
        self.main_col.addStretch(1)
        self.right = QWidget()
        self.right_grid = QGridLayout(self.right)
        self.right_grid.setContentsMargins(0, 0, 0, 0)
        self.right_grid.setSpacing(GAP)
        self._place(1500)

    # ------------------------------------------------------------ building

    def _build_banner(self) -> None:
        p = self.pal
        self.banner = hc.PhotoPanel(p, 14)
        self.banner.setMinimumHeight(118)
        lay = QHBoxLayout(self.banner)
        lay.setContentsMargins(26, 20, 26, 18)
        lay.setSpacing(24)
        left = QVBoxLayout()
        left.setSpacing(4)
        self.greeting = hc.label("", "", "big")
        f = self.greeting.font()
        f.setPixelSize(26)
        f.setWeight(f.Weight.DemiBold)
        self.greeting.setFont(f)
        left.addWidget(self.greeting)
        self.purpose = hc.label("Here's what's happening today and what needs your attention.",
                                "hSub", "body", wrap=True)
        left.addWidget(self.purpose)
        left.addStretch(1)
        chips = QHBoxLayout()
        chips.setSpacing(10)
        self.date = hc.label("", "hSub", "label")
        chips.addWidget(self.date)
        self.weather = hc.label("Weather not set up", "hFaint", "meta")
        self.weather.setToolTip("Jarvis has no weather source yet.")
        chips.addWidget(self.weather)
        chips.addStretch(1)
        left.addLayout(chips)
        lay.addLayout(left, 3)
        right = QVBoxLayout()
        right.setSpacing(4)
        right.addStretch(1)
        self.quote_text = hc.label("", "", "body", wrap=True)
        self.quote_text.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.quote_text)
        self.quote_by = hc.label("", "hFaint", "meta")
        self.quote_by.setAlignment(Qt.AlignmentFlag.AlignRight)
        right.addWidget(self.quote_by)
        right.addStretch(1)
        lay.addLayout(right, 2)
        for w in (self.greeting, self.purpose, self.date, self.weather, self.quote_text,
                  self.quote_by):
            hc.shadowed(w)

    def _build_cards(self) -> None:
        self.focus_card = hc.Card("Today's Focus", "focus", self.pal, count=True,
                                  link_text="View all →")
        self.focus_card.view_all.connect(lambda: self.open_workspace.emit("tasks"))
        self.deadline_card = hc.Card("Upcoming Deadlines", "calendar", self.pal, count=True,
                                     link_text="View all →")
        self.deadline_card.view_all.connect(lambda: self.open_workspace.emit("tasks"))
        self.health_card = hc.Card("Projects Health", "health", self.pal,
                                   link_text="Open Projects →")
        self.health_card.view_all.connect(lambda: self.open_workspace.emit("projects"))
        self.stats_card = hc.Card("Quick Stats", "stats", self.pal)
        self.today_link = self.focus_card.link          # kept: "View all" opens Tasks
        self.project_link = self.health_card.link
        for c in (self.focus_card, self.deadline_card, self.health_card, self.stats_card):
            c.setMinimumWidth(190)
            c.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.focus_rows: list[QWidget] = []
        self.deadline_rows: list[QWidget] = []
        self.project_rows: list[QWidget] = []
        self.stats: dict[str, QWidget] = {}

    def _build_glance(self) -> None:
        p = self.pal
        self.glance = hc.Card("Today at a glance", "glance", p)
        self.glance_date = hc.label("", "hMuted", "meta")
        self.glance.head.insertWidget(3, self.glance_date)
        self.glance_grid = QGridLayout()
        self.glance_grid.setSpacing(GAP)
        self.glance.body.addLayout(self.glance_grid)
        self.counts = QWidget()
        self.counts_col = QVBoxLayout(self.counts)
        self.counts_col.setContentsMargins(0, 0, 0, 0)
        self.counts_col.setSpacing(9)
        self.count_labels: dict[str, object] = {}
        # top priority
        self.priority = QFrame()
        self.priority.setObjectName("hInner")
        self.priority.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        pl = QVBoxLayout(self.priority)
        pl.setContentsMargins(16, 12, 16, 12)
        pl.setSpacing(5)
        tag = QHBoxLayout()
        tag.setSpacing(6)
        tag.addWidget(hc.Dot(p.red, 7))
        self.priority_tag = hc.label("Top priority", "", "meta")
        self.priority_tag.setStyleSheet(f"color: {p.red};")
        tag.addWidget(self.priority_tag)
        tag.addStretch(1)
        pl.addLayout(tag)
        self.priority_title = hc.label("", "", "title", wrap=True)
        pl.addWidget(self.priority_title)
        self.priority_why = hc.label("", "hMuted", "label", wrap=True)
        pl.addWidget(self.priority_why)
        pl.addStretch(1)
        self.priority_btn = hc.button("View task →")
        self.priority_btn.clicked.connect(self._open_priority)
        pl.addWidget(self.priority_btn, 0, Qt.AlignmentFlag.AlignLeft)
        # recommendation
        self.recommend = QFrame()
        self.recommend.setObjectName("hInner")
        self.recommend.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        rl = QVBoxLayout(self.recommend)
        rl.setContentsMargins(16, 12, 16, 12)
        rl.setSpacing(5)
        rtag = QHBoxLayout()
        rtag.setSpacing(6)
        rtag.addWidget(hc.Icon("bulb", p.accent, 16))
        tag_label = hc.label("Recommendation", "", "meta")
        tag_label.setStyleSheet(f"color: {p.accent};")
        rtag.addWidget(tag_label)
        rtag.addStretch(1)
        rl.addLayout(rtag)
        self.recommend_text = hc.label("", "", "body", wrap=True)
        rl.addWidget(self.recommend_text)
        self.recommend_note = hc.label("A suggestion by the dates on your list; you decide.",
                                       "hFaint", "meta", wrap=True)
        rl.addWidget(self.recommend_note)
        rl.addStretch(1)
        self.plan_btn = hc.button("Plan it →")
        self.plan_btn.setToolTip("Opens the chat with a planning request; nothing is saved")
        self.plan_btn.clicked.connect(
            lambda: self.ask.emit(self.data.recommendation.ask
                                  if self.data and self.data.recommendation else ""))
        rl.addWidget(self.plan_btn, 0, Qt.AlignmentFlag.AlignLeft)

    def _build_bottom(self) -> None:
        p = self.pal
        self.week_card = hc.Card("Weekly Overview", "week", p)
        legend = QHBoxLayout()
        legend.setSpacing(6)
        for name in hc.WeekBars.SERIES:
            legend.addWidget(hc.Dot(p.category(name), 7))
            legend.addWidget(hc.label(name, "hMuted", "meta"))
            legend.addSpacing(6)
        legend.addStretch(1)
        self.week_card.body.addLayout(legend)
        self.week_bars = hc.WeekBars(p)
        self.week_card.body.addWidget(self.week_bars, 1)
        self.week_note = hc.label("What's due each day this week", "hFaint", "meta")
        self.week_card.body.addWidget(self.week_note)
        self.activity_card = hc.Card("Recent Activity", "activity", p)
        self.snapshot_card = hc.Card("Projects Snapshot", "snapshot", p,
                                     link_text="View all →")
        self.snapshot_card.view_all.connect(lambda: self.open_workspace.emit("projects"))
        for c in (self.week_card, self.activity_card, self.snapshot_card):
            c.setMinimumWidth(220)
        self.activity_rows: list[QWidget] = []

    def _build_right(self) -> None:
        p = self.pal
        self.schedule_card = hc.Card("Today's Schedule", "schedule", p)
        nav = QHBoxLayout()
        nav.setSpacing(6)
        self.prev_day = QPushButton("‹")
        self.next_day = QPushButton("›")
        for b in (self.prev_day, self.next_day):
            b.setObjectName("hDayBtn")
            b.setFixedSize(24, 24)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
        self.day_label = hc.label("", "hMuted", "meta")
        self.prev_day.clicked.connect(lambda: self._step_day(-1))
        self.next_day.clicked.connect(lambda: self._step_day(1))
        nav.addWidget(self.prev_day)
        nav.addWidget(self.day_label)
        nav.addWidget(self.next_day)
        self.schedule_card.head.addLayout(nav)
        self.today_rows: list[hc.SlotRow] = []
        self.upcoming_card = hc.Card("Upcoming", "flag", p, link_text="View all →")
        self.upcoming_card.view_all.connect(lambda: self.open_workspace.emit("tasks"))
        self.actions_card = hc.Card("Quick Actions", "bolt", p)
        acts = (("plus", "Add task", "Capture in plain words", self.capture_requested.emit),
                ("chat", "Ask Jarvis", "Questions, plans, what's next",
                 lambda: self.ask.emit("")),
                ("book", "Before I study", "What's coming up and what's first",
                 lambda: self.ask.emit(STUDY_ASK)),
                ("folder", "Open projects", "Milestones, issues and the graph",
                 lambda: self.open_workspace.emit("projects")))
        self.actions: dict[str, hc.ActionButton] = {}
        for icon, title, sub, fn in acts:
            b = hc.ActionButton(icon, title, sub, p)
            b.clicked.connect(fn)
            self.actions_card.body.addWidget(b)
            self.actions[title] = b
        self.capture_btn = self.actions["Add task"]
        self.quote_card = hc.PhotoPanel(p, 12, side="right")
        self.quote_card.setMinimumHeight(120)
        ql = QVBoxLayout(self.quote_card)
        ql.setContentsMargins(18, 16, 18, 14)
        ql.addStretch(1)
        self.quote_card_text = hc.label("", "", "label", wrap=True)
        self.quote_card_text.setAlignment(Qt.AlignmentFlag.AlignRight)
        ql.addWidget(self.quote_card_text)
        self.quote_card_by = hc.label("", "hFaint", "meta")
        self.quote_card_by.setAlignment(Qt.AlignmentFlag.AlignRight)
        ql.addWidget(self.quote_card_by)
        for w in (self.quote_card_text, self.quote_card_by):
            hc.shadowed(w)

    # ------------------------------------------------------------ layout

    def _place(self, width: int) -> None:
        """Arrange the cards for Home's width: the right column beside the main
        cards (``columns`` 2) or under them (1); rows of four / two / one."""
        beside = width >= BESIDE_FROM
        main_w = width - 44 - (RIGHT_W + GAP if beside else 0)
        four = 4 if main_w >= FOUR_FROM else 2 if main_w >= 440 else 1
        three = 3 if main_w >= THREE_FROM else 1
        glance_row = main_w >= GLANCE_ROW_FROM
        right_cols = 1 if beside or main_w < 600 else 2
        mode = (beside, four, three, glance_row, right_cols)
        self.narrow = not beside
        if mode == self.mode:
            return
        self.mode = mode
        self.columns = 2 if beside else 1
        for g in (self.grid, self.row1, self.row3, self.right_grid, self.glance_grid):
            while g.count():
                g.takeAt(0)
        top = Qt.AlignmentFlag.AlignTop
        if beside:
            self.grid.addWidget(self.main, 0, 0)
            self.grid.addWidget(self.right, 0, 1, top)
            self.right.setFixedWidth(RIGHT_W)
            self.grid.setColumnStretch(0, 1)
            self.grid.setColumnStretch(1, 0)
        else:
            self.grid.addWidget(self.main, 0, 0)
            self.grid.addWidget(self.right, 1, 0, top)
            self.right.setMinimumWidth(0)
            self.right.setMaximumWidth(16777215)
            self.grid.setColumnStretch(1, 0)
        cards = (self.focus_card, self.deadline_card, self.health_card, self.stats_card)
        for k, c in enumerate(cards):
            self.row1.addWidget(c, k // four, k % four)
        for k in range(4):
            self.row1.setColumnStretch(k, 1 if k < four else 0)
        for k, c in enumerate((self.week_card, self.activity_card, self.snapshot_card)):
            self.row3.addWidget(c, k // three, k % three)
        for k in range(3):
            self.row3.setColumnStretch(k, 1 if k < three else 0)
        parts = (self.counts, self.priority, self.recommend)
        for k, w in enumerate(parts):
            if glance_row:
                self.glance_grid.addWidget(w, 0, k)
            else:
                self.glance_grid.addWidget(w, k, 0)
        for k in range(3):
            self.glance_grid.setColumnStretch(k, (2 if k else 1) if glance_row and k < 3
                                              else 0)
        right = (self.schedule_card, self.upcoming_card, self.actions_card, self.quote_card)
        for k, w in enumerate(right):
            self.right_grid.addWidget(w, k // right_cols, k % right_cols)
        self.right_grid.setRowStretch(4, 1)

    def resizeEvent(self, e) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(e)
        self._place(self.width())

    # ------------------------------------------------------------ data

    def set_data(self, data: HomeData, force: bool = False) -> None:
        if data == self.data and not force:
            return
        first = self.data is None
        self.data = data
        self.greeting.setText(salutation_for(data))
        self.date.setText(f"{data.today:%A, %B} {data.today.day}")
        self.glance_date.setText(f"{data.today:%a %b} {data.today.day}")
        q = data.quote
        self.quote_text.setText(f"“{q.text}”" if q else "")
        self.quote_by.setText(f"— {q.author}" if q else "")
        ordinal = data.today.toordinal()
        self.banner.set_photo(hc.photo("banner", ordinal))
        self.quote_card.set_photo(hc.photo("quote", ordinal))
        q2 = _second_quote(data)
        self.quote_card_text.setText(f"“{q2[0]}”")
        self.quote_card_by.setText(f"— {q2[1]}")
        self._fill_focus(data)
        self._fill_deadlines(data)
        self._fill_health(data)
        self._fill_stats(data)
        self._fill_glance(data)
        self._fill_week(data)
        self._fill_activity(data)
        self._fill_snapshot(data)
        self.day_index = min(self.day_index, max(0, len(data.schedule) - 1))
        self._fill_schedule()
        self._fill_upcoming(data)
        self.errors.setText(" · ".join(data.errors))
        self.errors.setVisible(bool(data.errors))
        if first:
            cards = [self.banner, self.focus_card, self.deadline_card, self.health_card,
                     self.stats_card, self.glance]
            for k, w in enumerate(cards):
                motion.fade_rise(w, delay=k * motion.STAGGER)

    def _fill_focus(self, d: HomeData) -> None:
        box = self.focus_card.body
        hc.clear(box)
        self.focus_rows = []
        self.focus_card.set_count(len(d.focus) or None)
        for f in d.focus:
            row = hc.Row(f.head, f.note, self.pal.tone(f.tone))
            row.clicked.connect(lambda _=False, f=f: self._open(f.item_id, f.url))
            box.addWidget(row)
            self.focus_rows.append(row)
        if not d.focus:
            box.addWidget(hc.empty("Nothing needs attention right now.",
                                   "Overdue items, exams and deadlines show here."))
        box.addStretch(1)

    def _fill_deadlines(self, d: HomeData) -> None:
        box = self.deadline_card.body
        hc.clear(box)
        self.deadline_rows = []
        self.deadline_card.set_count(d.deadlines_total or None)
        tone = {"exam": "red", "milestone": "purple", "canvas": "blue"}
        for dl in d.deadlines[:4]:
            sub = dl.label + (f", {dl.time}" if dl.time else "") + (
                f" · {dl.project}" if dl.project else "")
            near = (dl.day - d.today).days <= 2
            row = hc.Row(dl.title, sub, self.pal.tone("amber" if near else tone.get(dl.kind,
                                                                                     "blue")))
            row.clicked.connect(lambda _=False, dl=dl: self._open(dl.item_id, dl.url))
            box.addWidget(row)
            self.deadline_rows.append(row)
        if not d.deadlines:
            box.addWidget(hc.empty("Nothing due in the next two weeks."))
        box.addStretch(1)

    def _fill_health(self, d: HomeData) -> None:
        box = self.health_card.body
        hc.clear(box)
        self.project_rows = []
        for s in d.projects[:4]:
            row = QPushButton()
            row.setObjectName("hRow")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            row.summary = s
            col = QVBoxLayout(row)
            col.setContentsMargins(4, 4, 4, 4)
            col.setSpacing(4)
            top = QHBoxLayout()
            top.addWidget(hc.elided(s.name, "", "body"), 1)
            pct = hc.label(f"{round(s.progress * 100)}%", "hMuted", "meta")
            top.addWidget(pct)
            col.addLayout(top)
            if s.risk:
                status = hc.label(s.risk, "", "meta")
                status.setStyleSheet(f"color: {self.pal.tone(s.risk_tone)};")
            else:
                status = hc.label(f"{s.open} open · {s.done} done", "hMuted", "meta")
            col.addWidget(status)
            bar = hc.Bar(s.progress, self.pal.tone(s.risk_tone) if s.risk_tone != "muted"
                         else self.pal.green, self.pal)
            col.addWidget(bar)
            bar.animate_in()
            row.setMinimumHeight(col.sizeHint().height())
            row.clicked.connect(lambda _=False, slug=s.slug: self.open_project.emit(slug))
            box.addWidget(row)
            self.project_rows.append(row)
        if not d.projects:
            box.addWidget(hc.empty("No active projects."))
        box.addStretch(1)

    def _fill_stats(self, d: HomeData) -> None:
        box = self.stats_card.body
        hc.clear(box)
        self.stats = {}
        for st in d.stats:
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addWidget(hc.label(st.label, "hMuted" if st.value is None else "", "label"), 1)
            if st.value is None:
                val = hc.label("Not tracked", "hFaint", "meta")
                val.setToolTip("Jarvis has no source for this yet.")
            else:
                val = hc.label(st.value, "", "value")
                if st.tone in ("bad", "good") and st.label != "Tasks completed":
                    val.setStyleSheet(f"color: {self.pal.tone(st.tone)};")
            row.addWidget(val, 0, Qt.AlignmentFlag.AlignRight)
            box.addLayout(row)
            self.stats[st.label] = val
            if st.delta:
                dl = hc.label(st.delta, "", "meta")
                dl.setAlignment(Qt.AlignmentFlag.AlignRight)
                dl.setStyleSheet(f"color: {self.pal.tone(st.tone)};")
                box.addWidget(dl)
        box.addStretch(1)

    def _fill_glance(self, d: HomeData) -> None:
        p = self.pal
        hc.clear(self.counts_col)
        self.count_labels = {}
        g = d.glance
        if g is not None:
            for key, n, one, many, tone in (
                    ("overdue", g.overdue, "overdue item", "overdue items", "red"),
                    ("due", g.due_week, "thing due this week", "things due this week", "amber"),
                    ("risk", g.at_risk, "project at risk", "projects at risk", "purple"),
                    ("done", g.done_week, "done in the last week", "done in the last week",
                     "green")):
                row = QHBoxLayout()
                row.setSpacing(10)
                row.addWidget(hc.Dot(p.tone(tone) if n else p.grey, 9, ring=True))
                text = hc.label(f"{n} {one if n == 1 else many}", "" if n else "hMuted",
                                "label")
                row.addWidget(text, 1)
                self.counts_col.addLayout(row)
                self.count_labels[key] = text
        self.counts_col.addStretch(1)
        top = d.top_priority
        if top is None:
            self.priority_title.setText("Nothing pressing")
            self.priority_why.setText("No overdue items, exams or close deadlines.")
            self.priority_btn.hide()
        else:
            self.priority_title.setText(top.head)
            self.priority_why.setText(d.top_why or top.note)
            self.priority_btn.setText("View task →" if top.item_id is not None
                                      else "Open in Canvas →" if top.url else "Ask Jarvis →")
            self.priority_btn.show()
        rec = d.recommendation
        self.recommend_text.setText(rec.text if rec else "")
        self.plan_btn.setVisible(rec is not None)

    def _fill_week(self, d: HomeData) -> None:
        self.week_bars.set_week([(b.label, (b.study, b.work, b.personal), b.today)
                                 for b in d.week])

    def _fill_activity(self, d: HomeData) -> None:
        box = self.activity_card.body
        hc.clear(box)
        self.activity_rows = []
        tone = {"done": "green", "added": "blue", "commit": "purple", "canvas": "green"}
        for a in d.recent:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(hc.Dot(self.pal.tone(tone.get(a.kind, "grey")), 8),
                          0, Qt.AlignmentFlag.AlignVCenter)
            text = hc.elided(a.text, "", "label")
            row.addWidget(text, 1)
            row.addWidget(hc.label(a.ago, "hFaint", "meta"))
            box.addLayout(row)
            self.activity_rows.append(text)
        if not d.recent:
            box.addWidget(hc.empty("Nothing in the last week.",
                                   "Items you add or finish and new commits show here."))
        box.addStretch(1)

    def _fill_snapshot(self, d: HomeData) -> None:
        box = self.snapshot_card.body
        hc.clear(box)
        colors = (self.pal.blue, self.pal.purple, self.pal.green, self.pal.amber)
        for k, s in enumerate(d.projects[:4]):
            row = QPushButton()
            row.setObjectName("hRow")
            row.setCursor(Qt.CursorShape.PointingHandCursor)
            lay = QHBoxLayout(row)
            lay.setContentsMargins(4, 4, 4, 4)
            lay.setSpacing(10)
            tile = hc.label(s.name[:1].upper(), "", "title")
            tile.setAlignment(Qt.AlignmentFlag.AlignCenter)
            tile.setFixedSize(32, 32)
            c = colors[k % len(colors)]
            tile.setStyleSheet(f"color: {c}; background: {self.pal.wash(c, 0.2)}; "
                               "border-radius: 8px;")
            lay.addWidget(tile)
            col = QVBoxLayout()
            col.setSpacing(1)
            col.addWidget(hc.elided(s.name, "", "label"))
            nxt = (f"Next: {s.next_title}" if s.next_title else "No open items") + (
                f" · {s.last_activity}" if s.last_activity else "")
            col.addWidget(hc.elided(nxt, "hMuted", "meta"))
            lay.addLayout(col, 1)
            row.setMinimumHeight(lay.sizeHint().height())
            row.clicked.connect(lambda _=False, slug=s.slug: self.open_project.emit(slug))
            box.addWidget(row)
        if not d.projects:
            box.addWidget(hc.empty("No active projects."))
        box.addStretch(1)

    def _fill_schedule(self) -> None:
        d = self.data
        box = self.schedule_card.body
        hc.clear(box)
        self.today_rows = []
        if d is None or not d.schedule:
            return
        day, slots = d.schedule[self.day_index]
        self.schedule_card.title.setText("Today's Schedule" if self.day_index == 0
                                         else "Schedule")
        self.day_label.setText(f"{day:%a %b} {day.day}")
        self.prev_day.setEnabled(self.day_index > 0)
        self.next_day.setEnabled(self.day_index < len(d.schedule) - 1)
        for sl in slots[:10]:
            row = hc.SlotRow(sl, self.pal)
            row.toggled.connect(self.complete)
            row.selected.connect(self.open_item)
            row.open_url.connect(lambda url: QDesktopServices.openUrl(QUrl(url)))
            box.addWidget(row)
            if sl.item_id is not None:
                self.today_rows.append(row)
        if len(slots) > 10:
            more = hc.link(f"+{len(slots) - 10} more in Tasks →")
            more.clicked.connect(lambda: self.open_workspace.emit("tasks"))
            box.addWidget(more, 0, Qt.AlignmentFlag.AlignLeft)
        if not slots:
            box.addWidget(hc.empty("Nothing on this day.",
                                   "Items with a date and Canvas entries show here; Jarvis "
                                   "has no calendar yet, so only what you gave a time has "
                                   "one."))

    def _step_day(self, step: int) -> None:
        if self.data is None:
            return
        self.day_index = max(0, min(len(self.data.schedule) - 1, self.day_index + step))
        self._fill_schedule()

    def _fill_upcoming(self, d: HomeData) -> None:
        box = self.upcoming_card.body
        hc.clear(box)
        tone = {"exam": "red", "milestone": "purple", "canvas": "blue"}
        for dl in d.upcoming:
            row = QHBoxLayout()
            row.setSpacing(10)
            row.addWidget(hc.Dot(self.pal.tone(tone.get(dl.kind, "amber")), 8))
            when = hc.label(dl.label, "hMuted", "meta")
            when.setFixedWidth(78)
            row.addWidget(when)
            row.addWidget(hc.elided(dl.title, "", "label"), 1)
            box.addLayout(row)
        if not d.upcoming:
            box.addWidget(hc.empty("Nothing dated after today."))

    # ------------------------------------------------------------ actions

    def _open(self, item_id: int | None, url: str | None) -> None:
        if item_id is not None:
            self.open_item.emit(item_id)
        elif url:
            QDesktopServices.openUrl(QUrl(url))

    def _open_priority(self) -> None:
        top = self.data.top_priority if self.data else None
        if top is None:
            return
        if top.item_id is not None or top.url:
            self._open(top.item_id, top.url)
        else:
            self.ask.emit(STUDY_ASK)


def salutation_for(d: HomeData) -> str:
    """The banner's greeting: by the time of day the data was built for."""
    from datetime import datetime, time
    hour = {"Good morning.": 9, "Good afternoon.": 14}.get(d.greeting, 19)
    return salutation(datetime.combine(d.today, time(hour)), d.name)


def _second_quote(d: HomeData) -> tuple[str, str]:
    """The quote card's line: the next quote in the list (not the banner's)."""
    from jarvis.gui.home_data import QUOTES
    k = (d.today.toordinal() + 5) % len(QUOTES)
    return QUOTES[k]
