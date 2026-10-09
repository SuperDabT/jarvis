"""Natural-language capture (jarvis.capture) and ``jarvis add``.

The model is a fake: each test gives the JSON form it "returns". Databases are
in memory or under tmp_path (conftest.py refuses the real one).
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import capture as cap
from jarvis import cli, db
from jarvis import items as it
from jarvis.ollama import OllamaError

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 15, 30, tzinfo=LA)  # Friday Oct 9, 3:30 PM
FRI = date(2026, 10, 9)


def form(**kw):
    base = {k: None for k in cap.SCHEMA["required"]}
    base.update(kind="task", priority=0)
    base.update(kw)
    return base


class FakeModel:
    """Returns the given replies in order (an Exception is raised instead)."""

    model = "fake:1b"

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.closed = False

    def chat_json(self, system, user, schema):
        assert schema is cap.SCHEMA
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    def close(self):
        self.closed = True


@pytest.fixture
def conn():
    return db.connect(":memory:")


def run(conn, text, *replies, answers=(), now=NOW, tz=LA):
    model = FakeModel(*replies)
    return cap.capture(conn, text, model, answers=answers, now=now, tz=tz), model


def count(conn):
    return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]


# ---------------------------------------------------------------- date phrases


@pytest.mark.parametrize("phrase, day, hhmm", [
    ("by Thursday", "2026-10-15", None),
    ("Thursday", "2026-10-15", None),
    ("by friday", "2026-10-09", None),          # today is Friday
    ("today", "2026-10-09", None),
    ("tonight", "2026-10-09", None),
    ("tomorrow at 5pm", "2026-10-10", "17:00"),
    ("tomorrow at 5:30 p.m.", "2026-10-10", "17:30"),
    ("noon tomorrow", "2026-10-10", "12:00"),
    ("day after tomorrow", "2026-10-11", None),
    ("tonight at 8", "2026-10-09", "20:00"),
    ("Oct 12", "2026-10-12", None),
    ("October 3rd", "2027-10-03", None),          # already past this year
    ("12th of October", "2026-10-12", None),
    ("in 3 days", "2026-10-12", None),
    ("in a week", "2026-10-16", None),
    ("in two weeks", "2026-10-23", None),
    ("2026-10-12", "2026-10-12", None),
    ("2026-10-12 17:00", "2026-10-12", "17:00"),
    ("at 17:30", None, "17:30"),
    ("at 13", None, "13:00"),
])
def test_phrases_worked_out_here(phrase, day, hhmm):
    r = cap.resolve_phrase(phrase, FRI)
    assert r.recognized and not r.options
    assert (r.day.isoformat() if r.day else None, r.time) == (day, hhmm)


@pytest.mark.parametrize("phrase, values", [
    ("next Thursday", ("2026-10-15", "2026-10-22")),
    ("next Friday", ("2026-10-16", "2026-10-23")),   # today is Friday: not today
    ("next Monday at 9am", ("2026-10-12T09:00", "2026-10-19T09:00")),
    ("tomorrow at 5", ("2026-10-10T05:00", "2026-10-10T17:00")),
    ("at 7", ("07:00", "19:00")),
])
def test_phrases_that_could_mean_two_things(phrase, values):
    r = cap.resolve_phrase(phrase, FRI)
    assert r.recognized and tuple(v for _, v in r.options) == values


@pytest.mark.parametrize("phrase", [
    "next week", "soon", "before the gym", "Feb 30", "2026-02-30", "at 25", "13pm",
    "Monday or Tuesday", "tomorrow and Friday", "whenever", "",
])
def test_phrases_left_to_the_model(phrase):
    assert cap.resolve_phrase(phrase, FRI) == cap.Resolved()


# ---------------------------------------------------------------- the four examples


def test_homework_by_thursday(conn):
    text = "I need to finish my differential equations homework by Thursday."
    res, model = run(conn, text, form(title="Finish differential equations homework",
                                      due_phrase="by Thursday", due_date="2026-10-15"))
    assert res.saved and res.item.kind == "task"
    assert (res.item.title, res.item.due_at, res.item.remind_at) == (
        "Finish differential equations homework", "2026-10-15", None)
    assert cap.summary(res, NOW, LA) == ("Saved task “Finish differential equations homework”"
                                         " · due Thu Oct 15")
    _system, user = model.calls[0]
    assert "Now: Friday 2026-10-09 15:30 (PDT)" in user and text in user
    assert "- lidar: LiDAR Scanner (SuperDabT/stm32-lidar-scanner)" in user


def test_cat_food_reminder(conn):
    res, _ = run(conn, "Remind me to buy cat food tomorrow at 5pm.",
                 form(kind="reminder", title="Buy cat food", remind_phrase="tomorrow at 5pm",
                      remind_date="2026-10-10", remind_time="17:00"))
    assert (res.item.kind, res.item.title, res.item.remind_at, res.item.due_at) == (
        "reminder", "Buy cat food", "2026-10-10T17:00", None)
    assert cap.summary(res, NOW, LA).endswith("· remind tomorrow 5:00 PM")


def test_idea_is_not_filed_under_a_project_it_does_not_name(conn):
    res, _ = run(conn, "Idea: build a local AI server for my STM32 projects.",
                 form(kind="idea", title="Build a local AI server for my STM32 projects",
                      project="lidar", project_phrase="my STM32 projects"))
    assert res.saved and res.item.kind == "idea" and res.item.project_id is None
    assert any("doesn't name it" in n for n in res.interpretation.notes)


def test_study_today_keeps_the_preference_as_body(conn):
    res, _ = run(conn, "I need to study for calc 3 today, preferably before the gym.",
                 form(title="study for calc 3", body="preferably before the gym",
                      due_phrase="today", due_date="2026-10-09"))
    assert (res.item.title, res.item.body, res.item.due_at) == (
        "Study for calc 3", "Preferably before the gym", "2026-10-09")


# ---------------------------------------------------------------- dates


def test_python_date_wins_over_the_models_arithmetic(conn):
    res, _ = run(conn, "finish homework by Thursday",
                 form(title="Finish homework", due_phrase="by Thursday",
                      due_date="2026-10-16"))
    assert res.item.due_at == "2026-10-15"
    assert res.interpretation.notes == ("read “by Thursday” as Thu Oct 15",)


@pytest.mark.parametrize("reply", [
    form(title="Pay rent", due_date="2026-10-15"),                          # no phrase
    form(title="Pay rent", due_phrase="by Friday", due_date="2026-10-09"),  # not in text
    form(title="Pay rent", remind_phrase="at noon", remind_time="12:00"),
])
def test_a_date_the_text_does_not_state_is_never_saved(conn, reply):
    res, _ = run(conn, "pay rent", reply)
    assert res.saved and res.item.due_at is None and res.item.remind_at is None
    assert any("isn't in your text" in n for n in res.interpretation.notes)


def test_unusual_phrasing_uses_the_models_date(conn):
    res, _ = run(conn, "pay rent by the 1st",
                 form(title="Pay rent", due_phrase="by the 1st", due_date="2026-11-01"))
    assert res.item.due_at == "2026-11-01"


@pytest.mark.parametrize("reply, words", [
    (form(title="Pay rent", due_phrase="by the 1st", due_date="2026-10-01"), "already past"),
    (form(title="Pay rent", due_phrase="by the 1st", due_date="2099-11-01"), "too far"),
    (form(title="Pay rent", due_phrase="by the 1st"), "couldn't read"),
])
def test_impossible_dates_ask_instead(conn, reply, words):
    res, _ = run(conn, "pay rent by the 1st", reply)
    assert res.status == "clarify" and words in res.clarification.question
    assert res.clarification.field == "due" and count(conn) == 0


def test_a_reminder_with_no_time_is_saved_without_one(conn):
    res, _ = run(conn, "remind me to call mom",
                 form(kind="reminder", title="Call mom", remind_phrase="remind me"))
    assert res.saved and (res.item.remind_at, res.item.due_at) == (None, None)


def test_a_reminder_day_without_a_time_becomes_the_due_day(conn):
    res, _ = run(conn, "remind me tomorrow to call mom",
                 form(kind="reminder", title="Call mom", remind_phrase="tomorrow",
                      remind_date="2026-10-10"))
    assert (res.item.kind, res.item.due_at, res.item.remind_at) == (
        "reminder", "2026-10-10", None)
    assert "no reminder time given" in res.interpretation.notes


@pytest.mark.parametrize("now, remind", [
    (NOW, "2026-10-09T17:00"),                                    # 3:30 PM: later today
    (datetime(2026, 10, 9, 18, 0, tzinfo=LA), "2026-10-10T17:00"),  # 6 PM: tomorrow
])
def test_a_time_alone_means_its_next_occurrence(conn, now, remind):
    res, _ = run(conn, "remind me to feed the cat at 5pm",
                 form(kind="reminder", title="Feed the cat", remind_phrase="at 5pm",
                      remind_time="17:00"), now=now)
    assert res.item.remind_at == remind


def test_today_depends_on_the_zone(conn):
    """2026-10-10 04:00 UTC is Oct 9 in Los Angeles and Oct 10 in Kolkata."""
    instant = datetime(2026, 10, 10, 4, 0, tzinfo=UTC)
    reply = form(title="Study", due_phrase="today", due_date="2026-10-09")
    la, _ = run(conn, "study today", reply, now=instant, tz=LA)
    ko, _ = run(conn, "study today", dict(reply), now=instant, tz=ZoneInfo("Asia/Kolkata"))
    assert (la.item.due_at, ko.item.due_at) == ("2026-10-09", "2026-10-10")


# ---------------------------------------------------------------- clarification


def test_two_meanings_ask_and_the_answer_settles_it(conn):
    text = "dentist next Thursday"
    reply = form(title="Dentist", due_phrase="next Thursday", due_date="2026-10-15")
    res, _ = run(conn, text, reply)
    assert res.status == "clarify" and count(conn) == 0
    c = res.clarification
    assert c.field == "due" and [v for _, v in c.options] == ["2026-10-15", "2026-10-22"]
    # the model still quotes "next Thursday"; the answer decides
    res2, model = run(conn, text, dict(reply), answers=res.answered("2026-10-22"))
    assert res2.saved and res2.item.due_at == "2026-10-22" and res2.text == text
    assert "You asked: When is it due: “next Thursday”?" in model.calls[0][1]
    assert "The user answered: 2026-10-22" in model.calls[0][1]


def test_the_models_own_question(conn):
    res, _ = run(conn, "stuff", form(title="Stuff", question="What should I note about stuff?"))
    assert res.status == "clarify" and res.clarification.field == "other" and count(conn) == 0
    res2, _ = run(conn, "stuff", form(title="Sort out the garage stuff",
                                      question="Anything else?"),
                  answers=res.answered("sort out the garage"))
    assert res2.saved and res2.item.title == "Sort out the garage stuff"  # asked once only


def test_an_answer_that_settles_nothing_gives_up_after_two(conn):
    reply = form(title="Dentist", due_phrase="next Thursday")
    res, _ = run(conn, "dentist next Thursday", reply)
    for _ in range(cap.MAX_ROUNDS):
        assert res.status == "clarify"
        res, _ = run(conn, "dentist next Thursday", dict(reply), answers=res.answered("dunno"))
    assert res.status == "error" and "still unclear" in res.error and count(conn) == 0


def test_answered_needs_a_question(conn):
    res, _ = run(conn, "pay rent", form(title="Pay rent"))
    with pytest.raises(ValueError):
        res.answered("x")


# ---------------------------------------------------------------- projects and priority


def test_projects(conn):
    res, _ = run(conn, "fix the tracker filter #lidar", form(title="Fix the tracker filter"))
    assert res.item.project_id == it.get_project(conn, "lidar").id   # tag, model said none
    res, _ = run(conn, "fix the tracker for the lidar",
                 form(title="Fix the tracker", project="lidar", project_phrase="the lidar"))
    assert res.item.project_id is not None
    res, _ = run(conn, "read about rockets #school", form(title="Read about rockets"))
    assert res.item.project_id is None and "no project #school" in res.interpretation.notes
    res, _ = run(conn, "read about rockets", form(title="Read about rockets", project="space",
                                                  project_phrase="rockets"))
    assert res.item.project_id is None and any("unknown project" in n
                                               for n in res.interpretation.notes)


def test_priority_needs_words_for_it(conn):
    res, _ = run(conn, "fix the servo asap",
                 form(title="Fix the servo", priority=2, priority_phrase="asap"))
    assert res.item.priority == 2
    res, _ = run(conn, "fix the servo", form(title="Fix the servo", priority=3))
    assert res.item.priority == 0 and any("priority" in n for n in res.interpretation.notes)


# ---------------------------------------------------------------- bad replies, no model


@pytest.mark.parametrize("reply, words", [
    (["not", "a", "dict"], "not a JSON object"),
    (form(kind="note", title="Buy milk"), "kind"),
    (form(title=None), "no title"),
    (form(title="   "), "no title"),
    (form(title=5), "not text"),
    (form(title="Launch the rocket to Mars"), "doesn't match your text"),
    (form(title="Buy milk", due_phrase="tomorrow", due_date="tomorrow"), "not a date"),
    (form(title="Buy milk", due_phrase="tomorrow", due_time="5pm"), "not a time"),
    (form(title="Buy milk", priority=7), "0 to 3"),
    (form(title="Buy milk", priority=True), "0 to 3"),
    (form(title="Buy milk", priority="2"), "0 to 3"),
])
def test_unusable_replies_save_nothing(conn, reply, words):
    res, _ = run(conn, "buy milk tomorrow", reply)
    assert res.status == "error" and words in res.error
    assert res.item is None and count(conn) == 0


def test_model_down_saves_nothing_and_plain_save_works(conn):
    res, _ = run(conn, "buy milk tomorrow", OllamaError("cannot reach Ollama at x"))
    assert res.status == "error" and "isn't available" in res.error and count(conn) == 0
    plain = cap.capture_plain(conn, "  buy milk   tomorrow ", now=NOW, tz=LA)
    assert plain.saved and (plain.item.title, plain.item.due_at) == ("buy milk tomorrow", None)


@pytest.mark.parametrize("text", ["", "   ", None, "x" * (cap.TEXT_MAX + 1)])
def test_bad_text_never_reaches_the_model(conn, text):
    res, model = run(conn, text, form(title="x"))
    assert res.status == "error" and model.calls == [] and count(conn) == 0
    assert not cap.capture_plain(conn, text).saved


def test_capture_never_touches_the_github_mirror(conn):
    db.upsert_issue(conn, {"number": 7, "title": "Track filter", "state": "open"})
    keep = ("issues", "commits", "milestones", "cards", "links", "projects", "chat_messages")
    before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in keep}
    run(conn, "finish #7 track filter #lidar by Thursday",
        form(title="Finish #7 track filter", due_phrase="by Thursday"))
    assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in keep} == before
    assert count(conn) == 1


# ---------------------------------------------------------------- jarvis add


@pytest.fixture
def cli_db(tmp_path, monkeypatch):
    path = tmp_path / "jarvis.db"
    monkeypatch.setenv("JARVIS_DB", str(path))
    return path


def use_model(monkeypatch, *replies):
    model = FakeModel(*replies)
    monkeypatch.setattr(cap, "make_client", lambda cfg: model)
    return model


def saved_titles(path):
    c = db.connect(path)
    try:
        return [i.title for i in it.list_items(c, status="all")]
    finally:
        c.close()


def test_cli_add_saves_and_prints(cli_db, monkeypatch, capsys):
    model = use_model(monkeypatch, form(title="Buy cat food", kind="reminder",
                                        remind_phrase="tomorrow at 5pm"))
    assert cli.main(["add", "remind", "me", "to", "buy", "cat", "food", "tomorrow", "at",
                     "5pm"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Saved reminder “Buy cat food” · remind ")
    assert saved_titles(cli_db) == ["Buy cat food"] and model.closed


def test_cli_add_asks_in_a_terminal(cli_db, monkeypatch, capsys):
    reply = form(title="Dentist", due_phrase="next Thursday")
    use_model(monkeypatch, reply, dict(reply))
    from jarvis.config import Config

    cfg = Config.from_env()
    conn = db.connect(cfg.db_path)
    code = cli._run_add(cfg, conn, "dentist next Thursday", interactive=True,
                        read=lambda prompt: "2")
    conn.close()
    out = capsys.readouterr().out
    assert code == 0 and "When is it due: “next Thursday”?" in out
    assert "  1) " in out and "  2) " in out and out.rstrip().splitlines()[-1].startswith("Saved")
    c = db.connect(cli_db)
    assert [i.due_at for i in it.list_items(c)] == [cap.resolve_phrase(
        "next Thursday", date.today()).options[1][1]]
    c.close()


def test_cli_add_question_without_a_terminal_saves_nothing(cli_db, monkeypatch, capsys):
    use_model(monkeypatch, form(title="Dentist", due_phrase="next Thursday"))
    assert cli.main(["add", "dentist next Thursday"]) == 3
    io = capsys.readouterr()
    assert "When is it due" in io.out and "Not saved" in io.err
    assert saved_titles(cli_db) == []


def test_cli_add_empty_answer_saves_nothing(cli_db, monkeypatch):
    use_model(monkeypatch, form(title="Stuff", question="What about stuff?"))
    from jarvis.config import Config

    cfg = Config.from_env()
    conn = db.connect(cfg.db_path)
    assert cli._run_add(cfg, conn, "stuff", interactive=True, read=lambda p: "") == 1
    conn.close()
    assert saved_titles(cli_db) == []


def test_cli_add_model_down_suggests_plain(cli_db, monkeypatch, capsys):
    use_model(monkeypatch, OllamaError("cannot reach Ollama"))
    assert cli.main(["add", "buy milk"]) == 1
    err = capsys.readouterr().err
    assert "Not saved" in err and "--plain" in err and saved_titles(cli_db) == []
    assert cli.main(["add", "--plain", "buy", "milk"]) == 0
    assert saved_titles(cli_db) == ["buy milk"]


def test_cli_still_has_its_other_commands(cli_db, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    help_text = capsys.readouterr().out
    for cmd in ("sync", "summary", "brief", "checks", "cards", "card", "done", "research",
                "ask", "watch", "add"):
        assert cmd in help_text
