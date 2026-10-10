"""The tool-using assistant: tool selection, clarification, proposals, atomic
apply, limits and the boundaries (scratch database, scripted fake model).

The fake model follows a script of steps (tool calls or an answer) and records
every request, so the tests check what the loop executed, what it sent back to
the model, what the answer cites and what the database holds afterwards.
"""
from __future__ import annotations

import json
import time
from datetime import timedelta

import pytest
from test_chat import cfg
from test_intelligence import LA, NOW, Model, add, scratch_conn

from jarvis import actions, agent, chat, escalate, tools
from jarvis import items as it
from jarvis.ollama import OllamaError

WEEKEND = ("2026-10-10", "2026-10-12")      # Saturday to Monday


class Script:
    """A tool-calling fake model: each step is a list of (tool, args) or a str answer."""

    model = "script"

    def __init__(self, *steps, fail_at: int | None = None):
        self.steps = list(steps)
        self.requests: list[dict] = []
        self.fail_at = fail_at

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

    def tool_results(self, k: int = -1, own: bool = True) -> list[dict]:
        """Tool results in request ``k``; ``own``: only the model's calls, not the
        records Jarvis fetched first (the first ``required`` ones)."""
        msgs = self.requests[k]["messages"]
        out = [json.loads(m["content"]) for m in msgs if m["role"] == "tool"]
        return out[self.required:] if own else out

    @property
    def required(self) -> int:
        first = self.requests[0]["messages"]
        return sum(1 for m in first if m["role"] == "tool")


@pytest.fixture
def conn(tmp_path):
    c = scratch_conn(tmp_path)
    yield c
    c.close()


def run(conn, tmp_path, question, model, history=(), session_id=None):
    return escalate.answer_with_escalation(conn, cfg(tmp_path), question, list(history),
                                           fast_client=None, deep_client=model, now=NOW,
                                           session_id=session_id)


