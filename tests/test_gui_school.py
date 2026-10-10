"""The School page offscreen: course tabs, the tutor (a fake model), check cards,
photos, the narrow sheet, and the real stage page (scratch databases only)."""
from __future__ import annotations

import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from PySide6.QtCore import QEvent, QSettings
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QWidget

from jarvis import canvas, db
from jarvis.config import Config
from jarvis.gui import school_stage, theme
from jarvis.gui.app import make_app
from jarvis.gui.school_view import SchoolView, days_text, photo_b64
from jarvis.school import tutor

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=LA)
CHECK = '```check\n{"q": "When is p → q false?", "a": "Only when p is true and q false."}\n```'


@pytest.fixture(scope="module")
def app():
    return make_app()


OPEN: list[QWidget] = []


@pytest.fixture(autouse=True)
def close_views(app):
    """Every view a test shows is closed after it (a window left open would take
    later tests' mouse events)."""
    yield
    while OPEN:
        w = OPEN.pop()
        w.close()
        w.deleteLater()
    QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "school.db"
    assert path.parent == tmp_path
    c = db.connect(path)
    yield c
    c.close()


class FakeStage(QWidget):
    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []

    def show_scene(self, scene, data):
        self.calls.append(("show", scene, data))

    def clear(self):
        self.calls.append(("clear",))


class Tutor:
    """Streams ``reply`` in chunks like the model and records the requests."""

    def __init__(self, reply="An implication is a promise.\n" + CHECK, error=None):
        self.reply, self.error = reply, error
        self.requests: list[tutor.Request] = []

    def __call__(self, cfg, req, on_token):
        self.requests.append(req)
        if self.error:
            raise self.error
        for i in range(0, len(self.reply), 9):
            on_token(self.reply[i:i + 9])
        text, blocks = tutor.split_blocks(self.reply)
        return tutor.Reply(text, self.reply, blocks)


def make(app, conn, tmp_path, fake=None, width=1400, courses=True):
    if courses:
        canvas.store(conn, [
            canvas.CanvasEvent("q1", "event", "Quiz 3", "COMSC-230-5148", "2026-10-13"),
            canvas.CanvasEvent("x1", "event", "Midterm exam", "COMSC-230-5148", "2026-10-21"),
            canvas.CanvasEvent("h1", "assignment", "Essay", "HIST-17", "2026-10-15")], NOW)
    settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
    v = SchoolView(Config(db_path=tmp_path / "school.db"), conn, theme.tokens_for(),
                   now_fn=lambda: NOW, tutor_fn=fake or Tutor(),
                   stage_factory=FakeStage, settings=settings)
    v.resize(width, 820)
    v.show()
    OPEN.append(v)
    app.processEvents()
    return v


def wait(app, cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)
    assert cond()


def test_tabs_come_from_canvas_with_the_module_and_next_exam(app, conn, tmp_path):
    v = make(app, conn, tmp_path)
    assert list(v.tabs) == ["COMSC-230", "HIST-17", "General"] and v.course == "COMSC-230"
    assert v.module.key == "discrete" and v.module_box.currentText() == "Discrete Math"
    assert v.exam.text() == "Midterm exam · in 12 days"        # an exam beats a quiz
    assert [b.text() for b in v.starter_buttons][0] == "Truth tables"
    assert v.empty.isVisible() and isinstance(v.stage, FakeStage)
    v.tabs["HIST-17"].click()
    assert v.course == "HIST-17" and v.module.key == "general" and v.exam.isHidden()


def test_a_course_can_be_hidden_and_shown_again(app, conn, tmp_path):
    v = make(app, conn, tmp_path)
    v.tabs["HIST-17"].click()
    v.hide_course("HIST-17")
    assert list(v.tabs) == ["COMSC-230", "General"] and v.course == "COMSC-230"
    v.hide_course("General")                                   # General always stays
    assert "General" in v.tabs
    again = make(app, conn, tmp_path)                          # remembered
    assert list(again.tabs) == ["COMSC-230", "General"]
    assert again.hidden_courses() == ["HIST-17"]
    again.unhide_course("HIST-17")
    assert list(again.tabs) == ["COMSC-230", "HIST-17", "General"]
    assert canvas.courses(conn) == ["COMSC-230", "HIST-17"]    # Canvas itself is untouched


def test_general_is_for_any_subject(app, conn, tmp_path):
    v = make(app, conn, tmp_path)
    v.tabs["General"].click()
    assert v.module.key == "general" and v.exam.isHidden()
    assert v.module_box.currentText() == "General"


def test_without_canvas_there_is_one_general_tab(app, conn, tmp_path):
    v = make(app, conn, tmp_path, courses=False)
    assert list(v.tabs) == ["General"] and v.module.key == "general"


def test_a_question_streams_into_text_and_a_check_card(app, conn, tmp_path):
    fake = Tutor()
    v = make(app, conn, tmp_path, fake)
    v.starter_buttons[0].click()
    wait(app, lambda: not v.busy)
    req = fake.requests[0]
    assert (req.question, req.course, req.module.key) == ("Truth tables", "COMSC-230",
                                                         "discrete")
    assert req.upcoming.startswith("Midterm exam in 12 days")
    msg = v.messages[-1]
    assert msg.body.text() == "An implication is a promise."     # the block never shows
    card = msg.checks[0]
    assert card.question.text() == "When is p → q false?" and not card.open
    card.toggle.click()
    assert card.open and "true and q false" in card.answer.text()
    assert not v.empty.isVisible() and v.composer.send_btn.isEnabled() is False


