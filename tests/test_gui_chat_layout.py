"""The chat beside the workspaces: replies fit the panel, the window fits the
chat, and streaming and typing stay cheap. Offscreen, fake ask functions,
databases under tmp_path.

Root causes covered: a QLabel can't break a long word, so one path made a reply
as wide as the path (past the window's edge); the window's minimum width with
the chat open was more than its own minimum (Tasks' header alone needed 623 px
of the 524 left at 980); each streamed token re-set and re-laid out the whole
reply, so the cost per token grew with the reply and starved typing.
"""
from __future__ import annotations

import os
import statistics
import threading
import time
from types import SimpleNamespace

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QScrollArea  # noqa: E402
from test_graph_items import big  # noqa: E402
from test_gui_data import seed  # noqa: E402
from test_gui_think import _settings  # noqa: E402
from test_gui_voice import pump  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis import items as it  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.chat_panel import STREAM_CARET, BotMessage  # noqa: E402
from jarvis.gui.theme import DARK  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402

PATH = "/home/tal_desktop/Projects/stm32-lidar-scanner/firmware/Core/Src/servo_pwm_control.c"
WORD = "STM32F401RCT6_TIM3_CH1_PWM_SERVO_PAN_OUTPUT_COMPARE_PRELOAD_ENABLE_" * 2
REPLY = (f"Check `{PATH}` first.\n\nThe identifier {WORD} drives the pan servo.\n\n"
         "- `power.rails.5V.max_current_amps_continuous_rating` is unknown\n\n"
         + "More prose so the answer is long. " * 30)
SIZES = ((1500, 940), (980, 600))


def answer(text=REPLY):
    return SimpleNamespace(text=text, sources=[], notes=[], model="gemma4:26b", duration=2.0)


def streaming_ask(text=REPLY, delay=0.0):
    def ask(conn, cfg, question, history, on_token, session_id, **kw):
        for w in text.split(" "):
            on_token(w + " ")
            if delay:
                time.sleep(delay)
        return answer(text)
    return ask


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="dark")


@pytest.fixture
def make(app, tmp_path):
    made = []

    def build(ask=None, start="projects"):
        cfg = _cfg(tmp_path)
        conn = db.connect(cfg.db_path)
        seed(conn, n_cards=2)
        it.create_item(conn, "Re-measure the 5V rail", due="2026-10-07", project="lidar")
        conn.commit()
        win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start=start,
                         ask_fn=ask or streaming_ask(), chat_open=True,
                         voice={"settings": _settings(tmp_path), "mic_ok": False})
        win.resize(1500, 940)
        win.show()
        app.processEvents()
        made.append((win, conn))
        return win

    yield build
    for w, c in made:
        w.close()
        c.close()


@pytest.fixture
def bot(app):
    b = BotMessage(DARK)
    b.resize(340, 600)
    b.show()
    yield b
    b.close()
    b.deleteLater()
    QApplication.processEvents()


def right_edge(w, win) -> int:
    return w.mapTo(win, w.rect().topRight()).x()


def settle(win, size):
    win.resize(*size)
    for _ in range(3):
        QApplication.processEvents()


# ---------------------------------------------------------------- clipping


@pytest.mark.parametrize("size", SIZES)
def test_long_words_wrap_inside_the_panel(make, size):
    win = make()
    chat = win.chat
    chat.ask(f"Why does {PATH} stall? {WORD}")
    chat.wait()
    settle(win, size)
    user, bot = chat.messages
    holder = chat.scroll.widget()
    assert holder.width() <= chat.scroll.viewport().width()     # nothing wider than the panel
    for w in (user, user.label, bot, bot.body):
        assert right_edge(w, win) <= right_edge(chat, win), type(w).__name__
    # the long word wrapped instead of setting the reply's width
    assert bot.body.minimumSizeHint().width() < 100
    assert bot.body.height() > 6 * bot.body.fontMetrics().lineSpacing()
    # wrapping inserts nothing: copying gives the text as written
    assert PATH in user.label.text() and WORD in bot.body.text().replace("\n", "")
    # the header and the composer stay on screen
    for w in (chat.title, chat.new_btn, chat.composer.edit, chat.mic_btn, chat.composer.send_btn):
        assert w.isVisible() and right_edge(w, win) <= win.width(), w.objectName()


def test_short_question_keeps_a_snug_bubble(make):
    win = make()
    win.chat.ask("Is my 5V rail OK?")
    win.chat.wait()
    bubble = win.chat.messages[0].label
    assert bubble.width() < win.chat.width() // 2
    assert bubble.height() < 2 * bubble.fontMetrics().lineSpacing() + 4    # one line