def snapshot(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM items ORDER BY id")]


@pytest.fixture
def week(conn):
    """The weekend example's data: an exam, the dog, project work, GitHub issues."""
    return {"exam": add(conn, "Calculus exam", due="2026-10-12T09:00"),
            "dog": add(conn, "Shower the dog"),
            "bracket": add(conn, "Write up the bracket dimensions", project="lidar"),
            "lab": add(conn, "Lab report", due="2026-10-13")}


# ---------------------------------------------------------------- 1. organize, no writes


def test_1_organize_weekend_gathers_several_sources_and_writes_nothing(conn, tmp_path, week):
    q = ("I have a calculus exam on Monday, need to shower the dog, and want to make progress "
         "on my LiDAR project. Help me organize my weekend.")
    m = Script([("get_agenda", {"start": WEEKEND[0], "end": WEEKEND[1]}),
                ("list_items", {"undated_only": True})],
               [("project_status", {})],
               "Facts: the calculus exam is Mon 9:00. Suggestion: study Saturday, shower the "
               "dog and do the bracket write-up Sunday; #31 is open if you want project work. "
               f"Your free hours are unknown.\nSOURCES: item:{week['exam'].id}, "
               f"item:{week['dog'].id}, #31")
    before = snapshot(conn)
    a = run(conn, tmp_path, q, m)
    assert a.tier == "agent" and a.intent == "planning"
    # Jarvis fetched the agenda, the items and (project work was asked for) the project
    assert a.steps == ["jarvis:get_agenda", "jarvis:list_items", "jarvis:project_status",
                       "get_agenda", "list_items", "project_status"]
    first = m.tool_results(1)                          # what the model read back
    assert [x["title"] for x in first[0]["due"]] == ["Calculus exam"]   # Mon 9:00
    assert first[0]["due"][0]["time"] == "9:00 AM" and first[0]["overdue"] == []
    assert "Shower the dog" in json.dumps(first[1])
    assert any(i["number"] == 31 for i in m.tool_results(2)[-1]["open_issues"])
    assert "BACKLOG" in m.tool_results(2)[-1]["note"]
    assert {s.ref for s in a.cited_sources} == {f"item:{week['exam'].id}",
                                                f"item:{week['dog'].id}", "#31"}
    assert a.action["kind"] == "none"                  # a plan in words: no changes proposed
    assert snapshot(conn) == before


# ---------------------------------------------------------------- 2. schedule: ask, then propose


def test_2_schedule_asks_for_availability_then_proposes_dates(conn, tmp_path, week):
    m = Script([("list_items", {})],
               [("ask_user", {"question": "When are you free on Saturday and Sunday?",
                              "missing": ["free time"]})])
    before = snapshot(conn)
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    assert a.text == "When are you free on Saturday and Sunday?"
    assert a.action["kind"] == "clarify" and a.action["missing"] == ["free time"]
    assert snapshot(conn) == before
    history = [("user", "Schedule these tasks for my weekend"), ("assistant", a.text)]
    m2 = Script([("list_items", {})],
                [("propose_changes", {
                    "summary": "Dog Saturday, bracket Sunday",
                    "changes": [
                        {"op": "set_remind", "item_id": week["dog"].id,
                         "remind": "2026-10-10T10:00", "reason": "you are free Sat 10-14"},
                        {"op": "set_due", "item_id": week["bracket"].id, "due": "2026-10-11",
                         "reason": "Sunday afternoon is free"}],
                    "assumptions": ["Sat 10-14 and Sun afternoon are free, as you said"]})],
                "Proposed: dog Saturday 10:00, bracket Sunday. Apply saves it.\n"
                f"SOURCES: item:{week['dog'].id}, item:{week['bracket'].id}")
    b = run(conn, tmp_path, "Saturday 10 to 2, and Sunday afternoon", m2, history=history)
    p = actions.Proposal.from_dict(b.action["proposal"])
    assert [(c.op, c.item_id, c.due or c.remind) for c in p.changes] == [
        ("set_remind", week["dog"].id, "2026-10-10T10:00"),
        ("set_due", week["bracket"].id, "2026-10-11")]
    assert p.assumptions and snapshot(conn) == before   # proposed, not saved


# ---------------------------------------------------------------- 3 + 4. atomic apply


def propose(conn, week, *extra):
    changes = [{"op": "create", "title": "Review integrals", "due": "2026-10-10",
                "reason": "before the exam"},
               {"op": "set_remind", "item_id": week["dog"].id, "remind": "2026-10-10T10:00",
                "reason": "Saturday"},
               {"op": "set_due", "item_id": week["bracket"].id, "due": "2026-10-11",
                "reason": "Sunday"}, *extra]
    p, errors = actions.build_proposal(conn, changes, NOW, LA, question="q")
    assert not errors
    return p


def test_3_multi_change_proposal_applies_all_and_verifies(conn, week):
    p = propose(conn, week)
    r = actions.apply_proposal(conn, p, NOW, LA)
    assert r.ok and r.message == "Saved and checked 3 changes."
    assert it.get_item(conn, week["dog"].id).remind_at == "2026-10-10T10:00"
    assert it.get_item(conn, week["bracket"].id).due_at == "2026-10-11"
    new = [i for i in it.open_items(conn) if i.title == "Review integrals"]
    assert len(new) == 1 and new[0].due_at == "2026-10-10"
    assert [s["title"] for s in r.stored] == ["Review integrals", "Shower the dog",
                                              "Write up the bracket dimensions"]
    again = actions.apply_proposal(conn, p, NOW, LA)   # targets changed: refused, whole
    assert not again.ok and "edited since" in again.message
    assert len([i for i in it.open_items(conn) if i.title == "Review integrals"]) == 1


def test_4_stale_item_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before_dog = it.get_item(conn, week["dog"].id)
    it.update_item(conn, week["bracket"].id, priority=2,
                   now=NOW + timedelta(minutes=5))     # edited after the proposal
    r = actions.apply_proposal(conn, p, NOW, LA)
    assert not r.ok and "edited since this was proposed" in r.message
    assert it.get_item(conn, week["dog"].id) == before_dog     # the earlier change rolled back
    assert not [i for i in it.open_items(conn) if i.title == "Review integrals"]


def test_4_validation_failure_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before = snapshot(conn)
    r = actions.apply_proposal(conn, p, NOW + timedelta(days=2), LA)   # Saturday is past
    assert not r.ok and "in the past" in r.message
    assert snapshot(conn) == before


def test_4_database_failure_leaves_nothing_applied(conn, week):
    p = propose(conn, week)
    before = snapshot(conn)
    conn.execute(f"""CREATE TEMP TRIGGER boom BEFORE UPDATE OF due_at ON items
                     WHEN NEW.id = {week['bracket'].id}
                     BEGIN SELECT RAISE(ABORT, 'disk said no'); END""")
    r = actions.apply_proposal(conn, p, NOW, LA)
    assert not r.ok and "disk said no" in r.message
    assert snapshot(conn) == before                    # the create and the reminder too
    assert not conn.in_transaction


# ---------------------------------------------------------------- 5. open-ended, no pattern


def test_5_unfamiliar_goal_reaches_the_agent_and_it_picks_tools(conn, tmp_path, week):
    q = "anything I'm forgetting before my trip to Haifa?"
    m = Script([("search_memory", {"query": "trip Haifa"}),
                ("get_agenda", {"start": "2026-10-09", "end": "2026-10-18"})],
               "Nothing about a trip is stored. Dated before Oct 18: the calculus exam (Mon) "
               f"and the lab report (Tue).\nSOURCES: item:{week['exam'].id}, "
               f"item:{week['lab'].id}")
    m.chat_json = lambda system, user, schema: {"intent": "mixed"}
    a = run(conn, tmp_path, q, m)
    assert a.route_by == "model" and a.tier == "agent"
    assert a.steps == ["jarvis:get_agenda", "jarvis:list_items", "search_memory", "get_agenda"]
    assert {s.ref for s in a.cited_sources} == {f"item:{week['exam'].id}",
                                                f"item:{week['lab'].id}"}


# ---------------------------------------------------------------- 6. undated + trade-offs


def test_6_a_plan_can_leave_tasks_undated_with_reasons(conn, tmp_path, week):
    m = Script([("propose_changes", {
        "changes": [{"op": "set_due", "item_id": week["dog"].id, "due": "2026-10-10",
                     "reason": "Saturday is the only free day you named"}],
        "unscheduled": [{"item_id": week["bracket"].id,
                         "reason": "the exam comes first; no time left this weekend"}]})],
        "Proposed: the dog on Saturday. The bracket write-up stays undated: the exam comes "
        f"first.\nSOURCES: item:{week['dog'].id}, item:{week['bracket'].id}")
    a = run(conn, tmp_path, "Schedule my weekend, I'm only free Saturday", m)
    p = actions.Proposal.from_dict(a.action["proposal"])
    assert [c.item_id for c in p.changes] == [week["dog"].id]
    assert p.unscheduled == [{"item_id": week["bracket"].id,
                              "title": "Write up the bracket dimensions",
                              "reason": "the exam comes first; no time left this weekend"}]


# ---------------------------------------------------------------- 7. backlog stays backlog


def test_7_backlog_cannot_become_a_commitment_unless_asked(conn, tmp_path, week):
    fix = {"op": "create", "title": "Fix #31 timestamps", "due": "2026-10-10",
           "reason": "open issue"}
    m = Script([("propose_changes", {"changes": [fix]})],
               "I can't schedule project issues unless you include them.\nSOURCES: none")
    a = run(conn, tmp_path, "Schedule my weekend tasks", m)
    refused = m.tool_results(1)[-1]
    assert refused["proposed"] == [] and "engineering backlog (#31)" in refused["refused"][0]
    assert a.action["kind"] == "none" and not it.list_items(conn, status="all")[4:]
    # a title that is (nearly) an open issue's counts too
    p, errors = actions.build_proposal(conn, [{**fix, "title": "Timestamp each detection "
                                               "when its run closes"}], NOW, LA)
    assert p is None and "#31" in errors[0]
    # the user asked for project work: allowed, and marked as backlog
    p, errors = actions.build_proposal(conn, [fix], NOW, LA, allow_backlog=True)
    assert not errors and p.changes[0].backlog_ref == "#31"
    assert agent.wants_backlog(conn, "plan my weekend, include issue #31")
    assert not agent.wants_backlog(conn, "plan my weekend")


# ---------------------------------------------------------------- 8. fast paths


def test_8_agenda_stays_direct_with_zero_model_calls(conn, tmp_path, week):
    m = Script()
    t = time.monotonic()
    a = run(conn, tmp_path, "What do I need to do today?", m)
    assert a.tier == "direct" and m.requests == [] and time.monotonic() - t < 0.5
    project = Model("#31 is open.\nSOURCES: #31")      # project questions: single answer path
    b = run(conn, tmp_path, "Is #31 done?", project)
    assert b.tier == "fast" and len(project.requests) == 1


# ---------------------------------------------------------------- errors + limits


def test_tool_errors_go_back_to_the_model_and_it_recovers(conn, tmp_path, week):
    m = Script([("no_such_tool", {}), ("get_agenda", {"start": "next week"}),
                ("list_items", {"status": "maybe"}), ("list_items", {"nonsense": 1})],
               [("list_items", {})],
               f"Your open items include the lab report.\nSOURCES: item:{week['lab'].id}")
    a = run(conn, tmp_path, "help me plan my week", m)
    errors = [r["error"] for r in m.tool_results(1)]
    assert "unknown tool 'no_such_tool'" in errors[0]
    assert "start must be a date" in errors[1] and "status must be open or done" in errors[2]
    assert "bad arguments for list_items" in errors[3]
    assert a.tier == "agent" and [s.ref for s in a.cited_sources] == [f"item:{week['lab'].id}"]


def test_iteration_limit_forces_an_answer_without_tools(conn, tmp_path, week):
    loop = [("list_items", {})]
    m = Script(loop, loop, loop, loop, "Here is what I found: four open items.\nSOURCES: none")
    a = run(conn, tmp_path, "help me organize everything", m)
    assert len(m.requests) == agent.MAX_MODEL_CALLS
    assert m.requests[-1]["tools"] is None and all(r["tools"] for r in m.requests[:-1])
    assert a.text.startswith("Here is what I found")


def test_tool_call_limit_and_a_model_that_never_stops(conn, tmp_path, week):
    many = [("list_items", {})] * 6
    m = Script(many, many, many, many, many)          # still calling tools on the last step
    a = run(conn, tmp_path, "help me organize everything", m)
    own = [s for s in a.steps if not s.startswith("jarvis:")]
    assert len(own) == agent.MAX_TOOL_CALLS            # never more than the limit ran
    assert a.text.startswith("I stopped before finishing")
    assert snapshot(conn)                              # nothing broke


def test_model_failure_mid_loop_is_reported_not_raised(conn, tmp_path, week):
    m = Script([("list_items", {})], fail_at=2)
    a = run(conn, tmp_path, "help me plan my weekend", m)
    assert a.error and "isn't reachable" in a.text and a.tier == "agent"


def test_a_claimed_change_gets_a_nothing_saved_note(conn, tmp_path, week):
    m = Script(f"I've scheduled the dog for Saturday.\nSOURCES: item:{week['dog'].id}")
    a = run(conn, tmp_path, "help me plan my weekend", m)
    assert agent.NOT_SAVED_NOTE in a.notes


# ---------------------------------------------------------------- stored proposals


def test_stored_proposal_is_applied_once_from_its_message(conn, tmp_path, week):
    m = Script([("propose_changes", {"changes": [
        {"op": "set_due", "item_id": week["dog"].id, "due": "2026-10-10", "reason": "Sat"}]})],
        f"Proposed: the dog on Saturday.\nSOURCES: item:{week['dog'].id}")
    a = run(conn, tmp_path, "schedule the dog this weekend", m, session_id="s1")
    mid = a.action["message_id"]
    stored, status = actions.load_proposal(conn, mid)
    assert status == "pending" and stored.changes[0].due == "2026-10-10"
    p = actions.Proposal.from_dict(a.action["proposal"])
    r = actions.decide(conn, p, mid, True, NOW, LA)
    assert r.ok and it.get_item(conn, week["dog"].id).due_at == "2026-10-10"
    assert actions.load_proposal(conn, mid)[1] == "applied"
    assert "already applied" in actions.decide(conn, p, mid, True, NOW, LA).message


def test_dismissed_proposal_changes_nothing_and_cannot_be_applied(conn, tmp_path, week):
    m = Script([("propose_changes", {"changes": [
        {"op": "set_due", "item_id": week["dog"].id, "due": "2026-10-10", "reason": "Sat"}]})],
        "Proposed.\nSOURCES: none")
    a = run(conn, tmp_path, "schedule the dog this weekend", m, session_id="s1")
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


def test_tools_never_write(conn, week):
    ctx = tools.ToolContext(conn, cfg_path_free(), NOW, "q")
    before = conn.total_changes
    for name, args in (("get_agenda", {"start": "2026-10-09"}), ("list_items", {}),
                       ("search_memory", {"query": "dog"}), ("project_status", {}),
                       ("list_projects", {}),
                       ("propose_changes", {"changes": [{"op": "set_due",
                                                         "item_id": week["dog"].id,
                                                         "due": "2026-10-10",
                                                         "reason": "r"}]})):
        assert "error" not in json.loads(tools.run_tool(ctx, name, args)), name
    assert conn.total_changes == before and ctx.proposal is not None


def cfg_path_free():
    from pathlib import Path

    from jarvis.config import Config
    return Config(repo="me/proj", db_path=Path("/nonexistent/never-opened.db"))


def test_old_single_answer_path_still_used_without_tool_support(conn, tmp_path, week):
    m = Model(f"Options: the lab report (due Tue).\nSOURCES: item:{week['lab'].id}")
    a = run(conn, tmp_path, "What should I work on?", m)
    assert a.tier == "fast" and len(m.requests) == 1 and chat.FOCUS_TITLE not in m.context


def test_an_answer_to_jarvis_question_continues_the_plan(conn, tmp_path, week):
    """"I'm free Saturday 10 to 2" after "When are you free?" is not a capture."""
    history = [("user", "Schedule these tasks for my weekend"),
               ("assistant", "When are you free on Saturday and Sunday?")]
    m = Script([("propose_changes", {"changes": [
        {"op": "set_remind", "item_id": week["dog"].id, "remind": "2026-10-10T11:00",
         "reason": "inside Sat 10-14"}]})], "Proposed: the dog Saturday 11:00.\nSOURCES: none")
    a = run(conn, tmp_path, "Saturday 10 to 2, Sunday after 3", m, history=history)
    assert a.route_by == "followup" and a.tier == "agent" and a.action["kind"] == "proposal"


def test_inline_refs_are_kept_out_of_the_answer_text(conn, tmp_path, week):
    m = Script([("list_items", {})],
               f"Shower the dog (item:{week['dog'].id}) on Saturday.\nSOURCES: "
               f"item:{week['dog'].id}, item:999")
    a = run(conn, tmp_path, "help me plan my weekend", m)
    assert a.text == "Shower the dog on Saturday."
    assert [s.ref for s in a.cited_sources] == [f"item:{week['dog'].id}"]
    assert a.dropped_refs == ["item:999"]          # only records a tool returned count


def test_model_quirks_are_cleaned_and_a_missing_proposal_is_flagged(conn, tmp_path, week):
    bad = {"op": "set_due", "item_id": week["dog"].id, "due": "2026-10-10T10:00:00",
           "reason": "Sat"}                              # seconds: accepted
    m = Script([("propose_changes", {"changes": [bad]})],
               "<channel|>Proposed: the dog on Saturday at 10. Click Apply.\nSOURCES: none")
    a = run(conn, tmp_path, "schedule the dog this weekend", m)
    assert a.text.startswith("Proposed: the dog") and a.action["kind"] == "proposal"
    m2 = Script([("propose_changes", {"changes": [{**bad, "due": "Saturday"}]})],
                "<|\"|>Proposed: the dog on Saturday. Click Apply.\nSOURCES: none")
    b = run(conn, tmp_path, "schedule the dog this weekend", m2)
    assert b.action["kind"] == "none" and b.text.startswith("Proposed")
    assert any("no changes were proposed (change 1: due 'Saturday'" in n for n in b.notes)


def test_the_final_step_tells_the_model_to_answer(conn, tmp_path, week):
    loop = [("list_items", {})]
    m = Script(loop, loop, loop, loop, "Four open items.\nSOURCES: none")
    run(conn, tmp_path, "help me organize everything", m)
    last = m.requests[-1]["messages"][-1]
    assert last["role"] == "user" and "no more tools" in last["content"]


@pytest.mark.parametrize("text, flagged", [
    ("Would you like me to propose specific times?", False),
    ("I can propose dates once I know when you're free.", False),
    ("I've proposed a plan for Saturday.", True),
    ("To save these, click Apply.", True),
])
def test_only_answers_presenting_changes_get_the_nothing_to_apply_note(conn, tmp_path, week,
                                                                     text, flagged):
    a = run(conn, tmp_path, "help me plan my weekend", Script(text + "\nSOURCES: none"))
    assert any("nothing to apply" in n for n in a.notes) is flagged


# ---------------------------------------------------------------- reliability regressions
# Each case replays a failure seen in the live benchmark (scratchpad/agent/*.log).


class GoalScript(Script):
    """A Script that also classifies the turn's goal (chat_json), counting calls."""

    def __init__(self, *steps, outcome="schedule", start="2026-10-10", end="2026-10-11",
                 **kw):
        super().__init__(*steps, **kw)
        self.goal = {"outcome": outcome, "start": start, "end": end}
        self.classified = 0

    def chat_json(self, system, user, schema):
        self.classified += 1
        return dict(self.goal)


def _dog_change(week, when="2026-10-10T11:00"):
    return {"op": "set_remind", "item_id": week["dog"].id, "remind": when,
            "reason": "inside the Saturday window you gave"}


def test_r1_prose_instead_of_a_requested_schedule_gets_one_reminder(conn, tmp_path, week):
    """gemma: wrote a plan and offered to propose instead of proposing."""
    m = GoalScript("Here's a plan: the dog on Saturday. Would you like me to propose it?",
                   [("propose_changes", {"changes": [_dog_change(week)]})],
                   "Proposed: the dog on Saturday at 11. Apply saves it.\nSOURCES: none")
    a = run(conn, tmp_path, "Schedule the dog for Saturday between 10 and 2", m)
    assert a.goal == "schedule" and a.action["kind"] == "proposal"
    assert agent.SCHEDULE_NUDGE in [x["content"] for x in m.requests[1]["messages"]]


def test_r2_a_schedule_that_never_comes_is_stated(conn, tmp_path, week):
    m = GoalScript("Saturday: dog. Sunday: bracket.",
                   "Saturday: dog. Sunday: bracket. Good luck.")
    a = run(conn, tmp_path, "Schedule my tasks for the weekend, I'm free all day", m)
    assert a.action["kind"] == "none"
    assert any("no schedule was proposed" in n for n in a.notes)


def test_r3_a_question_asked_in_prose_is_the_clarification(conn, tmp_path, week):
    """qwen: asked about availability in prose instead of calling ask_user."""
    m = GoalScript("I found two undated tasks. When are you free this weekend?",
                   "I still need to know: when are you free on Saturday and Sunday?")
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m, session_id="s1")
    assert a.action["kind"] == "clarify"
    assert a.text == "I still need to know: when are you free on Saturday and Sunday?"
    assert a.action["goal"]["outcome"] == "schedule"
    assert a.action["goal"]["request"] == "Schedule these tasks for my weekend"


