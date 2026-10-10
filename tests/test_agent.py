"""The tool-using assistant: structured plans, scheduling constraints, follow-up,
proposals, atomic apply, limits and boundaries (scratch database, scripted fake
models).

The fake model follows a script of steps (tool calls or prose) and records every
request, so the tests check what the loop offered, executed and sent back, what
the answer says (rendered by code from the accepted plan) and what the database
holds afterwards. These are guarantees about Jarvis's logic; how often a real
model gets a plan right first time is measured live (scratchpad/agent).
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from test_chat import cfg
from test_intelligence import LA, NOW, Model, add, scratch_conn

from jarvis import actions, agent, escalate, planning, tools
from jarvis import items as it
from jarvis.ollama import OllamaError

SAT, SUN, MON = "2026-10-10", "2026-10-11", "2026-10-12"


class Script:
    """A tool-calling fake model: each step is a list of (tool, args) or a str reply.
    ``goal``: what its chat_json returns for the goal call (None: no chat_json)."""

    model = "script"

    def __init__(self, *steps, goal=None, fail_at=None):
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.fail_at = fail_at
        self.goal = goal
        self.classified = 0

    def chat_json(self, system, user, schema):
        if self.goal is None:
            raise RuntimeError("no classifier")
        self.classified += 1
        return dict(self.goal)

    def chat_tools(self, messages, tools_, think=False, options=None):
        self.requests.append({"messages": [dict(m) for m in messages], "tools": tools_})
        if self.fail_at is not None and len(self.requests) == self.fail_at:
            raise OllamaError("cannot reach Ollama at http://ollama.test: refused")
        step = self.steps.pop(0) if self.steps else "Done.\nSOURCES: none"
        if isinstance(step, str):
            return {"content": step, "tool_calls": []}
        return {"content": "", "tool_calls": [{"name": n, "arguments": a} for n, a in step]}

    def chat_stream(self, *a, **kw):          # the old single-answer path must not run
        raise AssertionError("chat_stream called: the agent should have answered")

    def close(self):
        pass

    def offered(self, k: int = 0) -> list[str]:
        return [t["function"]["name"] for t in self.requests[k]["tools"] or []]

    def results(self, k: int = -1) -> list[dict]:
        """The model's own tool results in request ``k`` (not Jarvis's fetches)."""
        msgs = self.requests[k]["messages"]
        first = len([m for m in self.requests[0]["messages"] if m["role"] == "tool"])
        return [json.loads(m["content"]) for m in msgs if m["role"] == "tool"][first:]

    def user_text(self, k: int = 0) -> str:
        return [m for m in self.requests[k]["messages"] if m["role"] == "user"][-1]["content"]


def plan_goal(outcome="plan", start=SAT, end=SUN):
    return {"outcome": outcome, "start": start, "end": end}


@pytest.fixture
def conn(tmp_path):
    c = scratch_conn(tmp_path)
    yield c
    c.close()


@pytest.fixture
def week(conn):
    """A Friday: an exam Monday, a lab report Tuesday, a present Thursday, an overdue
    reply, two undated tasks, and something far off."""
    return {"exam": add(conn, "Calculus exam", due="2026-10-12T09:00"),
            "dog": add(conn, "Shower the dog"),
            "bracket": add(conn, "Write up the bracket dimensions", project="lidar"),
            "lab": add(conn, "Lab report", due="2026-10-13"),
            "dana": add(conn, "Reply to Dana", due="2026-10-07"),
            "gift": add(conn, "Buy a present for Mom", due="2026-10-15"),
            "far": add(conn, "Renew passport", due="2026-11-20")}


def run(conn, tmp_path, question, model, history=(), session_id=None):
    return escalate.answer_with_escalation(conn, cfg(tmp_path), question, list(history),
                                           fast_client=None, deep_client=model, now=NOW,
                                           session_id=session_id)


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")]


def decide(week, **over):
    """A complete, valid plan for the weekend (day level), with overrides by key."""
    plan = {
        "exam": {"action": "leave", "reason": "the exam is Monday 9:00; keep time before it"},
        "dog": {"action": "work_on", "date": SAT, "reason": "quick, no deadline"},
        "bracket": {"action": "work_on", "date": SUN, "reason": "project progress you asked for"},
        "lab": {"action": "work_on", "date": SUN, "reason": "due Tuesday"},
        "dana": {"action": "work_on", "date": SAT, "reason": "overdue since Wednesday"},
        "gift": {"action": "leave", "reason": "due Thursday; after the weekend"},
    }
    plan.update(over)
    return [{"item_id": week[k].id, **v} for k, v in plan.items() if v is not None]


def submit(week, summary="The exam on Monday shapes the weekend.", **kw):
    over = {k: v for k, v in kw.items() if k in ("exam", "dog", "bracket", "lab", "dana",
                                                  "gift")}
    rest = {k: v for k, v in kw.items() if k not in over}
    return ("submit_plan", {"summary": summary, "decisions": decide(week, **over), **rest})


# ---------------------------------------------------------------- the brief


def test_brief_holds_what_a_weekend_plan_must_account_for(conn, week):
    b = planning.build_brief(conn, NOW.date() + timedelta(days=1), NOW.date() + timedelta(
        days=2), NOW.date())
    status = {x.title: x.status for x in b.items.values()}
    assert status == {"Calculus exam": "due_soon", "Shower the dog": "undated",
                      "Write up the bracket dimensions": "undated", "Lab report": "due_soon",
                      "Reply to Dana": "overdue", "Buy a present for Mom": "due_soon"}
    assert "Renew passport" not in status                    # beyond the horizon
    assert str(b.horizon) == "2026-10-23"                     # today + 14 days


# ---------------------------------------------------------------- organize: a plan


def test_organize_gives_a_complete_provisional_plan_and_saves_nothing(conn, tmp_path, week):
    m = Script([submit(week, suggestions=[{"text": "Keep Sunday evening free to review for "
                                                   "the exam", "reason": "exam Monday 9:00"}])],
               goal=plan_goal("plan"))
    before = snapshot(conn)
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert a.tier == "agent" and a.goal == "plan" and len(m.requests) == 1   # one model call
    assert "ask_user" not in m.offered() and "submit_plan" in m.offered()    # plans don't ask
    brief = m.user_text()
    assert f"item_id {week['exam'].id}: Calculus exam" in brief             # given, not searched
    assert "Renew passport" not in brief
    text = a.text
    assert "Calculus exam due Mon Oct 12, 9:00 AM" in text                   # on record, fixed
    assert "Reply to Dana overdue since Wed Oct 7" in text
    assert "**Saturday Oct 10**" in text and "- Shower the dog: work on it (suggested day, " \
        "not saved)." in text
    assert "Keep Sunday evening free to review for the exam (exam Monday 9:00)" in text
    assert "Availability**: not known, so the days are provisional" in text
    assert a.action["kind"] == "none" and snapshot(conn) == before


# ---------------------------------------------------------------- coverage, not words


def test_a_dropped_item_is_sent_back_by_id_and_the_fix_is_accepted(conn, tmp_path, week):
    m = Script([submit(week, dana=None)], [submit(week)], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    first = m.results(1)[0]
    assert not first["accepted"]
    assert f"not decided: item_id {week['dana'].id} Reply to Dana" in first["errors"][0]
    assert "Reply to Dana: work on it" in a.text


def test_coverage_is_by_id_whatever_the_wording(conn, tmp_path, week):
    """No title matching: a summary calling it "the exam" is fine, the record shows it."""
    m = Script([submit(week, summary="Most of the weekend should go to the exam.")],
               goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert not a.notes and "Calculus exam due Mon Oct 12" in a.text


def test_a_plan_that_never_comes_falls_back_to_the_complete_record(conn, tmp_path, week):
    m = Script("Relax this weekend.", "Really, just relax.", goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert a.text.startswith("I couldn't put together a plan")
    for title in ("Calculus exam", "Shower the dog", "Reply to Dana", "Lab report",
                  "Buy a present for Mom", "Write up the bracket dimensions"):
        assert title in a.text
    assert agent.PLAN_NUDGE in m.user_text(1)


def test_a_plan_turn_never_turns_a_prose_question_into_a_clarification(conn, tmp_path, week):
    m = Script("Which tasks do you mean?", "Which tasks?", goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert a.action["kind"] == "none" and a.text.startswith("I couldn't put together a plan")


# ---------------------------------------------------------------- availability


def test_times_without_stated_availability_are_refused(conn, tmp_path, week):
    """qwen invented 10:00 and 11:00 slots the user never gave."""
    m = Script([submit(week, dog={"action": "work_on", "date": SAT, "time": "10:00",
                                  "reason": "morning"})],
               [submit(week)], goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    err = m.results(1)[0]["errors"][0]
    assert f"no availability the user stated covers {SAT} 10:00" in err
    assert "Availability**: not known" in a.text


def test_availability_must_quote_the_user(conn, tmp_path, week):
    window = {"date": SAT, "start": "10:00", "end": "14:00", "quote": "free 10 to 2"}
    m = Script([submit(week, availability=[window])], goal=plan_goal("schedule"))
    run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    assert "its quote isn't the user's words" in m.results(1)[0]["errors"][0]


def test_stated_windows_become_timed_reminders_inside_them(conn, tmp_path, week):
    q = "Schedule my weekend tasks. I'm free Saturday 10am to 2pm and Sunday after 3pm."
    windows = [{"date": SAT, "start": "10:00", "end": "14:00",
                "quote": "Saturday 10am to 2pm"},
               {"date": SUN, "start": "15:00", "end": "21:00", "quote": "Sunday after 3pm"}]
    plan = submit(week, availability=windows,
                  dog={"action": "set_due", "date": SAT, "time": "10:00", "reason": "Sat window"},
                  dana={"action": "work_on", "date": SAT, "time": "11:00", "reason": "overdue"},
                  bracket={"action": "work_on", "date": SUN, "time": "15:00",
                           "reason": "Sunday window"},
                  lab={"action": "work_on", "date": SUN, "reason": "due Tuesday"})
    m = Script([plan], goal=plan_goal("schedule"))
    before = snapshot(conn)
    a = run(conn, tmp_path, q, m)
    p = actions.Proposal.from_dict(a.action["proposal"])
    assert {(c.op, c.item_id, c.due or c.remind) for c in p.changes} == {
        ("set_due", week["dog"].id, SAT), ("set_remind", week["dog"].id, f"{SAT}T10:00"),
        ("set_remind", week["dana"].id, f"{SAT}T11:00"),
        ("set_remind", week["bracket"].id, f"{SUN}T15:00")}
    assert "Lab report: work on it (suggested day, not saved)" in a.text   # no time: no change
    assert "Availability you gave**: Sat 10:00 AM–2:00 PM; Sun 3:00 PM–9:00 PM" in a.text
    assert snapshot(conn) == before                                       # proposed only


def test_a_time_outside_the_stated_window_is_refused(conn, tmp_path, week):
    q = "Schedule my weekend; I'm only free Saturday 10am to 2pm."
    window = [{"date": SAT, "start": "10:00", "end": "14:00", "quote": "Saturday 10am to 2pm"}]
    m = Script([submit(week, availability=window,
                       dog={"action": "work_on", "date": SAT, "time": "16:00", "reason": "x"},
                       bracket={"action": "work_on", "date": SUN, "reason": "y"},
                       lab={"action": "work_on", "date": SAT, "reason": "z"})],
               goal=plan_goal("schedule"))
    run(conn, tmp_path, q, m)
    errors = " | ".join(m.results(1)[0]["errors"])
    assert f"no availability the user stated covers {SAT} 16:00" in errors
    assert f"the user gave times only for {SAT}; {SUN} isn't one of them" in errors


def test_plans_have_nowhere_to_put_durations():
    schema = tools.TOOLS["submit_plan"][0]["function"]["parameters"]["properties"]
    decision = schema["decisions"]["items"]["properties"]
    assert not any("duration" in k or "hours" in k for k in [*schema, *decision])


# ---------------------------------------------------------------- deadlines


def test_deadlines_are_limits_and_are_never_moved(conn, tmp_path, week):
    m = Script([submit(week, lab={"action": "work_on", "date": "2026-10-14", "reason": "x"},
                       exam={"action": "set_due", "date": SUN, "reason": "earlier"},
                       gift={"action": "work_on", "date": "2026-10-17", "reason": "y"})],
               goal=plan_goal("schedule", SAT, "2026-10-14"))
    run(conn, tmp_path, "Schedule my week", m)
    errors = " | ".join(m.results(1)[0]["errors"])
    assert "“Lab report” is due 2026-10-13: work on it on or before that day" in errors
    assert "“Calculus exam” already has a deadline (2026-10-12T09:00); it isn't moved" in errors
    assert "2026-10-17 for “Buy a present for Mom” is outside the period" in errors


def test_existing_deadline_cannot_be_moved_through_a_proposal_either(conn, week):
    move = {"op": "set_due", "item_id": week["lab"].id, "due": SUN, "reason": "prep"}
    p, errors = actions.build_proposal(conn, [move], NOW, LA)
    assert p is None and "already has a deadline (2026-10-13)" in errors[0]


# ---------------------------------------------------------------- schedule: ask once, continue


def test_schedule_without_availability_may_ask_once_then_must_plan(conn, tmp_path, week):
    ask = Script([("ask_user", {"question": "When are you free on Saturday and Sunday?"})],
                 goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", ask, session_id="s1")
    assert a.action["kind"] == "clarify" and "ask_user" in ask.offered()
    windows = [{"date": SAT, "start": "10:00", "end": "14:00", "quote": "Saturday 10 to 2"}]
    m = Script([submit(week, availability=windows,
                       dog={"action": "work_on", "date": SAT, "time": "10:00", "reason": "w"},
                       bracket={"action": "work_on", "date": SAT, "reason": "w"},
                       lab={"action": "work_on", "date": SAT, "reason": "w"})])
    b = run(conn, tmp_path, "Saturday 10 to 2", m, session_id="s1",
            history=[("user", "Schedule these tasks for my weekend"), ("assistant", a.text)])
    assert m.classified == 0 and b.route_by == "followup" and b.goal == "schedule"
    assert "ask_user" not in m.offered()                    # one question per goal
    assert "My request was: Schedule these tasks for my weekend" in m.user_text()
    assert "Now I say: Saturday 10 to 2" in m.user_text()
    p = actions.Proposal.from_dict(b.action["proposal"])
    assert [(c.op, c.remind) for c in p.changes] == [("set_remind", f"{SAT}T10:00")]


def test_schedule_without_availability_can_be_a_provisional_date_plan(conn, tmp_path, week):
    m = Script([submit(week, dog={"action": "set_due", "date": SAT, "reason": "no deadline"},
                       bracket={"action": "set_due", "date": SUN, "reason": "project"})],
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    p = actions.Proposal.from_dict(a.action["proposal"])
    assert [(c.op, c.due) for c in p.changes] == [("set_due", SAT), ("set_due", SUN)]
    assert "due date Sat Oct 10 (proposed)" in a.text and "provisional" in a.text


def test_a_correction_after_a_proposal_replans_with_the_new_constraint(conn, tmp_path, week):
    first = Script([submit(week, dog={"action": "set_due", "date": SAT, "reason": "r"})],
                   goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", first, session_id="s1")
    assert a.action["kind"] == "proposal"
    windows = [{"date": SUN, "start": "15:00", "end": "20:00", "quote": "only Sunday after 3"}]
    m = Script([submit(week, availability=windows,
                       dog={"action": "set_due", "date": SUN, "time": "15:00", "reason": "Sun"},
                       dana={"action": "work_on", "date": SUN, "reason": "overdue"},
                       bracket={"action": "work_on", "date": SUN, "reason": "Sun"},
                       lab={"action": "work_on", "date": SUN, "reason": "Sun"})])
    m.chat_json = lambda *a: {"intent": "general"}       # the classifier would get it wrong
    b = run(conn, tmp_path, "Actually I'm busy Saturday, only Sunday after 3", m,
            session_id="s1", history=[("user", "Schedule these tasks for my weekend"),
                                      ("assistant", a.text)])
    assert b.route_by == "followup" and b.goal == "schedule"
    p = actions.Proposal.from_dict(b.action["proposal"])
    assert ("set_remind", f"{SUN}T15:00") in [(c.op, c.remind) for c in p.changes]


def test_a_clear_new_question_is_not_taken_as_the_answer(conn, tmp_path, week):
    ask = Script([("ask_user", {"question": "When are you free?"})], goal=plan_goal("schedule"))
    run(conn, tmp_path, "Schedule these tasks for my weekend", ask, session_id="s1")
    m = Script()
    b = run(conn, tmp_path, "What do I need to do today?", m, session_id="s1")
    assert b.tier == "direct" and m.requests == []


def test_a_prose_question_in_a_schedule_turn_is_the_clarification(conn, tmp_path, week):
    m = Script("When are you free?", "I need to know: when are you free this weekend?",
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m, session_id="s1")
    assert a.action["kind"] == "clarify"
    assert a.text == "I need to know: when are you free this weekend?"


def test_clarifying_questions_lose_raw_references(conn, tmp_path, week):
    m = Script([("ask_user", {"question": f"When can Shower the dog (item:{week['dog'].id}) "
                                          "happen?"})], goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    assert a.text == "When can Shower the dog happen?"


# ---------------------------------------------------------------- apply


def test_an_approved_schedule_is_applied_atomically_and_verified(conn, tmp_path, week):
    m = Script([submit(week, dog={"action": "set_due", "date": SAT, "reason": "r"},
                       bracket={"action": "set_due", "date": SUN, "reason": "r"})],
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m, session_id="s1")
    mid = a.action["message_id"]
    p = actions.Proposal.from_dict(a.action["proposal"])
    r = actions.decide(conn, p, mid, True, NOW, LA)
    assert r.ok and r.message == "Saved and checked 2 changes."
    assert it.get_item(conn, week["dog"].id).due_at == SAT
    assert it.get_item(conn, week["bracket"].id).due_at == SUN
    assert "already applied" in actions.decide(conn, p, mid, True, NOW, LA).message


def propose(conn, week):
    changes = [{"op": "create", "title": "Review integrals", "due": SAT, "reason": "exam"},
               {"op": "set_remind", "item_id": week["dog"].id, "remind": f"{SAT}T10:00",
                "reason": "Saturday"},
               {"op": "set_due", "item_id": week["bracket"].id, "due": SUN, "reason": "Sunday"}]
    p, errors = actions.build_proposal(conn, changes, NOW, LA, question="q")
    assert not errors
    return p


def test_stale_item_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before_dog = it.get_item(conn, week["dog"].id)
    it.update_item(conn, week["bracket"].id, priority=2, now=NOW + timedelta(minutes=5))
    r = actions.apply_proposal(conn, p, NOW, LA)
    assert not r.ok and "edited since this was proposed" in r.message
    assert it.get_item(conn, week["dog"].id) == before_dog
    assert not [i for i in it.open_items(conn) if i.title == "Review integrals"]


def test_validation_failure_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before = snapshot(conn)
    r = actions.apply_proposal(conn, p, NOW + timedelta(days=2), LA)
    assert not r.ok and "in the past" in r.message and snapshot(conn) == before


def test_database_failure_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before = snapshot(conn)
    conn.execute(f"""CREATE TEMP TRIGGER boom BEFORE UPDATE OF due_at ON items
                     WHEN NEW.id = {week['bracket'].id}
                     BEGIN SELECT RAISE(ABORT, 'disk said no'); END""")
    r = actions.apply_proposal(conn, p, NOW, LA)
    assert not r.ok and "disk said no" in r.message
    assert snapshot(conn) == before and not conn.in_transaction


def test_dismissed_proposal_changes_nothing_and_cannot_be_applied(conn, tmp_path, week):
    m = Script([submit(week, dog={"action": "set_due", "date": SAT, "reason": "r"})],
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m, session_id="s1")
    before = snapshot(conn)
    p = actions.Proposal.from_dict(a.action["proposal"])
    assert actions.decide(conn, p, a.action["message_id"], False, NOW, LA).ok
    assert "already dismissed" in actions.decide(conn, p, a.action["message_id"], True,
                                                 NOW, LA).message
    assert snapshot(conn) == before


@pytest.mark.parametrize("bad", [None, {}, {"version": 9}, {"version": 1, "id": "x",
                                 "created_at": "t", "changes": [{"op": "drop_table"}]},
                                 {"version": 1, "id": "x", "created_at": "t", "changes": []},
                                 {"version": 1, "id": "x", "created_at": "t",
                                  "changes": [{"op": "set_due", "title": "t"}]}])
def test_malformed_stored_proposals_are_refused(bad):
    with pytest.raises(actions.ProposalError):
        actions.Proposal.from_dict(bad)


def test_transaction_refuses_nesting_and_rolls_back(conn):
    with pytest.raises(it.ItemError), it.transaction(conn), it.transaction(conn):
        pass
    with pytest.raises(RuntimeError), it.transaction(conn):
        it.create_item(conn, "inside", now=NOW, tz=LA)
        raise RuntimeError("stop")
    assert not [i for i in it.list_items(conn, status="all") if i.title == "inside"]


# ---------------------------------------------------------------- backlog


def test_backlog_is_context_only_when_asked_and_never_in_scope(conn, tmp_path, week):
    m = Script([submit(week)], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert "jarvis:project_status" not in a.steps and "#31" not in m.user_text()
    m2 = Script([submit(week)], goal=plan_goal())
    b = run(conn, tmp_path, "Help me organize my weekend and my LiDAR project", m2)
    assert "jarvis:project_status" in b.steps
    issue_31 = 31
    m3 = Script([("submit_plan", {"summary": "s", "decisions": [
        *decide(week), {"item_id": issue_31, "action": "work_on", "date": SAT,
                        "reason": "issue"}]})], goal=plan_goal())
    run(conn, tmp_path, "Help me organize my weekend and the LiDAR project", m3)
    assert any("item_id 31 isn't in scope" in e for e in m3.results(1)[0]["errors"])


def test_backlog_cannot_become_a_commitment_through_a_proposal(conn, week):
    fix = {"op": "create", "title": "Fix #31 timestamps", "due": SAT, "reason": "issue"}
    p, errors = actions.build_proposal(conn, [fix], NOW, LA)
    assert p is None and "engineering backlog (#31)" in errors[0]
    p, errors = actions.build_proposal(conn, [fix], NOW, LA, allow_backlog=True)
    assert not errors and p.changes[0].backlog_ref == "#31"


# ---------------------------------------------------------------- fast paths + limits


def test_agenda_stays_direct_with_zero_model_calls(conn, tmp_path, week):
    m = Script()
    t = time.monotonic()
    a = run(conn, tmp_path, "What do I need to do today?", m)
    assert a.tier == "direct" and m.requests == [] and time.monotonic() - t < 0.5
    m = Script()
    a = run(conn, tmp_path, "what tasks i have for today?", m)        # once went to the planner
    assert a.tier == "direct" and m.requests == [] and m.classified == 0
    project = Model("#31 is open.\nSOURCES: #31")
    b = run(conn, tmp_path, "Is #31 done?", project)
    assert b.tier == "fast" and len(project.requests) == 1


def test_a_plan_takes_one_model_call_plus_the_goal_call(conn, tmp_path, week):
    m = Script([submit(week)], goal=plan_goal())
    run(conn, tmp_path, "Help me organize my weekend", m)
    assert (m.classified, len(m.requests)) == (1, 1)


def test_tool_errors_go_back_to_the_model_and_it_recovers(conn, tmp_path, week):
    m = Script([("no_such_tool", {}), ("get_agenda", {"start": "next week"}),
                ("list_items", {"status": "maybe"})], [submit(week)], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    errors = [r["error"] for r in m.results(1)]
    assert "no_such_tool isn't available now" in errors[0]
    assert "start must be a date" in errors[1] and "status must be open or done" in errors[2]
    assert a.goal == "plan" and "**Saturday Oct 10**" in a.text


def test_a_plan_turn_keeps_submit_plan_on_its_last_step(conn, tmp_path, week):
    loop = [("list_items", {})]
    m = Script(loop, loop, loop, loop, [submit(week)], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert len(m.requests) == agent.MAX_MODEL_CALLS and m.offered(-1) == ["submit_plan"]
    assert "**Saturday Oct 10**" in a.text


def test_tool_call_limit_holds(conn, tmp_path, week):
    many = [("list_items", {})] * 6
    m = Script(many, many, many, many, many, goal=plan_goal("answer"))
    a = run(conn, tmp_path, "help me think about my week", m)
    assert len([s for s in a.steps if not s.startswith("jarvis:")]) == agent.MAX_TOOL_CALLS
    assert a.text.startswith("I stopped before finishing")


def test_model_failure_mid_plan_still_gives_the_record(conn, tmp_path, week):
    m = Script([("list_items", {})], fail_at=2, goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert a.text.startswith("I couldn't put together a plan that fits your records (the local "
                             "model isn't reachable)")
    assert "Calculus exam" in a.text and a.error


# ---------------------------------------------------------------- answer turns


def test_answer_turns_answer_in_prose_and_keep_their_checks(conn, tmp_path, week):
    m = Script([("list_items", {})],
               f"I've scheduled the dog (item:{week['dog'].id}).\nSOURCES: "
               f"item:{week['dog'].id}, item:999", goal=plan_goal("answer"))
    a = run(conn, tmp_path, "help me think about my week", m)
    assert a.text == "I've scheduled the dog." and a.dropped_refs == ["item:999"]
    assert agent.NOT_SAVED_NOTE in a.notes
    assert "propose_changes" in m.offered() and "submit_plan" not in m.offered()


def test_old_single_answer_path_still_used_without_tool_support(conn, tmp_path, week):
    m = Model(f"Options: the lab report (due Tue).\nSOURCES: item:{week['lab'].id}")
    a = run(conn, tmp_path, "What should I work on?", m)
    assert a.tier == "fast" and len(m.requests) == 1


# ---------------------------------------------------------------- goals


def test_goal_classification_failures_fall_back_to_a_plan(conn):
    g = agent.classify_goal(Script(), "Help me organize my weekend", [], NOW)
    assert (g.outcome, str(g.start), str(g.end)) == ("plan", SAT, SUN)
    g = agent.classify_goal(Script(goal={"outcome": "schedule", "start": "1999-01-01",
                                         "end": "1999-01-02"}), "Schedule my weekend", [], NOW)
    assert g.outcome == "schedule" and str(g.start) == SAT
    assert agent.Goal.from_dict({"outcome": "rm -rf", "start": "x", "end": "y"}) is None


def test_tools_never_write(conn, week):
    ctx = tools.ToolContext(conn, cfg_path_free(), NOW, "q")
    before = conn.total_changes
    for name, args in (("get_agenda", {"start": "2026-10-09"}), ("list_items", {}),
                       ("search_memory", {"query": "dog"}), ("project_status", {}),
                       ("list_projects", {}),
                       ("propose_changes", {"changes": [{"op": "set_remind",
                                                         "item_id": week["dog"].id,
                                                         "remind": f"{SAT}T10:00",
                                                         "reason": "r"}]})):
        assert "error" not in json.loads(tools.run_tool(ctx, name, args)), name
    ctx.brief = planning.build_brief(conn, NOW.date(), NOW.date() + timedelta(days=2),
                                     NOW.date())
    ctx.scheduling = True
    out = json.loads(tools.run_tool(ctx, "submit_plan", {"summary": "s",
                                                          "decisions": decide(week)}))
    assert out["accepted"] and conn.total_changes == before


def cfg_path_free():
    from pathlib import Path

    from jarvis.config import Config
    return Config(repo="me/proj", db_path=Path("/nonexistent/never-opened.db"))


def test_the_summary_is_labelled_as_jarvis_suggestion_and_claims_are_flagged(conn, tmp_path,
                                                                            week):
    m = Script([submit(week, summary="I've also scheduled the dog for Saturday.")],
               [submit(week, summary="The dog fits on Saturday.")], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    # a plan saves nothing, so a claim that something was done is refused and fixed
    assert "nothing is saved by a plan" in str(m.requests[1]["messages"][-1]["content"])
    assert a.text.startswith("**Jarvis's read** (a suggestion): The dog fits on Saturday.")
    assert agent.NOT_SAVED_NOTE not in a.notes


# ---------------------------------------------------------------- known failures


def test_the_brief_covers_the_planning_horizon_not_only_the_period(conn, tmp_path, week):
    """1. A plan for today must still see a deadline 12 days out and a reminder
    set for today on an item due next month."""
    late = add(conn, "Scholarship application", due="2026-10-21")
    nudge = add(conn, "Start the thesis outline", due="2026-11-30", remind="2026-10-09T18:00")
    b = planning.build_brief(conn, NOW.date(), NOW.date(), NOW.date())
    assert b.items[late.id].status == "due_soon"
    assert b.items[nudge.id].status == "reminder"
    assert week["far"].id not in b.items                   # beyond today + 14 days
    m = Script([submit(week)], goal=plan_goal(start="2026-10-09", end="2026-10-09"))
    run(conn, tmp_path, "What should I focus on today?", m)
    assert f"item_id {late.id}: Scholarship application" in m.user_text()


def test_exams_are_fixed_commitments_with_the_days_before_them(conn, week):
    """2. The exam is listed as a commitment, with the days of the period before it,
    and each item's allowed days are computed by code."""
    b = planning.build_brief(conn, NOW.date() + timedelta(days=1),
                             NOW.date() + timedelta(days=2), NOW.date())
    text = "\n".join(b.lines())
    assert ("- Mon 2026-10-12 09:00 Calculus exam (item_id 1); days of the period before it: "
            "Sat, Sun") in text
    assert b.items[week["exam"].id].event and not b.items[week["lab"].id].event
    assert f"item_id {week['lab'].id}: Lab report" in text
    assert "allowed days Sat 2026-10-10–Sun 2026-10-11" in text
    dated_exam = add(conn, "Physics midterm", due="2026-10-14")      # no time, still an event
    assert planning.build_brief(conn, NOW.date(), NOW.date(), NOW.date()).items[
        dated_exam.id].event


