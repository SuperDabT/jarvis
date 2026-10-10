"""School's tutor without Qt: blocks, the prompt, sessions, modules, Canvas courses
(scratch databases only, a fake model)."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis import canvas, db
from jarvis.school import modules, tutor

LA = ZoneInfo("America/Los_Angeles")
TODAY = date(2026, 10, 9)
CHECK = '```check\n{"q": "Is p → q true when p is false?", "a": "Yes, vacuously."}\n```'


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "school.db"
    assert path.parent == tmp_path            # a scratch file, never the real DB
    c = db.connect(path)
    yield c
    c.close()


def req(question="What is an implication?", **kw):
    base = dict(course="COMSC-230", module=modules.get("discrete"), question=question,
                session_id="school:COMSC-230:t1", today=TODAY)
    return tutor.Request(**{**base, **kw})


class Model:
    """A fake streaming model: yields ``reply`` in small chunks, records messages."""

    def __init__(self, reply: str):
        self.reply = reply
        self.messages: list[list[dict]] = []

    def chat_stream(self, messages, options=None, **_):
        self.messages.append(messages)
        for i in range(0, len(self.reply), 7):
            yield self.reply[i:i + 7]


# ---------------------------------------------------------------- blocks


def test_blocks_leave_the_text_and_parse():
    text, blocks = tutor.split_blocks(f"An implication p → q is false only when…\n\n{CHECK}")
    assert text == "An implication p → q is false only when…"
    assert [(b.kind, b.data) for b in blocks] == [
        ("check", {"q": "Is p → q true when p is false?", "a": "Yes, vacuously."})]


def test_bad_blocks_are_reported_not_shown():
    text, blocks = tutor.split_blocks('Hi\n```check\n{"a": "no question"}\n```\n'
                                      '```stage\nnot json\n```')
    assert text == "Hi"
    assert [b.data for b in blocks] == [None, None]
    assert "question" in blocks[0].error and "not JSON" in blocks[1].error


def test_ordinary_code_stays_and_a_cut_off_block_is_dropped():
    code = "Run:\n```python\nprint(1)\n```"
    assert tutor.split_blocks(code) == (code, [])
    assert tutor.split_blocks('Text\n```check\n{"q": "half')[0] == "Text"


@pytest.mark.parametrize("partial, shown", [
    ("p → q means", "p → q means"),
    ("Done.\n``", "Done.\n``"),                     # could still be a code block
    ("Done.\n```", "Done.\n"),                      # a fence: hidden until we know
    ("Done.\n```che", "Done.\n"),
    ('Done.\n```check\n{"q": "Is', "Done.\n"),
    ("Done.\n```python\nx = 1", "Done.\n```python\nx = 1"),
    (f"Done.\n{CHECK}\nMore", "Done.\n\nMore"),
])
def test_streaming_hides_blocks_as_they_are_written(partial, shown):
    assert tutor.streaming_text(partial) == shown


@pytest.mark.parametrize("tex, shown", [
    (r"Prove $1 + 2 + ... + n = \frac{n(n + 1)}{2}$ for all $n \geq 1$.",
     "Prove 1 + 2 + ... + n = (n(n + 1))/2 for all n ≥ 1."),
    (r"the codomain is $\mathbb{Z}$", "the codomain is ℤ"),
    (r"$x = \frac{y - 1}{2}$", "x = (y - 1)/2"),
    (r"$x^2 + y^{10}$ and $a_{k+1}$", "x² + y¹⁰ and aₖ₊₁"),
    (r"$p \to q \equiv \neg p \lor q$", "p → q ≡ ¬ p ∨ q"),
    (r"\(\forall x \in A\)", "∀ x ∈ A"),
    ("I owe you $10 (q). It costs $5 and $10.", "I owe you $10 (q). It costs $5 and $10."),
])
def test_latex_becomes_unicode_and_money_stays(tex, shown):
    assert tutor.unlatex(tex) == shown


def test_latex_is_converted_while_streaming_and_at_the_end():
    raw = "So $x \\in \\mathbb{Z}$.\n" + CHECK
    assert tutor.streaming_text(raw) == "So x ∈ ℤ.\n"
    assert tutor.split_blocks(raw)[0] == "So x ∈ ℤ."


# ---------------------------------------------------------------- prompt


def test_the_prompt_knows_the_course_module_and_what_is_coming():
    p = tutor.system_prompt(req(upcoming="Midterm in 5 days (Wed Oct 14)"))
    assert "COMSC-230 (Discrete Math)" in p and "Friday October 9, 2026" in p
    assert "truth tables" in p and "Midterm in 5 days" in p
    assert "```check" in p and "never LaTeX" in p


def test_general_study_has_no_course_in_the_prompt():
    p = tutor.system_prompt(req(course="General", module=modules.GENERAL))
    assert "studying on their own" in p and "course General" not in p


def test_a_photo_goes_with_the_question():
    msgs = tutor.build_messages(req(image_b64="QUJD"), [("user", "hi"), ("assistant", "hello")])
    assert [m["role"] for m in msgs] == ["system", "user", "assistant", "user"]
    assert msgs[-1]["images"] == ["QUJD"] and "images" not in msgs[1]


# ---------------------------------------------------------------- ask + sessions


def test_ask_streams_stores_and_returns_the_checks(conn):
    model = Model(f"Think of it as a promise.\n{CHECK}")
    got: list[str] = []
    r = tutor.ask(model, conn, req(), got.append)
    assert "".join(got) == r.raw and r.text == "Think of it as a promise."
    assert r.checks == [{"q": "Is p → q true when p is false?", "a": "Yes, vacuously."}]
    assert tutor.transcript(conn, "school:COMSC-230:t1") == [
        ("user", "What is an implication?"), ("assistant", r.raw)]
    tutor.ask(Model("Sure."), conn, req("And the converse?"))   # history goes back in
    assert model.messages[0][-1]["content"] == "What is an implication?"


def test_history_is_sent_on_the_next_question(conn):
    tutor.ask(Model("First."), conn, req())
    second = Model("Second.")
    tutor.ask(second, conn, req("Again?"))
    roles = [(m["role"], m["content"]) for m in second.messages[0][1:]]
    assert roles == [("user", "What is an implication?"), ("assistant", "First."),
                     ("user", "Again?")]


def test_session_ids_are_per_course():
    a, b = tutor.new_session_id("COMSC-230"), tutor.new_session_id("COMSC-230")
    assert a != b and a.startswith("school:COMSC-230:") and tutor.is_school_session(a)
    assert not tutor.is_school_session("abc123")


# ---------------------------------------------------------------- modules + Canvas


def test_courses_map_to_modules():
    assert modules.for_course("COMSC-230").key == "discrete"
    assert modules.for_course("comsc-230").key == "discrete"
    assert modules.for_course("HIST-101") is modules.GENERAL
    assert modules.get("calc").name == "Calculus III" and modules.get("nope") is modules.GENERAL
    for m in modules.MODULES.values():
        assert m.starters and m.summary


def test_canvas_courses_are_short_unique_and_current(conn):
    assert canvas.courses(conn) == []
    now = datetime(2026, 10, 9, 9, 0, tzinfo=LA)
    events = [canvas.CanvasEvent("a1", "assignment", "HW 3", "COMSC-230-5148", "2026-10-12"),
              canvas.CanvasEvent("a2", "event", "Quiz 2", "COMSC-230-5148", "2026-10-14"),
              canvas.CanvasEvent("m1", "event", "Midterm", "MATH 1C - Fall 2026", "2026-10-20"),
              canvas.CanvasEvent("p1", "event", "Lab", "PHYS-4A-0120", "2026-10-21")]
    canvas.store(conn, events, now)
    assert canvas.courses(conn) == ["COMSC-230", "MATH 1C", "PHYS-4A"]
    canvas.store(conn, events[:2], now)                       # MATH left the feed
    assert canvas.courses(conn) == ["COMSC-230"]
