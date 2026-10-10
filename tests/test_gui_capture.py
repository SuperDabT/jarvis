"""Quick capture in the window (Ctrl+N), offscreen, with a fake model.

The overlay runs the real capture service (jarvis.capture) on a worker thread
against a database under tmp_path; only the model is fake.
"""
from __future__ import annotations

import os
import threading

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from test_capture import FakeModel, form  # noqa: E402
from test_gui_window import _cfg  # noqa: E402

from jarvis import capture as cap  # noqa: E402
from jarvis import db  # noqa: E402
from jarvis import items as it  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.window import MainWindow  # noqa: E402
from jarvis.ollama import OllamaError  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


class Brain:
    """capture_fn for the window: the real service with a scripted fake model.
    ``gate`` (optional) holds the worker until the test lets it go."""

    def __init__(self, *replies, now=None, tz=None):
        self.model = FakeModel(*replies)
        self.gate: threading.Event | None = None
        self.calls: list[tuple[str, tuple]] = []
        self.now, self.tz = now, tz  # a fixed clock, for captures with dates

    def __call__(self, cfg, text, answers):
        self.calls.append((text, answers))
        if self.gate is not None:
            self.gate.wait(5)
        conn = db.connect(cfg.db_path)
        try:
            return cap.capture(conn, text, self.model, answers=answers, now=self.now, tz=self.tz)
        finally:
            conn.close()


@pytest.fixture
def make(app, tmp_path):
    made = []

    def build(brain):
        cfg = _cfg(tmp_path)
        win = MainWindow(cfg, auto_refresh=False, watch=False, capture_fn=brain)
        win.resize(1500, 940)
        win.show()
        app.processEvents()
        made.append(win)
        return win, cfg

    yield build
    for w in made:
        w.close()


def stored(cfg):
    conn = db.connect(cfg.db_path)
    try:
        return it.list_items(conn, status="all")
    finally:
        conn.close()


def type_and_send(win, text):
    win.capture.input.setText(text)
    QTest.keyClick(win.capture.input, Qt.Key.Key_Return)
    win.capture.wait()


def test_ctrl_n_opens_a_focused_box_and_esc_closes_it_first(make, app):
    win, _ = make(Brain())
    win.set_chat_open(True, focus=True)
    assert win.capture.isHidden()
    QTest.keyClick(win, Qt.Key.Key_N, Qt.KeyboardModifier.ControlModifier)
    app.processEvents()
    assert win.capture.isVisible() and win.capture.input.hasFocus()
    assert win.capture.input.placeholderText().startswith("Add a task, idea or reminder")
    page = win.centralWidget()
    assert win.capture.geometry().right() <= page.width()
    win._on_escape()  # the capture box goes first; the chat stays open
    assert win.capture.isHidden() and win.chat.isVisible()
    win._on_escape()
    assert not win.chat.isVisible()


def test_capture_saves_and_reports(make):
    brain = Brain(form(title="Finish differential equations homework",
                       due_phrase="by Thursday"))
    win, cfg = make(brain)
    win.open_capture()
    type_and_send(win, "I need to finish my differential equations homework by Thursday.")
    [item] = stored(cfg)
    assert item.title == "Finish differential equations homework" and item.due_at
    status = win.capture.status.text()
    assert status.startswith("Saved task “Finish differential equations homework” · due ")
    assert win.capture.status.property("level") == "ok"
    assert win.status_label.text().startswith("Saved task")   # the ticker too
    assert win.capture.input.text() == "" and win.capture.state == "saved"
    assert win.capture.isVisible()  # ready for the next one


def test_input_is_held_while_reading(make):
    brain = Brain(form(title="Buy milk"))
    brain.gate = threading.Event()
    win, cfg = make(brain)
    win.open_capture()
    win.capture.input.setText("buy milk")
    win.capture.submit()
    assert win.capture.busy and win.capture.input.isReadOnly()
    assert win.capture.status.text() == "Reading it…"
    win.capture.submit()  # a second Enter while busy does nothing
    brain.gate.set()
    win.capture.wait()
    assert [i.title for i in stored(cfg)] == ["Buy milk"] and len(brain.calls) == 1


def test_clarification_keeps_the_text_and_takes_an_option(make):
    reply = form(title="Dentist", due_phrase="next Thursday")
    brain = Brain(reply, dict(reply))
    win, cfg = make(brain)
    win.open_capture()
    type_and_send(win, "dentist next Thursday")
    box = win.capture
    assert box.state == "clarify" and stored(cfg) == []
    assert box.question.text() == "When is it due: “next Thursday”?"
    assert box.original.text() == "“dentist next Thursday”" and not box.original.isHidden()
    assert len(box.option_buttons) == 2
    later = box.option_buttons[1]
    later.click()
    box.wait()
    [item] = stored(cfg)
    assert item.title == "Dentist" and item.due_at == box.last.item.due_at
    assert brain.calls[1][0] == "dentist next Thursday"     # the original text, kept
    assert brain.calls[1][1][0].field == "due"


def test_clarification_answered_by_typing(make):
    brain = Brain(form(title="Stuff", question="What about stuff?"),
                  form(title="Sort the garage stuff"))
    win, cfg = make(brain)
    win.open_capture()
    type_and_send(win, "stuff")
    assert win.capture.state == "clarify"
    assert win.capture.input.placeholderText() == "Type an answer, or pick one above"
    type_and_send(win, "sort the garage")
    assert [i.title for i in stored(cfg)] == ["Sort the garage stuff"]
    assert brain.calls[1] == ("stuff", (cap.Answer("other", "What about stuff?",
                                                   "sort the garage"),))


def test_model_down_offers_retry_and_save_as_typed(make):
    brain = Brain(OllamaError("cannot reach Ollama"), form(title="Buy milk"))
    win, cfg = make(brain)
    win.open_capture()
    type_and_send(win, "buy milk")
    box = win.capture
    assert box.state == "error" and stored(cfg) == []
    assert box.status.text().startswith("Not saved: the local model isn't available")
    assert box.status.property("level") == "error"
    assert not box.retry_btn.isHidden() and not box.plain_btn.isHidden()
    box.retry_btn.click()   # works the second time
    box.wait()
    assert [i.title for i in stored(cfg)] == ["Buy milk"]


def test_save_as_typed_needs_no_model(make):
    brain = Brain(OllamaError("cannot reach Ollama"))
    win, cfg = make(brain)
    win.open_capture()
    type_and_send(win, "call the landlord about the heater")
    win.capture.plain_btn.click()
    win.capture.wait()
    [item] = stored(cfg)
    assert (item.title, item.kind, item.due_at) == ("call the landlord about the heater",
                                                     "task", None)
    assert win.capture.status.text().startswith("Saved task")


def test_a_crashing_capture_fn_is_shown_not_raised(make):
    def boom(cfg, text, answers):
        raise RuntimeError("disk full")

    win, cfg = make(boom)
    win.open_capture()
    type_and_send(win, "buy milk")
    assert win.capture.state == "error" and "disk full" in win.capture.status.text()
    assert stored(cfg) == []


def test_capture_fits_the_minimum_window(make, app):
    win, _ = make(Brain())
    win.resize(980, 600)
    app.processEvents()
    win.open_capture()
    app.processEvents()
    page = win.centralWidget()
    g = win.capture.geometry()
    assert g.left() >= 0 and g.right() <= page.width() and g.bottom() <= page.height()