def test_work_is_never_planned_over_a_commitment_on_record(conn, tmp_path, week):
    q = "Schedule my study time. I'm free Monday 8am to noon."
    window = [{"date": MON, "start": "08:00", "end": "12:00", "quote": "Monday 8am to noon"}]
    m = Script([submit(week, availability=window, dog=None, bracket=None, dana=None,
                       exam={"action": "work_on", "date": MON, "time": "08:00",
                             "reason": "review before it"},
                       lab={"action": "work_on", "date": MON, "time": "09:30",
                            "reason": "due Tuesday"})],
               goal=plan_goal("schedule", MON, MON))
    run(conn, tmp_path, q, m)
    errors = " | ".join(m.results(1)[0]["errors"])
    assert f"{MON} 09:30 is when “Calculus exam” is on record" in errors
    assert "08:00" not in errors                 # preparing for the exam itself is fine


def test_important_items_that_cannot_fit_are_surfaced_not_buried(conn, tmp_path, week):
    """2. Only Sunday is free; the overdue reply is left: it is listed under
    'Needs attention' with the records' fact, not hidden among the rest."""
    q = "Plan my weekend. I'm only free Sunday after 3pm."
    window = [{"date": SUN, "start": "15:00", "end": "23:59", "quote": "Sunday after 3pm"}]
    m = Script([submit(week, availability=window,
                       dog={"action": "exclude", "reason": "no room"},
                       bracket={"action": "exclude", "reason": "no room"},
                       lab={"action": "work_on", "date": SUN, "reason": "due Tuesday"},
                       dana={"action": "leave", "reason": "no room this weekend"})],
               goal=plan_goal())
    a = run(conn, tmp_path, q, m)
    head, _, rest = a.text.partition("**Sunday Oct 11**")
    assert ("**Needs attention, not fitted in this plan**\n- Reply to Dana (overdue since "
            "Wed Oct 7): no room this weekend") in head
    assert "Reply to Dana" not in rest                    # listed once, at the top
    assert "**Not in this plan**\n- Shower the dog: no room" in rest


