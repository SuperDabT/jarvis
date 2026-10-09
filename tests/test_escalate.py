"""Thinking mode: weak-answer detection, escalation and the template fallback."""
from __future__ import annotations

import pytest

from jarvis import chat, escalate, voice
from jarvis.chat import Answer, Source
from test_chat import NOW, FakeClient, cfg, seeded
from test_voice import RecordingSpeaker

MS = "milestone:Core demo"


class ThinkClient:
    """A deep-tier fake: records the think flag, emits thinking chunks first."""

    def __init__(self, chunks, thinking=("Let me", " reason."), model="gemma-fake"):
        self.model = model
        self.chunks = list(chunks)
        self.thinking = list(thinking)
        self.calls = []
        self.last_thinking_s = None
        self.closed = False

    def chat_stream(self, messages, options=None, think=None, on_thinking=None,
                    max_thinking_s=None):
        self.calls.append({"think": think, "messages": messages,
                           "max_thinking_s": max_thinking_s})
        if think:
            for t in self.thinking:
                if on_thinking:
                    on_thinking(t)
            self.last_thinking_s = 1.5
        yield from self.chunks

    def close(self):
        self.closed = True


def ans(text, **kw):
    a = Answer(text=text, sources=[], unverified_numbers=[], notes=[], model="m", duration=0)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


# ---------------------------------------------------------------- classification


@pytest.mark.parametrize("q,cls", [
    ("Jarvis, what is our task for today for the project?", "planning"),
    ("What should I do today?", "planning"),
    ("What's next?", "planning"),
    ("How should I prioritize the open issues?", "planning"),
    ("What should I focus on this week?", "planning"),
    ("Where do I start?", "planning"),
    ("What's the weather tomorrow?", "out_of_scope"),
    ("Tell me a joke", "out_of_scope"),
    ("What's left for Core demo?", "project"),
    ("Is #31 done?", "project"),
    ("Does the servo rail have news?", "project"),  # a project word wins
])
def test_classify_question(q, cls):
    assert escalate.classify_question(q) == cls


# ---------------------------------------------------------------- weak detection

STEP_REFS = {"fact:next_steps", "#8", "#9", MS}


@pytest.mark.parametrize("q,answer,weak,reasons", [
    ("Is #31 done?", ans(""), True, ["empty"]),
    ("Is #31 done?", ans("   \n"), True, ["empty"]),
    ("Is #31 done?", ans("SOURCES: #31, commits:0"), True, ["empty"]),
    ("Is #31 done?", ans("<think>hmm</think>"), True, ["empty"]),
    ("Is #31 done?", ans("", error="cannot reach Ollama"), True, ["model_error"]),
    ("Is #31 done?", ans("#31 is open but may already be done."), False, []),
    ("When is the demo?", ans("I don't know when the demo is."), True, ["says_unknown"]),
    ("Which MCU clock?", ans("That is not in the data."), True, ["says_unknown"]),
    ("What's the weather tomorrow?",
     ans("Jarvis only knows this project's data, so it can't tell the weather."), False, []),
    ("What's left?", ans("17 issues are left.", unverified_numbers=["17"]), True,
     ["unverified_numbers"]),
    ("Is #99 done?", ans("#99 is closed.", invalid_refs=["#99"]), True, ["unknown_refs"]),
    ("Any work lately?", ans("No commits since Oct 8.", claim_errors=["No commits since Oct 8"]),
     True, ["wrong_commit_date"]),
    ("What should I do today?", ans("Keep going with the tracker."), True,
     ["planning_without_next_steps"]),
    ("What should I do today?", ans("Start with #8 Measure baseline tracking."), False, []),
    ("What should I do today?",
     ans("Follow the suggested steps.", sources=[Source("fact:next_steps", "x")]), False, []),
])
def test_weak_answer_table(q, answer, weak, reasons):
    assert escalate.weak_answer(q, answer, STEP_REFS) == (weak, reasons)


# ---------------------------------------------------------------- orchestrator

GOOD = ["Core demo is due Oct 18, in 10 days. ", "#31 may already be done.\n",
        "SOURCES: milestone:Core demo, #31"]
DEEP_PLAN = ["Suggested by Jarvis: first confirm and close #31, it may already be done. ",
             "Then work on #10 Kalman filter on x/y.\nSOURCES: fact:next_steps, #31, #10"]


def phases_log():
    log = []
    return log, lambda name, info: log.append((name, info))


def test_think_mode_goes_straight_to_deep(tmp_path):
    fast = FakeClient(GOOD)
    deep = ThinkClient(GOOD)
    log, on_phase = phases_log()
    shown = []
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "When is Core demo due?", [], mode="think",
        fast_client=fast, deep_client=deep, on_token=shown.append, on_phase=on_phase, now=NOW)
    assert fast.messages is None  # fast tier never ran
    assert [c["think"] for c in deep.calls] == [True]
    assert [n for n, _ in log] == ["deep", "thinking", "answering", "done"]
    assert log[0][1] == {"model": "gemma-fake", "reason": "think_mode"}
    assert a.tier == "deep" and a.escalated and a.escalation_reasons == ["think_mode"]
    assert a.thinking_s == 1.5
    assert "reason" not in "".join(shown) and "Let me" not in a.text
    assert not deep.closed  # caller's client is not closed