def test_long_source_chips_are_elided(make):
    def ask(conn, cfg, q, history, on_token, sid, **kw):
        a = answer("See the milestone.")
        a.sources = [SimpleNamespace(ref="milestone:" + "Core demo with tracking " * 6,
                                     label="Core demo", url=None)]
        return a
    win = make(ask)
    win.chat.ask("q")
    win.chat.wait()
    chip = win.chat.messages[-1].chip_buttons[0]
    assert chip.text().endswith("…") and right_edge(chip, win) <= right_edge(win.chat, win)


@pytest.mark.parametrize("size", SIZES)
def test_the_window_fits_every_workspace_with_the_chat_open(make, size):
    win = make()
    win.chat.ask(f"{PATH} {WORD}")
    win.chat.wait()
    settle(win, size)
    states = [(k, None) for k in win.workspaces] + [("tasks", "editor")]
    for key, extra in states:
        win.show_workspace(key)
        if extra == "editor":
            v = win.tasks_view
            v.select(next(iter(v.rows)))
        settle(win, size)
        page = win.centralWidget()
        assert page.minimumSizeHint().width() <= size[0], (key, extra)
        assert right_edge(win.chat, win) < size[0]
        # scrolling workspaces never lay out wider than they show
        for sa in win.workspaces[key].findChildren(QScrollArea):
            sideways = sa.horizontalScrollBarPolicy() != Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            if sa.isVisible() and sa.widget() is not None and not sideways:   # (the board)
                assert sa.widget().width() <= sa.viewport().width() + 1, (key, extra)
        if extra == "editor":
            win.tasks_view.select(None)


def test_cockpit_left_column_steps_aside_only_when_needed(make):
    win = make()
    settle(win, (1500, 940))
    assert win.left_column.isVisible()                   # room for vitals, graph and chat
    settle(win, (980, 600))
    assert not win.left_column.isVisible()               # the graph keeps its room instead
    assert win.graph.width() >= 420
    win.set_chat_open(False)
    QApplication.processEvents()
    assert win.left_column.isVisible() and win.right.isVisible()
    win.set_chat_open(True)
    QApplication.processEvents()
    assert not win.left_column.isVisible()


def test_tasks_and_home_reflow_beside_the_chat(make):
    win = make(start="tasks")
    settle(win, (980, 600))
    v = win.tasks_view
    assert v.filters_below and v.scroll.isVisible()
    v.select(next(iter(v.rows)))
    QApplication.processEvents()
    assert v.editor_alone and not v.scroll.isVisible()   # the editor takes the board's place
    v.select(None)
    QApplication.processEvents()
    assert v.scroll.isVisible()
    win.set_chat_open(False)
    settle(win, (1500, 940))
    assert not v.filters_below
    win.show_workspace("home")
    settle(win, (1500, 940))
    assert win.home_view.columns == 2 and not win.home_view.narrow
    win.set_chat_open(True)
    settle(win, (980, 600))
    assert win.home_view.columns == 1


# ---------------------------------------------------------------- streaming + typing


def test_streaming_appends_instead_of_relaying_out_the_reply(bot):
    edits = []
    bot.add_token("First ")
    bot.body.document().contentsChange.connect(lambda pos, rem, add: edits.append((rem, add)))
    tokens = ["word "] * 200 + ["\n\n"] + ["more "] * 200
    for t in tokens:
        bot.add_token(t)
    # each token only inserted its own text; nothing was removed or re-set
    assert len(edits) == len(tokens)
    assert all(rem == 0 for rem, _ in edits)
    assert [add for _, add in edits] == [len(t) for t in tokens]
    assert bot.body.text() == bot.streamed and bot.body.toPlainText().endswith(STREAM_CARET)
    bot.finish(answer("Done: **bold**."))
    assert bot.body.text() == "Done: bold." and STREAM_CARET not in bot.body.toPlainText()


def test_token_cost_does_not_grow_with_the_reply(bot):
    para = "More prose so the answer is long, with words of many lengths. " * 6 + "\n\n"
    words = (para * 40).split(" ")

    def cost(chunk):
        t0 = time.perf_counter()
        for w in chunk:
            bot.add_token(w + " ")
            QApplication.processEvents()
        return (time.perf_counter() - t0) / len(chunk)

    early = cost(words[:150])
    cost(words[150:-150])                                 # ~15,000 characters in
    late = cost(words[-150:])
    # before the fix the late tokens cost ~15x the early ones (the whole reply re-laid out)
    assert late < max(4 * early, 0.002), (early, late)