def test_no_room_before_a_deadline_is_computed_from_the_records(conn, week):
    b = planning.build_brief(conn, NOW.date() + timedelta(days=4),
                             NOW.date() + timedelta(days=5), NOW.date())   # Tue..Wed
    assert b.allowed(b.items[week["lab"].id]) == (b.start, b.start)       # due Tuesday
    early = add(conn, "Hand in the form", due="2026-10-11")
    b = planning.build_brief(conn, NOW.date() + timedelta(days=4),
                             NOW.date() + timedelta(days=5), NOW.date())
    assert b.allowed(b.items[early.id]) is None
    assert b.no_room(b.items[early.id]) == "due Sun Oct 11, before any day of this plan"


@pytest.mark.parametrize(("where", "text", "expected"), [
    ("summary", "Spend 2 hours on the lab report on Sunday.", "how long things take"),
    ("summary", "Do the dog at 4pm, after lunch.", "mentions 16:00"),
    ("reason", "due Wednesday, so Sunday works", "it is due Tuesday 2026-10-13"),
    ("summary", "I've scheduled the dog for Saturday.", "nothing is saved by a plan"),
])
def test_invented_times_durations_and_wrong_days_in_prose_are_refused(conn, tmp_path, week,
                                                                       where, text, expected):
    """3. Free text can't carry availability, durations or deadline days the
    records and the user don't support."""
    kw = ({"summary": text} if where == "summary"
          else {"lab": {"action": "work_on", "date": SUN, "reason": text}})
    m = Script([submit(week, **kw)], [submit(week)], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert expected in " | ".join(m.results(1)[0]["errors"])
    assert a.text.startswith("**Jarvis's read**")               # the fix is accepted


def test_times_and_durations_the_user_gave_may_be_repeated(conn, tmp_path, week):
    q = "Plan my weekend. I have 2 hours free Saturday from 10am."
    window = [{"date": SAT, "start": "10:00", "end": "12:00", "quote": "Saturday from 10am"}]
    m = Script([submit(week, summary="Use your 2 hours from 10am on Saturday.",
                       availability=window,
                       dog={"action": "work_on", "date": SAT, "reason": "fits"},
                       bracket={"action": "work_on", "date": SAT, "reason": "fits"},
                       lab={"action": "work_on", "date": SAT, "reason": "due Tuesday"},
                       dana={"action": "work_on", "date": SAT, "reason": "overdue"})],
               goal=plan_goal())
    a = run(conn, tmp_path, q, m)
    assert len(m.requests) == 1 and "Use your 2 hours from 10am on Saturday." in a.text


def test_a_window_quoting_another_weekday_is_refused(conn, tmp_path, week):
    q = "Schedule my weekend; I'm free Sunday after 3pm."
    window = [{"date": SAT, "start": "15:00", "end": "23:59", "quote": "Sunday after 3pm"}]
    m = Script([submit(week, availability=window)], goal=plan_goal("schedule"))
    run(conn, tmp_path, q, m)
    assert any("the quote names Sunday, but 2026-10-10 is a Saturday" in e
               for e in m.results(1)[0]["errors"])


def test_raw_item_ids_never_reach_the_answer(conn, tmp_path, week):
    m = Script([submit(week, summary="Prioritize item_id 5 (overdue) first.",
                       dana={"action": "work_on", "date": SAT,
                             "reason": "overdue (item_id 5)"})], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert "item_id" not in a.text and "Prioritize (overdue) first." in a.text


@pytest.mark.parametrize("question", [
    "When are you free on Saturday?",
    "What times are you available this weekend?",
])
def test_no_question_about_availability_the_user_already_gave(conn, tmp_path, week, question):
    """4. The user said when they are free; asking again is refused and the model
    plans with it instead."""
    q = "Schedule my weekend tasks, I'm free Saturday afternoon."
    m = Script([("ask_user", {"question": question})], [submit(week)],
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, q, m)
    err = m.results(1)[0]["error"]
    assert "already said when they are free (“Schedule my weekend tasks, I'm free" in err
    assert a.action["kind"] != "clarify" and "**Jarvis's read**" in a.text


def test_no_question_about_what_the_brief_already_holds(conn, tmp_path, week):
    for question in ("Which tasks do you want to schedule?", "When is the lab report due?",
                     "Are you free Saturday? And Sunday?"):
        m = Script([("ask_user", {"question": question})], [submit(week)],
                   goal=plan_goal("schedule"))
        a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
        assert "error" in m.results(1)[0], question
        assert a.action["kind"] != "clarify", question


def test_a_follow_up_answer_is_not_asked_for_again(conn, tmp_path, week):
    """4. After 'Saturday 10 to 2' the model asks in prose when they're free: that
    is not a clarification; it is told to plan with what was said."""
    ask = Script([("ask_user", {"question": "When are you free this weekend?"})],
                 goal=plan_goal("schedule"))
    first = run(conn, tmp_path, "Schedule these tasks for my weekend", ask, session_id="s2")
    m = Script("What time are you free on Sunday?", [submit(week)])
    b = run(conn, tmp_path, "Saturday 10 to 2, Sunday I'm busy", m, session_id="s2",
            history=[("user", "Schedule these tasks for my weekend"),
                     ("assistant", first.text)])
    assert "Don't ask: the user already said when they are free" in m.user_text(1)
    assert b.action["kind"] != "clarify"


def test_a_personal_item_worded_like_an_issue_is_not_backlog(conn, week):
    """5. Matching is by id: a personal task that shares words with issue #31 is
    still the user's own item; only a new item or an explicit #N is backlog."""
    mine = add(conn, "Timestamp each detection run", due=None)
    p, errors = actions.build_proposal(conn, [{"op": "set_due", "item_id": mine.id,
                                               "due": SAT, "reason": "mine"}], NOW, LA)
    assert not errors and p.changes[0].backlog_ref is None
    new = {"op": "create", "title": "Timestamp each detection when its run closes",
           "reason": "x"}
    p, errors = actions.build_proposal(conn, [new], NOW, LA)
    assert p is None and "engineering backlog (#31)" in errors[0]


# ---------------------------------------------------------------- corrections


def test_a_correction_resends_only_what_was_wrong(conn, tmp_path, week):
    """One bad decision: the rest is kept, the model sends just that item, and the
    result is the same plan a single valid submission gives."""
    bad = submit(week, lab={"action": "work_on", "date": "2026-10-14", "reason": "later"})
    fix = ("submit_plan", {"decisions": [{"item_id": week["lab"].id, "action": "work_on",
                                          "date": SUN, "reason": "due Tuesday"}]})
    m = Script([bad], [fix], goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    first = m.results(1)[0]
    assert first["accepted"] is False
    assert first["errors"] == [f"item_id {week['lab'].id}: 2026-10-14 for “Lab report” is "
                               f"outside the period {SAT} to {SUN}; plan inside it or leave "
                               "the item"]
    assert len(first["kept"]) == 5 and f"item_id {week['lab'].id}" not in first["kept"]
    once = Script([submit(week)], goal=plan_goal())
    assert a.text == run(conn, tmp_path, "Help me organize my weekend", once).text


def test_a_missing_item_is_added_on_its_own(conn, tmp_path, week):
    m = Script([submit(week, gift=None)],
               [("submit_plan", {"decisions": [{"item_id": week["gift"].id, "action": "leave",
                                                "reason": "due Thursday; after the weekend"}]})],
               goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert "not decided: item_id" in m.results(1)[0]["errors"][0]
    assert a.text.startswith("**Jarvis's read**") and len(m.requests) == 2


def test_a_bad_reason_drops_only_that_decision(conn, tmp_path, week):
    m = Script([submit(week, lab={"action": "work_on", "date": SUN,
                                  "reason": "due Wednesday"})],
               [("submit_plan", {"decisions": [{"item_id": week["lab"].id, "action": "work_on",
                                                "date": SUN, "reason": "due Tuesday"}]})],
               goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    first = m.results(1)[0]
    assert first["errors"][0].startswith(f"item_id {week['lab'].id}: the reason for")
    assert len(first["kept"]) == 5
    assert "Lab report: work on it (suggested day, not saved). due Tuesday" in a.text


def test_refused_availability_must_be_resent_and_kept_windows_carry_over(conn, tmp_path,
                                                                          week):
    q = "Schedule my weekend; I'm free Saturday 10am to 2pm."
    good = [{"date": SAT, "start": "10:00", "end": "14:00", "quote": "Saturday 10am to 2pm"}]
    bad = [{"date": SAT, "start": "10:00", "end": "14:00", "quote": "all weekend"}]
    sat_only = {"bracket": {"action": "work_on", "date": SAT, "reason": "w"},
                "lab": {"action": "work_on", "date": SAT, "reason": "w"}}
    m = Script([submit(week, availability=bad, **sat_only)],
               [("submit_plan", {"decisions": [], "availability": good})],
               goal=plan_goal("schedule"))
    a = run(conn, tmp_path, q, m)
    assert "the availability" in m.results(1)[0]["note"]
    assert "Availability you gave**: Sat 10:00 AM–2:00 PM" in a.text
    m2 = Script([submit(week, availability=good, dog={"action": "work_on", "date": SAT,
                                                      "time": "16:00", "reason": "x"},
                        **sat_only)],
                [("submit_plan", {"decisions": [{"item_id": week["dog"].id,
                                                 "action": "work_on", "date": SAT,
                                                 "time": "11:00", "reason": "x"}]})],
                goal=plan_goal("schedule"))
    b = run(conn, tmp_path, q, m2)
    assert "the availability" not in m2.results(1)[0]["note"]          # windows were valid
    p = actions.Proposal.from_dict(b.action["proposal"])
    assert [(c.op, c.remind) for c in p.changes] == [("set_remind", f"{SAT}T11:00")]


def test_the_brief_names_each_dates_weekday(conn, week):
    """qwen wrote "due Wednesday" for a Tuesday item: the brief now says Tue."""
    text = "\n".join(planning.build_brief(conn, NOW.date() + timedelta(days=1),
                                          NOW.date() + timedelta(days=2), NOW.date()).lines())
    assert "due Tue 2026-10-13" in text and "due Mon 2026-10-12 09:00" in text


def test_lookups_cannot_use_up_the_calls_a_correction_needs(conn, tmp_path, week):
    """After the first call a plan turn is offered only submit_plan (and ask_user
    for schedules), so a refused plan always has room to be corrected."""
    m = Script([("list_items", {})],
               [submit(week, lab={"action": "work_on", "date": SUN, "reason": "due Wednesday"})],
               [("submit_plan", {"decisions": [{"item_id": week["lab"].id, "action": "work_on",
                                                "date": SUN, "reason": "due Tuesday"}]})],
               goal=plan_goal())
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert "list_items" in m.offered(0) and m.offered(1) == ["submit_plan"]
    assert a.text.startswith("**Jarvis's read**")