def test_r4_deadlines_after_the_period_are_always_fetched_and_never_dropped(conn, tmp_path,
                                                                            week):
    """qwen: looked at Saturday-Sunday only and missed Monday's exam."""
    m = GoalScript([("get_agenda", {"start": "2026-10-10", "end": "2026-10-11"})],
                   "Nothing is dated this weekend; do the dog and the bracket.\nSOURCES: none",
                   outcome="plan")
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    first = m.requests[0]["messages"]
    fetched = next(json.loads(x["content"]) for x in first
                   if x["role"] == "tool" and x["tool_name"] == "get_agenda")
    assert [d["title"] for d in fetched["due"]] == ["Calculus exam", "Lab report"]
    assert "Calculus exam (due Mon Oct 12)" in a.notes[-1]
    assert "Lab report (due Tue Oct 13)" in a.notes[-1]


def test_r4_a_complete_answer_gets_no_deadline_note(conn, tmp_path, week):
    m = GoalScript("Study for the calculus exam (Mon 9:00); the lab report is due Tue.\n"
                   "SOURCES: none", outcome="plan")
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert not any("not mentioned" in n for n in a.notes)


def test_r5_these_tasks_are_already_in_view_so_no_which_tasks_question(conn, tmp_path, week):
    """gemma asked "which tasks?": the open items are fetched before the model starts."""
    m = GoalScript([("ask_user", {"question": "When are you free on Saturday and Sunday?"})])
    run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    first = m.requests[0]["messages"]
    items = next(json.loads(x["content"]) for x in first
                 if x["role"] == "tool" and x["tool_name"] == "list_items")
    assert {"Shower the dog", "Write up the bracket dimensions"} <= {
        i["title"] for i in items["items"]}
    assert "don't ask which ones" in first[0]["content"]


