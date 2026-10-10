"""Regression cases for the assistant pipeline: routing, evidence and grounding.

Each case runs the real path (escalate.answer_with_escalation: routing, the
database queries, the context, validation) on a scratch database file. The
model is a fake that records what it was sent and replies with fixed text, so
nothing depends on a live model: the checks are on what Jarvis retrieved, what
it sent, what it answered by itself and what it cited.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from test_chat import cfg, issue

from jarvis import assistant, chat, db, escalate, intent
from jarvis import items as it

LA = ZoneInfo("America/Los_Angeles")
NOW = datetime(2026, 10, 9, 9, 30, tzinfo=LA)          # a Friday morning
REPO = "me/proj"


class Model:
    """A fake model: records each request, replies with ``reply``; can classify."""

    model = "fake"

    def __init__(self, reply: str = "", intent_label: str | None = None):
        self.reply = reply
        self.intent_label = intent_label
        self.requests: list[list[dict]] = []
        self.classified: list[str] = []
        self.last_thinking_s = None

    def chat_stream(self, messages, options=None, think=None, on_thinking=None,
                    max_thinking_s=None):
        self.requests.append(messages)
        yield self.reply

    def chat_json(self, system, user, schema):
        self.classified.append(user)
        if self.intent_label is None:
            raise RuntimeError("model down")
        return {"intent": self.intent_label}

    def close(self):
        pass

    @property
    def system(self) -> str:
        return self.requests[-1][0]["content"]

    @property
    def context(self) -> str:
        return self.requests[-1][-1]["content"]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "x.db"
    assert path.parent == tmp_path                       # a scratch file, never the real DB
    c = db.connect(path)
    c.execute("UPDATE projects SET github_repo = ?", (REPO,))
    db.upsert_milestone(c, {"number": 1, "title": "Core demo", "state": "open",
                            "open_issues": 3, "closed_issues": 1,
                            "due_on": "2026-10-18T07:00:00Z",
                            "html_url": f"https://github.com/{REPO}/milestone/1"})
    issue(c, 31, "Timestamp each detection when its run closes", milestone=1)
    issue(c, 12, "servo_write returns a status", milestone=1)
    issue(c, 10, "Kalman filter on x/y", milestone=1)
    c.commit()
    yield c
    c.close()


def add(conn, title, **kw):
    item = it.create_item(conn, title, now=NOW, tz=LA, **kw)
    conn.commit()
    return item


def ask(conn, tmp_path, question, model=None, **kw):
    model = model or Model("SHOULD NOT BE CALLED")
    a = escalate.answer_with_escalation(conn, cfg(tmp_path), question, [], mode="auto",
                                        fast_client=None, deep_client=model, now=NOW, **kw)
    return a, model


# ---------------------------------------------------------------- A. personal tasks today


def test_a_today_lists_the_personal_task_not_the_project_backlog(conn, tmp_path):
    dog = add(conn, "Shower the dog", due="2026-10-09")
    add(conn, "Write up the bracket dimensions", project="lidar")     # undated project task
    a, model = ask(conn, tmp_path, "What do I need to do today?")
    assert a.intent == "agenda" and a.tier == "direct"
    assert model.requests == []                       # a lookup: answered from the data
    assert "Due today:" in a.text and "- Shower the dog" in a.text
    assert not chat.mentioned_issues(a.text)          # no GitHub issue presented as today's work
    assert "bracket" not in a.text                    # undated backlog isn't listed as due
    assert "1 open item has no date" in a.text
    assert [s.ref for s in a.cited_sources] == [f"item:{dog.id}"]


def test_a_overdue_items_are_separate_and_dated(conn, tmp_path):
    add(conn, "Shower the dog", due="2026-10-09T18:00")
    add(conn, "Reply to Dana", due="2026-10-07")
    a, _ = ask(conn, tmp_path, "what's on my plate today")
    assert a.text.splitlines()[:2] == ["Due today:", "- Shower the dog (6:00 PM)"]
    assert "- Reply to Dana (was due Wed Oct 7, 2 days ago)" in a.text


# ---------------------------------------------------------------- B. project retrieval


def test_b_project_question_gets_project_records_not_personal_ones(conn, tmp_path):
    add(conn, "Shower the dog", due="2026-10-09")
    bracket = add(conn, "Write up the bracket dimensions", project="lidar")
    model = Model("Three Core demo issues are open, among them #31 and #12, plus the "
                  f"bracket write-up.\nSOURCES: #31, #12, item:{bracket.id}, milestone:Core demo")
    a, _ = ask(conn, tmp_path, "What is left on the LiDAR project?", model)
    assert a.intent == "project" and len(model.requests) == 1
    ctx = model.context
    assert "[#31]" in ctx and "[milestone:Core demo]" in ctx       # GitHub data retrieved
    assert f"[item:{bracket.id}] Write up the bracket dimensions" in ctx  # its own task
    assert "Shower the dog" not in ctx                # a personal item isn't project data
    assert {s.ref for s in a.cited_sources} >= {"#31", "#12", f"item:{bracket.id}"}


# ---------------------------------------------------------------- C. nothing due today


def test_c_nothing_due_today_says_so_without_promoting_the_backlog(conn, tmp_path):
    add(conn, "Order TF-Luna spare", due="2026-10-12")
    add(conn, "Look into a cheaper LiPo charger")
    add(conn, "Write up the bracket dimensions", project="lidar")
    a, model = ask(conn, tmp_path, "What do I need to do today?")
    assert model.requests == []
    assert a.text.startswith("Nothing is explicitly due today.")
    assert "LiPo" not in a.text and "bracket" not in a.text        # counted, not listed
    assert "2 open items have no date" in a.text
    assert "the next dated item is Order TF-Luna spare (Mon Oct 12)" in a.text
    assert not chat.mentioned_issues(a.text)


# ---------------------------------------------------------------- D. planning


def test_d_planning_offers_options_from_both_and_leaves_the_choice(conn, tmp_path):
    lab = add(conn, "Lab report", due="2026-10-10", project=None)
    model = Model("Two things have dates: the lab report is due tomorrow. On the project, "
                  "#31 is open in Core demo. Which one comes first is up to you.\n"
                  f"SOURCES: item:{lab.id}, #31")
    before = conn.total_changes
    a, _ = ask(conn, tmp_path, "What should I work on?", model)
    assert a.intent == "planning" and a.tier == "fast" and not a.escalated   # not judged weak
    assert "leave the choice to the user" in model.system
    assert f"[item:{lab.id}] Lab report (due tomorrow" in model.context
    assert "SUGGESTED NEXT STEPS" in model.context    # the project's options are there too
    assert {s.ref for s in a.cited_sources} == {f"item:{lab.id}", "#31"}
    assert conn.total_changes == before               # nothing scheduled or changed


# ---------------------------------------------------------------- E. missing information


def test_e_recall_with_nothing_stored_admits_the_gap(conn, tmp_path):
    a, model = ask(conn, tmp_path, "What did I decide about the battery charger last week?")
    assert a.intent == "recall" and a.tier == "direct" and model.requests == []
    assert a.text.startswith("I couldn't find anything about")
    assert "battery" in a.text and a.sources == []


def test_e_recall_finds_what_the_user_said_and_labels_it(conn, tmp_path):
    conn.executemany(
        "INSERT INTO chat_messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
        [("s1", "user", "I decided to use the TP4056 battery charger.",
          "2026-10-03T19:00:00+00:00"),
         ("s1", "assistant", "Noted the TP4056 battery charger choice.",
          "2026-10-03T19:01:00+00:00")])
    conn.commit()
    mid = conn.execute("SELECT id FROM chat_messages WHERE role = 'user'").fetchone()[0]
    model = Model(f"On Oct 3 you said you'd use the TP4056 charger.\nSOURCES: chat:{mid}")
    a, _ = ask(conn, tmp_path, "What did I decide about the battery charger?", model)
    assert a.intent == "recall"
    assert f"[chat:{mid}] Oct 3, you said: I decided to use the TP4056" in model.context
    assert "Jarvis answered: Noted the TP4056" in model.context      # labelled as an answer
    assert "[#31]" not in model.context               # no project dump for a recall question
    assert [s.ref for s in a.cited_sources] == [f"chat:{mid}"]


# ---------------------------------------------------------------- F. grounding


def test_f_agenda_facts_match_the_records(conn, tmp_path):
    ps = add(conn, "Problem set 5", due="2026-10-09T17:00", project="lidar")
    rent = add(conn, "Rent", kind="reminder", remind="2026-10-15T09:00")
    a, _ = ask(conn, tmp_path, "What do I have due this week?")
    assert a.intent == "agenda" and "Due this week:" in a.text
    assert "- Problem set 5 (today, 5:00 PM, LiDAR Scanner)" in a.text   # date, time, project
    assert "- Rent" not in a.text                     # Oct 15 is next week: not due
    assert "next dated item is Rent (Thu Oct 15)" in a.text
    assert "On GitHub, milestone" not in a.text       # Core demo is Oct 18
    assert [assistant.item_ref(s.ref) for s in a.sources] == [ps.id, rent.id]
    row = it.get_item(conn, ps.id)
    assert row.due_at == "2026-10-09T17:00" and row.status == "open"


def test_f_invented_references_are_dropped(conn, tmp_path):
    lab = add(conn, "Lab report", due="2026-10-10")
    model = Model(f"The lab report is due tomorrow.\nSOURCES: item:{lab.id}, item:999, #77")
    a, _ = ask(conn, tmp_path, "What should I work on?", model)
    assert [s.ref for s in a.cited_sources] == [f"item:{lab.id}"]
    assert set(a.dropped_refs) == {"item:999", "#77"}


# ---------------------------------------------------------------- capture + general


def test_capture_is_handed_to_the_capture_form_and_saves_nothing(conn, tmp_path):
    before = len(it.list_items(conn, status="all"))
    a, model = ask(conn, tmp_path, "Remind me to shower the dog tomorrow.")
    assert a.intent == "capture" and model.requests == []
    assert a.action == {"kind": "capture", "text": "shower the dog tomorrow"}
    assert "nothing is saved until you do" in a.text
    assert len(it.list_items(conn, status="all")) == before


def test_general_question_gets_no_stored_data(conn, tmp_path):
    model = Model("A Kalman filter blends a prediction with noisy measurements.\nSOURCES: none")
    a, _ = ask(conn, tmp_path, "Explain how a complementary filter differs from averaging",
               model)
    assert a.intent == "general"
    assert "[#" not in model.context and "YOUR ITEMS" not in model.context
    assert "without internet access" in model.system
    assert a.notes == [chat.GENERAL_NOTE] and a.cited_sources == []


# ---------------------------------------------------------------- routing


@pytest.mark.parametrize("question, expected", [
    ("What do I need to do today?", "agenda"),
    ("Do I have anything tomorrow?", "agenda"),
    ("What's due this week?", "agenda"),
    ("Any appointments on Friday?", "agenda"),
    ("What should I work on?", "planning"),
    ("How should I prioritize the open issues?", "planning"),
    ("What is left on the LiDAR project?", "project"),
    ("Is #31 done?", "project"),
    ("What did I work on this week?", "project"),          # commits, not a memory
    ("What did I decide last week about the enclosure?", "recall"),
    ("Remind me to shower the dog tomorrow", "capture"),
    ("add a task: buy solder", "capture"),
    ("What's the weather tomorrow?", "general"),
])
def test_rules_route_clear_requests(conn, question, expected):
    r = intent.route_by_rules(question, NOW.date(), conn)
    assert r is not None and r.intent == expected, (question, r)


def test_agenda_timeframes():
    today = NOW.date()                                          # Friday Oct 9
    assert intent.timeframe("today", today)[2] == "today"
    assert intent.timeframe("tomorrow?", today)[:2] == (today.replace(day=10),) * 2
    start, end, span = intent.timeframe("this week", today)
    assert (start.day, end.day, span) == (9, 11, "this week")   # to Sunday
    start, end, _ = intent.timeframe("next week", today)
    assert (start.day, end.day) == (12, 18)
    assert intent.timeframe("on monday", today)[2] == "Monday Oct 12"


def test_unclear_questions_ask_the_model_then_fall_back_to_mixed(conn):
    q = "hmm, thoughts on the bracket?"
    assert intent.route_by_rules(q, NOW.date(), conn) is None
    m = Model(intent_label="agenda")
    r = intent.route(q, NOW, conn, m)
    assert (r.intent, r.by, r.span) == ("agenda", "model", "today") and m.classified == [q]
    r = intent.route(q, NOW, conn, Model(intent_label=None))      # classifier down
    assert (r.intent, r.by) == ("mixed", "fallback")
    assert r.personal and r.project_data