def test_auto_planning_with_fast_tier_goes_deep_directly(tmp_path):
    fast = FakeClient(["SOURCES: commits:0"])
    deep = ThinkClient(DEEP_PLAN)
    log, on_phase = phases_log()
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "Jarvis, what is our task for today for the project?", [],
        mode="auto", fast_client=fast, deep_client=deep, on_phase=on_phase, now=NOW,
        brevity="voice")
    assert fast.messages is None
    assert log[0] == ("deep", {"model": "gemma-fake", "reason": "planning"})
    assert a.tier == "deep" and a.escalation_reasons == ["planning"]
    assert "#31" in a.text and "SUGGESTED NEXT STEPS" in deep.calls[0]["messages"][0]["content"]


def test_auto_fast_ok_stays_fast(tmp_path):
    fast = FakeClient(GOOD)
    deep = ThinkClient(GOOD)
    log, on_phase = phases_log()
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], mode="auto",
        fast_client=fast, deep_client=deep, on_phase=on_phase, now=NOW)
    assert deep.calls == []
    assert [n for n, _ in log] == ["fast", "done"]
    assert a.tier == "fast" and not a.escalated and a.escalation_reasons == []
    assert a.thinking_s is None and a.text.startswith("Core demo is due Oct 18")


def test_auto_weak_fast_escalates_with_phases_in_order(tmp_path):
    fast = FakeClient(["SOURCES: commits:0, issues_closed:0"])
    deep = ThinkClient(GOOD)
    log, on_phase = phases_log()
    shown = []
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], mode="auto",
        fast_client=fast, deep_client=deep, on_token=shown.append, on_phase=on_phase,
        now=NOW)
    assert [n for n, _ in log] == ["fast", "escalating", "discard_fast", "deep", "thinking",
                                   "answering", "done"]
    assert log[1][1] == {"reasons": ["empty"], "spoken": False}
    assert log[3][1]["reason"] == "escalated"
    assert a.tier == "deep" and a.escalated and a.escalation_reasons == ["empty"]
    assert a.text.startswith("Core demo is due Oct 18") and a.first_token_s is not None


def test_escalation_persists_question_and_final_answer_once(tmp_path):
    import json
    conn = seeded()
    escalate.answer_with_escalation(
        conn, cfg(tmp_path), "What's left for Core demo?", [], mode="auto",
        fast_client=FakeClient(["SOURCES: commits:0"]), deep_client=ThinkClient(GOOD),
        now=NOW, session_id="s1")
    rows = conn.execute("SELECT role, content, sources_json FROM chat_messages").fetchall()
    assert [r["role"] for r in rows] == ["user", "assistant"]
    meta = json.loads(rows[1]["sources_json"])
    assert meta["tier"] == "deep" and meta["escalated"] and meta["thinking_s"] == 1.5


def test_deep_also_weak_gives_template_fallback(tmp_path):
    fast = FakeClient(["SOURCES: #31"])
    deep = ThinkClient(["SOURCES: fact:next_steps"])
    log, on_phase = phases_log()
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "Is #31 done?", [], mode="auto", fast_client=fast,
        deep_client=deep, on_phase=on_phase, now=NOW)
    assert "fallback" in [n for n, _ in log]
    assert a.tier == "fallback" and a.escalated and a.escalation_reasons == ["empty"]
    assert a.text.startswith("I couldn't get a reliable answer to that. From the data")
    assert "1. (uncertain) Confirm and close 1 issue that may already be done: #31" in a.text
    assert a.cited_sources and len(a.cited_sources) <= chat.CITED_SOURCES_MAX
    assert a.text.strip() and a.text != "(no answer)"


def test_deep_unreachable_fallback_says_so(tmp_path):
    class Down(ThinkClient):
        def chat_stream(self, messages, options=None, think=None, on_thinking=None,
                        max_thinking_s=None):
            from jarvis.ollama import OllamaError
            raise OllamaError("cannot reach Ollama at x: refused")
            yield  # pragma: no cover

    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What should I do today?", [], mode="think",
        fast_client=None, deep_client=Down([]), now=NOW)
    assert a.tier == "fallback" and "isn't reachable" in a.text and "Confirm and close" in a.text


def test_typed_path_without_fast_tier(tmp_path):
    # auto: the deep model runs without reasoning first; good -> no second pass
    deep = ThinkClient(GOOD)
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], mode="auto",
        fast_client=None, deep_client=deep, now=NOW)
    assert [c["think"] for c in deep.calls] == [False]
    assert a.tier == "fast" and not a.escalated and a.thinking_s is None
    # a planning question is not sent straight to deep without a fast tier...
    deep = ThinkClient(["Keep going.\nSOURCES: milestone:Core demo"])
    log, on_phase = phases_log()
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What should I do today?", [], mode="auto",
        fast_client=None, deep_client=deep, on_phase=on_phase, now=NOW)
    # ...but a weak plan escalates to reasoning; still weak -> template fallback
    assert [c["think"] for c in deep.calls] == [False, True]
    assert log[1] == ("escalating", {"reasons": ["planning_without_next_steps"],
                                     "spoken": False})
    assert a.tier == "fallback" and "Confirm and close 1 issue that may already be done: #31" in a.text