def test_r6_the_answer_to_jarvis_question_continues_the_stored_goal(conn, tmp_path, week):
    """The follow-up keeps the original request and outcome, without re-classifying."""
    ask = GoalScript([("ask_user", {"question": "When are you free on Saturday and Sunday?",
                                    "missing": ["free time"]})])
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", ask, session_id="s1")
    assert a.action["kind"] == "clarify" and ask.classified == 1
    m = GoalScript([("propose_changes", {"changes": [_dog_change(week)]})],
                   "Proposed: the dog Saturday 11:00.\nSOURCES: none")
    b = run(conn, tmp_path, "Saturday 10 to 2, Sunday after 3", m, session_id="s1",
            history=[("user", "Schedule these tasks for my weekend"), ("assistant", a.text)])
    assert m.classified == 0 and b.route_by == "followup" and b.goal == "schedule"
    said = next(x["content"] for x in m.requests[0]["messages"]
                if x["role"] == "user" and x["content"].startswith("My request was"))
    assert "Schedule these tasks for my weekend" in said
    assert "You replied: When are you free" in said and "Now I say: Saturday 10 to 2" in said
    assert b.action["kind"] == "proposal"


def test_r6_a_clear_new_question_is_not_taken_as_the_answer(conn, tmp_path, week):
    ask = GoalScript([("ask_user", {"question": "When are you free?"})])
    run(conn, tmp_path, "Schedule these tasks for my weekend", ask, session_id="s1")
    m = GoalScript()
    b = run(conn, tmp_path, "What do I need to do today?", m, session_id="s1")
    assert b.tier == "direct" and m.requests == []


