"""Canvas calendar feed: parsing, importing, reading (scratch database, a fake HTTP
client; nothing leaves the machine and the feed link never appears in output)."""
from __future__ import annotations

from datetime import timedelta
from zoneinfo import ZoneInfo

import httpx
import pytest
from test_intelligence import NOW, add, ask, scratch_conn

from jarvis import assistant, attention, canvas, cli, daemon, planning

LA = ZoneInfo("America/Los_Angeles")
SECRET = "https://school.instructure.com/feeds/calendars/user_SECRETTOKEN123.ics"

# Times in UTC as Canvas sends them: 2026-10-14T06:59Z is Oct 13 23:59 in LA.
FEED = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Instructure//Canvas//EN\r
BEGIN:VEVENT\r
UID:event-assignment-501\r
DTSTART:20261014T065900Z\r
DTEND:20261014T065900Z\r
SUMMARY:Lab 3: Op-amp filters [ECE 201 - Fall 2026]\r
URL;VALUE=URI:https://school.instructure.com/courses/7/assignments/501\r
DESCRIPTION:Submit the report as a PDF\\, with plots.\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-calendar-event-77\r
DTSTART:20261015T170000Z\r
DTEND:20261015T183000Z\r
SUMMARY:Midterm exam [MATH 221 - Fall 2026]\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-assignment-502\r
DTSTART;VALUE=DATE:20261012\r
SUMMARY:Reading response 4 [HIST 110]\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-assignment-503\r
DTSTART:20261020T065900Z\r
SUMMARY:Problem set 6 with a very long title that Canvas folds onto the\r
  next line [MATH 221 - Fall 2026]\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-assignment-400\r