def test_out_of_scope_decline_does_not_escalate(tmp_path):
    fast = FakeClient(["Jarvis only knows this project's data.\n",
                       "SOURCES: fact:activity, milestone:Core demo, #31, #12, #10"])
    deep = ThinkClient(GOOD)
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What's the weather tomorrow?", [], mode="auto",
        fast_client=fast, deep_client=deep, now=NOW)
    assert deep.calls == [] and a.tier == "fast" and not a.escalated
    assert a.cited_sources == [] and a.cited_more_count == 0


def test_unverified_fast_answer_escalates(tmp_path):
    fast = FakeClient(["Core demo has 17 issues left.\nSOURCES: milestone:Core demo"])
    deep = ThinkClient(GOOD)
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], mode="auto",
        fast_client=fast, deep_client=deep, now=NOW)
    assert a.tier == "deep" and a.escalation_reasons == ["unverified_numbers"]
    assert a.unverified_numbers == []


# ---------------------------------------------------------------- voice


def test_voice_filler_spoken_on_escalation(tmp_path):
    log = []
    speaker = RecordingSpeaker(log)
    phases = []
    a = voice.voice_answer(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], FakeClient(["SOURCES: #31"]),
        speaker, now=NOW, mode="auto", deep_client=ThinkClient(GOOD),
        on_phase=lambda n, i: phases.append(n))
    assert speaker.said[0] == voice.THINK_FILLER
    assert speaker.said[1:] == ["Core demo is due Oct 18, in 10 days.",
                                "Issue 31 may already be done."]
    assert a.tier == "deep" and "escalating" in phases


def test_voice_double_check_when_fast_was_spoken(tmp_path):
    speaker = RecordingSpeaker([])
    phases = []
    fast = FakeClient(["Core demo has 17 issues left, plus 23 more. ",
                       "SOURCES: milestone:Core demo"])
    a = voice.voice_answer(
        seeded(), cfg(tmp_path), "What's left for Core demo?", [], fast, speaker, now=NOW,
        mode="auto", deep_client=ThinkClient(GOOD),
        on_phase=lambda n, i: phases.append((n, i)))
    assert speaker.said[0] == "Core demo has 17 issues left, plus 23 more."
    assert speaker.said[1] == voice.DOUBLE_CHECK_FILLER
    assert speaker.said[2] == "Core demo is due Oct 18, in 10 days."
    assert voice.UNVERIFIED_SPOKEN not in speaker.said  # the final answer is verified
    assert ("escalating", {"reasons": ["unverified_numbers"], "spoken": True}) in phases
    assert a.tier == "deep"


def test_voice_planning_goes_deep_and_fallback_is_spoken(tmp_path):
    speaker = RecordingSpeaker([])
    fast = FakeClient(["never used"])
    a = voice.voice_answer(
        seeded(), cfg(tmp_path), "What should I do today?", [], fast, speaker, now=NOW,
        mode="auto", deep_client=ThinkClient(["SOURCES: fact:next_steps"]))
    assert fast.messages is None
    assert speaker.said[0] == voice.THINK_FILLER
    assert a.tier == "fallback"
    assert speaker.said[1].startswith("I couldn't get a reliable answer to that. From the data")
    assert "Issue 31" in speaker.said[1] or "issue 31" in speaker.said[1]


def test_thinking_budget_cut_reanswers_without_reasoning(tmp_path):
    from jarvis.ollama import ThinkingBudgetExceeded

    class Overthinker(ThinkClient):
        def chat_stream(self, messages, options=None, think=None, on_thinking=None,
                        max_thinking_s=None):
            self.calls.append({"think": think, "max_thinking_s": max_thinking_s})
            if think:
                on_thinking("Wait, let me re-check rule 4 again...")
                self.last_thinking_s = 20.4
                raise ThinkingBudgetExceeded("thinking budget exceeded (20 s, 900 chars)")
            yield from self.chunks

    deep = Overthinker(GOOD)
    log, on_phase = phases_log()
    shown = []
    a = escalate.answer_with_escalation(
        seeded(), cfg(tmp_path), "When is Core demo due?", [], mode="think",
        fast_client=None, deep_client=deep, on_token=shown.append, on_phase=on_phase,
        now=NOW, brevity="voice")
    assert deep.calls == [{"think": True, "max_thinking_s": 20.0},
                          {"think": False, "max_thinking_s": None}]
    assert [n for n, _ in log] == ["deep", "thinking", "thinking_cut", "done"]
    assert log[2][1] == {"thinking_s": 20.4}
    assert a.tier == "deep" and a.error is None and a.thinking_s == 20.4
    assert a.text.startswith("Core demo is due Oct 18") and "re-check" not in "".join(shown)