def test_r7_project_status_is_fetched_only_when_project_work_was_asked_for(conn, tmp_path,
                                                                           week):
    m = GoalScript("Saturday: the dog.\nSOURCES: none", outcome="plan")
    a = run(conn, tmp_path, "Help me organize my weekend", m)
    assert "jarvis:project_status" not in a.steps
    m2 = GoalScript("Saturday: the dog; #31 if you want project work.\nSOURCES: none",
                    outcome="plan")
    b = run(conn, tmp_path, "Help me organize my weekend and the LiDAR project", m2)
    assert "jarvis:project_status" in b.steps


def test_r8_goal_classification_failures_fall_back_to_a_plan(conn, tmp_path, week):
    g = agent.classify_goal(Script(), "Help me organize my weekend", [], NOW)
    assert (g.outcome, g.start.isoformat(), g.end.isoformat()) == (
        "plan", "2026-10-10", "2026-10-11")
    bad = GoalScript(start="1999-01-01", end="1999-01-02")
    g = agent.classify_goal(bad, "Schedule my weekend", [], NOW)
    assert g.outcome == "schedule" and g.start.isoformat() == "2026-10-10"
    assert agent.Goal.from_dict({"outcome": "rm -rf", "start": "x", "end": "y"}) is None


def test_r9_recommendations_must_carry_reasons(conn):
    assert "give each step's reason" in agent.AGENT_PROMPT
    assert "don't invent durations, dependencies or urgency" in agent.AGENT_PROMPT