def test_typing_stays_responsive_while_an_answer_streams(make):
    gate = threading.Event()
    slow = streaming_ask((REPLY + "\n\n") * 2, delay=0.004)   # a model at ~250 tokens/s

    def ask(*a, **kw):
        gate.set()
        return slow(*a, **kw)

    win = make(ask)
    chat = win.chat
    reloads = []
    win.reload = lambda *a, **kw: reloads.append(1)      # nothing reloads per keystroke
    chat.ask("Explain the rail")
    gate.wait(5)
    # unrelated background work holding the GIL now and then, like a busy worker
    stop = threading.Event()

    def busy():
        while not stop.is_set():
            sum(i * i for i in range(20_000))
            time.sleep(0.002)

    worker = threading.Thread(target=busy, daemon=True)
    worker.start()
    try:
        win.activateWindow()
        chat.focus_input()
        QApplication.processEvents()
        text = "Can the servos share the 5V rail with the LiDAR? " * 2
        times = []
        for ch in text:
            t0 = time.perf_counter()
            QTest.keyClick(chat.composer.edit, ch)
            QApplication.processEvents()
            times.append(time.perf_counter() - t0)
        streamed_meanwhile = chat.busy
        think = chat.think_mode
        QTest.keyClick(chat.composer.edit, Qt.Key.Key_T, Qt.KeyboardModifier.ControlModifier)
        QApplication.processEvents()
        shortcut_worked = chat.think_mode != think       # Ctrl+T, mid-stream
    finally:
        stop.set()
        worker.join()
    assert chat.composer.edit.toPlainText() == text      # every key landed, in order
    assert streamed_meanwhile                             # typed while the answer streamed
    assert shortcut_worked
    assert statistics.median(times) < 0.010 and max(times) < 0.100, (
        statistics.median(times), max(times))
    assert reloads == []
    chat.wait(20)
    pump(lambda: not chat.busy)
    assert chat.messages[-1].body.text().startswith("Check")


def test_wheel_over_a_reply_scrolls_the_conversation(make):
    win = make(streaming_ask(REPLY * 3))
    win.chat.ask("long one")
    win.chat.wait()
    settle(win, (980, 600))
    bar = win.chat.scroll.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum())
    before = bar.value()
    body = win.chat.messages[-1].body
    pos = QPointF(body.width() / 2, 10)
    ev = QWheelEvent(pos, QPointF(body.mapToGlobal(QPoint(int(pos.x()), 10))), QPoint(0, 0),
                     QPoint(0, 120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(body.viewport(), ev)
    QApplication.processEvents()
    assert bar.value() < before


# ---------------------------------------------------------------- composer


class _Tally(QObject):
    def __init__(self):
        super().__init__()
        self.shows = 0

    def eventFilter(self, obj, e):  # noqa: N802 - Qt API
        if e.type() == QEvent.Type.Show:
            self.shows += 1
        return False


def test_typing_past_line_ends_settles_with_a_large_graph(app, tmp_path):
    """The composer's scrollbar used to show and hide in a loop at the end of each
    line ("as needed": showing it re-wraps the text), compounding with every key
    until the window froze; Projects with a ~180-node graph and the chat open."""
    cfg = _cfg(tmp_path)
    conn = db.connect(cfg.db_path)
    seed(conn, n_cards=2)
    big(conn, n_items=20)
    conn.commit()
    win = MainWindow(cfg, conn, auto_refresh=False, watch=False, start="projects",
                     chat_open=True, voice={"settings": _settings(tmp_path), "mic_ok": False})
    win.resize(1500, 940)
    win.show()
    QApplication.processEvents()
    try:
        assert len(win.graph.graph.nodes) > 150
        reloads = []
        win.reload = lambda *a, **kw: reloads.append(1)
        edit = win.chat.composer.edit
        tally = _Tally()
        edit.verticalScrollBar().installEventFilter(tally)
        win.activateWindow()
        win.chat.focus_input()
        text = "Can the servos share the five volt rail with the LiDAR and the STM32? " * 3
        times = []
        for ch in text:
            t0 = time.perf_counter()
            QTest.keyClick(edit, ch)
            QApplication.processEvents()
            times.append(time.perf_counter() - t0)
        assert edit.toPlainText() == text
        assert edit.document().size().height() <= 6   # still below the 6-line cap
        assert tally.shows == 0                       # ... so no scrollbar, never toggled
        assert max(times) < 0.25, max(times)          # was seconds per key near a wrap
        assert reloads == []
        # past the cap it scrolls, with the scrollbar steadily on
        for _ in range(4):
            QTest.keyClicks(edit, text)
        QApplication.processEvents()
        assert edit.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOn
        assert tally.shows <= 1
        edit.clear()
        QApplication.processEvents()
        assert edit.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
    finally:
        win.close()
        conn.close()