DTSTART:20260801T065900Z\r
SUMMARY:Old summer assignment [ECE 101]\r
END:VEVENT\r
END:VCALENDAR\r
"""


class Http:
    """A fake httpx client: records the URL, returns ``text`` with ``status``."""

    def __init__(self, text=FEED, status=200, error=None):
        self.text, self.status, self.error = text, status, error
        self.urls: list[str] = []

    def get(self, url):
        self.urls.append(url)
        if self.error:
            raise self.error
        return httpx.Response(self.status, text=self.text)


@pytest.fixture
def conn(tmp_path):
    c = scratch_conn(tmp_path)
    yield c
    c.close()


def sync(conn, http=None, now=NOW):
    return canvas.sync(conn, SECRET, now, LA, http or Http())


# ---------------------------------------------------------------- parsing


def test_parse_reads_titles_courses_kinds_and_local_times():
    evs = {e.uid: e for e in canvas.parse_ics(FEED, LA)}
    lab = evs["event-assignment-501"]
    assert (lab.title, lab.course, lab.kind, lab.when) == (
        "Lab 3: Op-amp filters", "ECE 201 - Fall 2026", "assignment", "2026-10-13T23:59")
    assert lab.url == "https://school.instructure.com/courses/7/assignments/501"
    exam = evs["event-calendar-event-77"]
    assert (exam.kind, exam.when, exam.commitment) == ("event", "2026-10-15T10:00", True)
    assert evs["event-assignment-502"].when == "2026-10-12"            # all day
    assert evs["event-assignment-503"].title.endswith("onto the next line")   # unfolded
    assert not lab.commitment and lab.label == "Lab 3: Op-amp filters (ECE 201 - Fall 2026)"
    assert lab.short_id == "501"


def test_tzid_times_are_converted():
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:event-assignment-9\n"
           "DTSTART;TZID=America/New_York:20261013T235900\nSUMMARY:Essay\nEND:VEVENT\n"
           "END:VCALENDAR\n")
    assert canvas.parse_ics(ics, LA)[0].when == "2026-10-13T20:59"


def test_not_a_calendar_is_refused():
    with pytest.raises(canvas.FeedError, match="isn't a calendar"):
        canvas.parse_ics("<html>Log in to Canvas</html>")


def test_broken_events_are_skipped():
    ics = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:x\nSUMMARY:No date\nEND:VEVENT\n"
           "BEGIN:VEVENT\nUID:y\nDTSTART:garbage\nSUMMARY:Bad date\nEND:VEVENT\nEND:VCALENDAR")
    assert canvas.parse_ics(ics) == []


# ---------------------------------------------------------------- importing


def test_import_adds_then_updates_without_duplicating(conn):
    r = sync(conn)
    assert (r.added, r.updated, r.removed, r.total, r.error) == (4, 0, 0, 4, None)  # old one skipped
    again = sync(conn)
    assert (again.added, again.updated, again.removed) == (0, 0, 0)
    moved = Http(FEED.replace("20261014T065900Z", "20261015T065900Z"))
    r = sync(conn, moved)
    assert (r.added, r.updated) == (0, 1)
    assert canvas.find(conn, "501").when == "2026-10-14T23:59"
    assert conn.execute("SELECT COUNT(*) FROM canvas_events").fetchone()[0] == 4


def test_an_entry_that_leaves_the_feed_is_marked_removed_and_can_return(conn):
    sync(conn)
    without = FEED.replace("UID:event-assignment-502", "UID:event-assignment-999")
    r = sync(conn, Http(without))
    assert r.removed == 1
    assert "event-assignment-502" not in {e.uid for e in canvas.upcoming(
        conn, NOW.date(), NOW.date() + timedelta(days=30))}
    sync(conn)
    assert "event-assignment-502" in {e.uid for e in canvas.upcoming(
        conn, NOW.date(), NOW.date() + timedelta(days=30))}


def test_the_users_own_items_are_never_touched(conn):
    mine = add(conn, "Lab 3: Op-amp filters", due="2026-10-13")
    before = [tuple(r) for r in conn.execute("SELECT * FROM items")]
    sync(conn)
    assert [tuple(r) for r in conn.execute("SELECT * FROM items")] == before
    assert mine.id


def test_done_hides_an_entry_until_undone_and_survives_reimport(conn):
    sync(conn)
    canvas.set_done(conn, "501")
    sync(conn)
    week = canvas.upcoming(conn, NOW.date(), NOW.date() + timedelta(days=30))
    assert "event-assignment-501" not in {e.uid for e in week}
    canvas.set_done(conn, "501", False)
    assert "event-assignment-501" in {e.uid for e in canvas.upcoming(
        conn, NOW.date(), NOW.date() + timedelta(days=30))}
    with pytest.raises(LookupError):
        canvas.set_done(conn, "12345")


@pytest.mark.parametrize(("http", "message"), [
    (Http(status=404), "Canvas answered HTTP 404 (the feed link may have been reset)"),
    (Http(error=httpx.ConnectError("boom " + SECRET)), "couldn't reach Canvas (ConnectError)"),
    (Http(text="<html>login</html>"), "the feed isn't a calendar"),
])
def test_failures_change_nothing_and_never_show_the_link(conn, http, message):
    sync(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM canvas_events")]
    r = sync(conn, http)
    assert message in r.error and "SECRET" not in r.summary()
    assert [tuple(r) for r in conn.execute("SELECT * FROM canvas_events")] == before


def test_only_https_links_are_fetched(conn):
    http = Http()
    assert "must be an https:// link" in canvas.sync(conn, "http://x.test/a.ics", NOW, LA,
                                                      http).error
    assert http.urls == []
    canvas.sync(conn, "webcal://school.instructure.com/feeds/a.ics", NOW, LA, http)
    assert http.urls == ["https://school.instructure.com/feeds/a.ics"]


def test_no_link_says_where_to_put_it(conn):
    assert "set JARVIS_CANVAS_ICS in ~/.config/jarvis/env" in canvas.sync(conn, None, NOW).error


def test_feed_url_from_env_or_the_env_file(tmp_path):
    f = tmp_path / "env"
    f.write_text("GITHUB_TOKEN=abc\nexport JARVIS_CANVAS_ICS='https://a.test/f.ics'\n")
    assert canvas.feed_url({}, f) == "https://a.test/f.ics"
    assert canvas.feed_url({"JARVIS_CANVAS_ICS": "https://b.test"}, f) == "https://b.test"
    assert canvas.feed_url({}, tmp_path / "missing") is None


# ---------------------------------------------------------------- reading


def test_morning_summary_includes_canvas_with_courses(conn, tmp_path):
    sync(conn)
    a, model = ask(conn, tmp_path, "Good morning")
    assert not model.requests and not model.classified
    assert a.text.splitlines()[1] == (
        "1. Midterm exam (MATH 221 - Fall 2026) Thu Oct 15, 10:00 AM: 6 days left before it "
        "(today, Sat, Sun, Mon, Tue, Wed).")
    assert "Reading response 4 (HIST 110) is due Mon Oct 12" in a.text
    assert any(s.ref == "canvas:event-calendar-event-77" for s in a.sources)


def test_a_past_canvas_assignment_is_never_called_overdue(conn):
    sync(conn, now=NOW - timedelta(days=10))          # imported earlier
    later = NOW + timedelta(days=4)                    # the reading response (Oct 12) is past
    assert all("overdue" not in s.text for s in attention.picture(conn, later).signals)


def test_study_check_puts_the_canvas_exam_first(conn):
    sync(conn)
    add(conn, "Calculus exam", due="2026-10-16T09:00")
    lines = attention.study_check(conn, NOW).text.splitlines()
    assert lines[1].startswith("- Midterm exam (MATH 221 - Fall 2026), Thu Oct 15, 10:00 AM")
    assert lines[2].startswith("- Calculus exam, Fri Oct 16, 9:00 AM")
    assert "Lab 3: Op-amp filters (ECE 201 - Fall 2026) (Tue Oct 13)" in lines[3]


def test_agenda_lists_canvas_with_the_course(conn):
    sync(conn)
    ag = assistant.build_agenda(conn, NOW.date(), NOW.date() + timedelta(days=6), "this week",
                                NOW)
    text = assistant.agenda_answer(ag).text
    assert "- Lab 3: Op-amp filters (Tue Oct 13, 11:59 PM, ECE 201 - Fall 2026)" in text
    assert "- Midterm exam (Thu Oct 15, 10:00 AM, MATH 221 - Fall 2026)" in text


def test_planner_brief_has_canvas_as_facts_and_refuses_planning_over_the_exam(conn):
    sync(conn)
    add(conn, "Shower the dog")
    b = planning.build_brief(conn, NOW.date(), NOW.date() + timedelta(days=6), NOW.date())
    text = "\n".join(b.lines())
    assert "- Thu 2026-10-15 10:00 Midterm exam (MATH 221 - Fall 2026) [exam]" in text
    assert "- Tue 2026-10-13 23:59 Lab 3: Op-amp filters (ECE 201 - Fall 2026) [assignment]" in text
    dog = next(iter(b.items))
    raw = {"decisions": [{"item_id": dog, "action": "work_on", "date": "2026-10-15",
                          "time": "10:30", "reason": "free then"}],
           "availability": [{"date": "2026-10-15", "start": "08:00", "end": "18:00",
                             "quote": "Thursday 8 to 6"}], "summary": "s"}
    _, errors = planning.check_plan(b, raw, "I'm free Thursday 8 to 6")
    assert any("is when “Midterm exam (MATH 221 - Fall 2026)” is on record" in e
               for e in errors)


def test_without_the_table_readers_still_work(conn):
    conn.execute("DROP TABLE canvas_events")
    assert canvas.upcoming(conn, NOW.date(), NOW.date()) == []
    assert attention.daily_brief(conn, NOW).text


# ---------------------------------------------------------------- service + command


def test_the_daemon_imports_hourly_and_logs_without_the_link(conn, tmp_path):
    from test_chat import cfg

    lines: list[str] = []
    calls: list = []

    def fake_sync(now):
        calls.append(now)
        return canvas.SyncResult(added=2, total=2)

    d = daemon.Daemon(cfg(tmp_path), conn, out=lines.append, canvas_sync=fake_sync)
    assert d.run_canvas() == canvas.SYNC_INTERVAL_S and len(calls) == 1
    assert lines[-1].endswith("canvas: 2 in the feed: 2 new, 0 changed, 0 removed")
    d.canvas_sync = lambda now: canvas.SyncResult(error="Canvas answered HTTP 404")
    assert d.run_canvas() == daemon.CANVAS_RETRY_S
    d.canvas_sync = lambda now: (_ for _ in ()).throw(RuntimeError(SECRET))
    assert d.run_canvas() == daemon.CANVAS_RETRY_S
    assert all("SECRET" not in line for line in lines)


def test_cli_list_done_and_undone(conn, tmp_path, capsys, monkeypatch):
    sync(conn)
    assert cli._run_canvas(conn, "list", None, 30, NOW) == 0
    out = capsys.readouterr().out
    assert "     501  Tue Oct 13, 11:59 PM" in out and "[exam]" in out
    assert cli._run_canvas(conn, "done", "501", 30) == 0
    assert "done: Lab 3: Op-amp filters (ECE 201 - Fall 2026)" in capsys.readouterr().out
    assert cli._run_canvas(conn, "done", None, 30) == 2
    monkeypatch.setattr(canvas, "feed_url", lambda *a, **k: None)
    assert cli._run_canvas(conn, "sync", None, 30, NOW) == 1
    assert "set JARVIS_CANVAS_ICS" in capsys.readouterr().err


@pytest.mark.parametrize(("title", "exam"), [
    ("Chapter Quiz - Week 3", False), ("Video Quiz 3", False), ("Midterm exam", True),
    ("Final Exam", True), ("Finals week review", True), ("Final project report", False),
    ("Implications and Digital Circuits", False),
])
def test_quizzes_are_deadlines_and_only_exams_are_commitments(title, exam):
    assert canvas.CanvasEvent("u", "assignment", title, "COMSC-230", "2026-10-13").commitment \
        is exam


def test_weekly_quizzes_dont_crowd_the_summary_as_exams(conn):
    ics = FEED.replace("Midterm exam [MATH 221 - Fall 2026]", "Video Quiz 3 [COMSC-230]")
    canvas.sync(conn, SECRET, NOW, LA, Http(ics))
    kinds = {s.kind for s in attention.picture(conn, NOW).signals}
    assert "event" not in kinds