def test_a_failure_shows_in_the_reply_and_the_box_frees(app, conn, tmp_path):
    v = make(app, conn, tmp_path, Tutor(error=RuntimeError("Ollama is down")))
    v.composer.edit.setPlainText("hello")
    v.composer.submit()
    wait(app, lambda: not v.busy)
    assert "Ollama is down" in v.messages[-1].body.text()


def test_a_photo_goes_with_the_next_question_only(app, conn, tmp_path):
    img = QImage(2400, 1200, QImage.Format.Format_RGB32)
    img.fill(QColor("#ffffff"))
    path = tmp_path / "problem.png"
    img.save(str(path))
    fake = Tutor("Read it.")
    v = make(app, conn, tmp_path, fake)
    assert v.composer.attach(path) and v.composer.attach_row.isVisible()
    v.send("Solve this")
    wait(app, lambda: not v.busy)
    assert fake.requests[0].image_b64 and not v.composer.photo
    back = QImage.fromData(__import__("base64").b64decode(fake.requests[0].image_b64))
    assert max(back.width(), back.height()) == 1280                # scaled down
    v.send("And now?")
    wait(app, lambda: not v.busy)
    assert fake.requests[1].image_b64 is None
    assert not v.composer.attach(tmp_path / "missing.png") and photo_b64("nope.png") is None


def test_sessions_are_kept_per_course_and_new_session_clears(app, conn, tmp_path):
    def stored(cfg, req, on_token):        # stores like the real tutor: its own connection
        class M:
            def chat_stream(self, messages, options=None):
                yield "Stored reply.\n" + CHECK
        c = db.connect(cfg.db_path)
        try:
            return tutor.ask(M(), c, req, on_token)
        finally:
            c.close()

    v = make(app, conn, tmp_path, stored)
    v.send("What is a set?")
    wait(app, lambda: not v.busy)
    first = v.session_id
    v.tabs["HIST-17"].click()
    assert v.session_id != first and not v.messages
    v.tabs["COMSC-230"].click()
    assert v.session_id == first and len(v.messages) == 2       # reopened from the DB
    assert v.messages[1].checks[0].question.text() == "When is p → q false?"
    v.new_session()
    assert v.session_id != first and not v.messages and v.empty.isVisible()
    assert v.stage.calls[-1] == ("clear",)


def test_the_module_pick_is_remembered_per_course(app, conn, tmp_path):
    v = make(app, conn, tmp_path)
    i = v.module_box.findData("calc")
    v.module_box.setCurrentIndex(i)
    v.module_box.activated.emit(i)
    assert v.module.key == "calc" and v.starter_buttons[0].text() == "What is the gradient?"
    again = make(app, conn, tmp_path)
    assert again.module.key == "calc"


def test_narrow_hides_the_stage_until_a_sheet_opens(app, conn, tmp_path):
    v = make(app, conn, tmp_path, width=820)
    assert v.narrow and v.tutor_card.isVisible() and not v.stage_card.isVisible()
    v.open_sheet()
    assert v.stage_card.isVisible() and not v.tutor_card.isVisible() and v.back_btn.isVisible()
    assert v.escape() and v.tutor_card.isVisible() and not v.stage_card.isVisible()
    assert not v.escape()
    v.resize(1400, 820)
    app.processEvents()
    assert v.tutor_card.isVisible() and v.stage_card.isVisible() and not v.back_btn.isVisible()


def test_days_text():
    d = NOW.date()
    assert [days_text(d.replace(day=x), d) for x in (9, 10, 14)] == [
        "today", "tomorrow", "in 5 days"]


# ---------------------------------------------------------------- the stage page


def test_the_stage_serves_only_its_folder_and_never_navigates_away():
    assert school_stage.resolve("stage.html")[1] == "text/html"
    assert school_stage.resolve("/vendor/three.module.js")[1] == "text/javascript"
    css, kind = school_stage.resolve("theme.css")
    assert kind == "text/css" and b"--accent: #5b9bff" in css
    for bad in ("../school_view.py", "/etc/passwd", "vendor/../../app.py", "nope.js"):
        assert school_stage.resolve(bad) is None, bad
    assert school_stage.navigation("jarvis-stage:/stage.html", False) == "load"
    assert school_stage.navigation("https://phet.colorado.edu/", True) == "external"
    assert school_stage.navigation("https://evil.example/", False) == "refuse"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
        assert school_stage.navigation(bad, True) == "refuse"


def test_the_real_stage_page_loads_with_its_api(app):
    stage = school_stage.SchoolStage()
    if stage.page is None:
        pytest.skip("Qt WebEngine is not available")
    OPEN.append(stage)
    stage.resize(700, 500)
    stage.show()
    wait(app, lambda: stage.ready, timeout=20)
    got: list = []
    stage.page.runJavaScript(
        "JSON.stringify([typeof window.stage.show, stage.current(), stage.motion,"
        " document.getElementById('empty').classList.contains('shown'),"
        " getComputedStyle(document.body).backgroundColor])", 0, got.append)
    wait(app, lambda: got, timeout=10)
    import json
    kind, current, motion_on, empty_shown, bg = json.loads(got[0])
    assert (kind, current, motion_on, empty_shown) == ("function", None, False, True)
    assert bg == "rgb(16, 26, 44)"                       # the navy card, from theme.css
    assert stage.page.errors == []