def test_r10_a_reply_after_a_proposal_revises_it_instead_of_leaving_the_task(conn, tmp_path,
                                                                             week):
    """qwen proposed at once; the next message ("I'm free ...") went to "general"."""
    first = GoalScript([("propose_changes", {"changes": [_dog_change(week, "2026-10-10T09:00")]})],
                       "Proposed: the dog Saturday 9:00.\nSOURCES: none")
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", first, session_id="s1")
    assert a.action["kind"] == "proposal" and a.action["goal"]["outcome"] == "schedule"
    m = GoalScript([("propose_changes", {"changes": [_dog_change(week, "2026-10-10T11:00")]})],
                   "Revised: the dog Saturday 11:00.\nSOURCES: none")
    m.chat_json = lambda *a: {"intent": "general"}       # the classifier would say so
    b = run(conn, tmp_path, "I'm free Saturday 10am to 2pm and Sunday after 3pm.", m,
            session_id="s1", history=[("user", "Schedule these tasks for my weekend"),
                                      ("assistant", a.text)])
    assert b.route_by == "followup" and b.tier == "agent" and b.goal == "schedule"
    assert actions.Proposal.from_dict(b.action["proposal"]).changes[0].remind == \
        "2026-10-10T11:00"


def test_r11_a_deadline_named_by_the_start_of_its_title_counts_as_mentioned(conn, week):
    goal = agent.Goal("plan", NOW.date(), NOW.date())
    text = "The calculus exam is Monday and the lab report Tuesday."
    assert agent.missed_deadlines(conn, goal, NOW, text, None) == []
    assert agent.missed_deadlines(conn, goal, NOW, "The calculus exam is Monday.", None) == [
        "Lab report (due Tue Oct 13)"]


def test_r12_an_existing_deadline_is_never_moved_only_planned_with_a_reminder(conn, week):
    """gemma proposed moving the lab report's deadline from Tue to Sun "to prepare"."""
    move = {"op": "set_due", "item_id": week["lab"].id, "due": "2026-10-11", "reason": "prep"}
    p, errors = actions.build_proposal(conn, [move], NOW, LA)
    assert p is None and "already has a deadline (2026-10-13)" in errors[0]
    plan = {"op": "set_remind", "item_id": week["lab"].id, "remind": "2026-10-11T15:00",
            "reason": "work on it Sunday"}
    p, errors = actions.build_proposal(conn, [plan], NOW, LA)
    assert not errors and p.changes[0].remind == "2026-10-11T15:00"


def test_r12_clarifying_questions_lose_raw_references(conn, tmp_path, week):
    m = GoalScript([("ask_user", {"question": f"How long is Shower the dog (item:{week['dog'].id})?"})])
    a = run(conn, tmp_path, "Schedule these tasks for my weekend", m)
    assert a.text == "How long is Shower the dog?"
