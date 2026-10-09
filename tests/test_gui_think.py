"""Thinking mode in the chat panel, offscreen, with fake ask / voice functions.

The fakes emit the escalation phase events (``jarvis.escalate``) from a worker
thread like the real ones; nothing calls a model.
"""
from __future__ import annotations

import os
import threading

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import QSettings  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.chat import Answer, Source  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.gui import chat_panel  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.chat_panel import NO_ANSWER, BotMessage, ChatPanel, thought_text  # noqa: E402
from jarvis.gui.theme import LIGHT  # noqa: E402
from test_gui_data import REPO  # noqa: E402
from test_gui_voice import FakeRecorder, FakeSpeaker, pump  # noqa: E402

SRC = [Source(f"#{n}", f"#{n} issue", f"https://github.com/{REPO}/issues/{n}") for n in range(1, 9)]


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


def _cfg(tmp_path) -> Config:
    cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db", hardware_path=tmp_path / "none.yaml")
    db.connect(cfg.db_path).close()
    return cfg


def _settings(tmp_path) -> QSettings:
    return QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)


def _deep_answer(**kw) -> Answer:
    base = dict(text="Start with #31: the tilt driver blocks the demo.", sources=SRC,
                unverified_numbers=[], notes=[], model="gemma4:26b", duration=24.0,
                cited_sources=SRC[:6], cited_more_count=2, tier="deep", escalated=True,
                escalation_reasons=["no_sources"], thinking_s=14.2)
    base.update(kw)
    return Answer(**base)


class EscalatingAsk:
    """Streams a weak fast answer, escalates, then streams the deep one. Each
    step waits on a gate so the test can look at the bubble in between."""

    def __init__(self, answer: Answer | None = None):
        self.answer = answer or _deep_answer()
        self.calls: list[dict] = []
        self.gates = [threading.Event() for _ in range(3)]

    def __call__(self, conn, cfg, question, history, on_token, session_id, *, mode,
                 on_phase):
        self.calls.append({"mode": mode})
        on_phase("fast", {"model": "gemma4:26b"})
        on_token("Not sure.")
        self.gates[0].wait(5)
        on_phase("escalating", {"reasons": ["no_sources"], "spoken": False})
        on_phase("discard_fast", {})
        on_phase("deep", {"model": "gemma4:26b", "reason": "escalated"})
        on_phase("thinking", {})
        self.gates[1].wait(5)
        on_phase("answering", {"thinking_s": 14.2})
        on_token("Start with #31")
        self.gates[2].wait(5)
        on_phase("done", {"tier": "deep", "escalated": True})
        return self.answer


def test_typed_escalation_fades_fast_text_and_shows_deep_state(app, tmp_path):
    ask = EscalatingAsk()
    p = ChatPanel(_cfg(tmp_path), LIGHT, ask, settings=_settings(tmp_path), mic_ok=False)
    p.show()
    p.ask("What should I do today?")
    bot = p.messages[-1]
    pump(lambda: bot.streamed == "Not sure.")
    assert bot.thinking.isHidden() and not bot.deep
    ask.gates[0].set()
    pump(lambda: bot.deep)
    # the weak text is gone, the indicator is back in its deep form
    assert bot.streamed == "" and bot.body.isHidden() and not bot.thinking.isHidden()
    assert bot.thinking_orb.state == "deep"
    assert bot.thinking_text.text().startswith("Thinking deeper — first answer was weak")
    ask.gates[1].set()
    pump(lambda: bot.streamed == "Start with #31")
    assert bot.thinking.isHidden() and bot.body.graphicsEffect() is None
    ask.gates[2].set()
    p.wait()
    assert ask.calls == [{"mode": "auto"}]
    assert "#31" in bot.body.text()
    assert bot.meta.text() == "gemma4:26b · deep · thought for 14 s · 24 s"
    # only the cited sources, plus a "+2 more" chip
    assert [b.text() for b in bot.chip_buttons] == [f"#{n}" for n in range(1, 7)]
    assert bot.more_chip is not None and bot.more_chip.text() == "+2 more"
    p.close()


def test_think_toggle_persists_and_sets_mode(app, tmp_path):
    ask = EscalatingAsk()
    for g in ask.gates:
        g.set()
    p = ChatPanel(_cfg(tmp_path), LIGHT, ask, settings=_settings(tmp_path), mic_ok=False)
    assert not p.think_mode and p.mode == "auto"
    p.think_btn.click()
    assert p.think_mode and p.mode == "think"
    p.ask("Is my 5V rail OK?")
    p.wait()
    assert ask.calls == [{"mode": "think"}]
    p.close()
    # remembered by a new panel (same settings file)
    q = ChatPanel(_cfg(tmp_path), LIGHT, ask, settings=_settings(tmp_path), mic_ok=False)
    assert q.think_mode and q.think_btn.isChecked()
    q._think_shortcut.activated.emit()  # Ctrl+T toggles it back
    assert not q.think_mode
    assert not _settings(tmp_path).value(chat_panel.THINK_KEY, type=bool)
    q.close()


def test_old_style_ask_fn_still_works(app, tmp_path):
    """An ask_fn without mode / on_phase gets the six positional arguments only."""
    seen = []

    def plain(conn, cfg, question, history, on_token, session_id):
        seen.append(question)
        return _deep_answer(tier="fast", escalated=False, thinking_s=None, duration=3.0)

    p = ChatPanel(_cfg(tmp_path), LIGHT, plain, settings=_settings(tmp_path), mic_ok=False)
    p.think_btn.click()
    p.ask("q?")
    p.wait()
    bot = p.messages[-1]
    assert seen == ["q?"] and bot.meta.text() == "gemma4:26b · 3.0 s"
    p.close()


def test_empty_answer_never_shows_no_answer(app, tmp_path):
    def empty(conn, cfg, question, history, on_token, session_id):
        return _deep_answer(text="", cited_sources=[], cited_more_count=0, sources=[])

    p = ChatPanel(_cfg(tmp_path), LIGHT, empty, settings=_settings(tmp_path), mic_ok=False)
    p.ask("q?")
    p.wait()
    bot = p.messages[-1]
    assert "(no answer)" not in bot.body.text() and "couldn't put an answer" in bot.body.text()
    assert NO_ANSWER.startswith("I couldn't")
    assert bot.chips_box.isHidden()
    p.close()


def test_fallback_and_cut_meta(app):
    bot = BotMessage(LIGHT)
    bot.phase("deep", {"reason": "think_mode"})
    assert bot.thinking_text.text().startswith("Thinking deeper — thinking mode")
    bot.phase("thinking_cut", {"thinking_s": 20.4})
    bot.phase("fallback", {"reasons": ["empty"]})
    bot.finish(_deep_answer(tier="fallback", thinking_s=None, duration=31.0,
                            cited_more_count=0))
    assert bot.meta.text() == "gemma4:26b · fallback · thought for 20 s, cut short · 31 s"
    assert bot.more_chip is None
    assert thought_text(None) == "" and thought_text(4.25) == "thought for 4.2 s"


def test_voice_turn_gets_mode_and_deep_orb(app, tmp_path):
    gate = threading.Event()
    calls = []

    def voice_answer(conn, cfg, question, history, speaker, on_token, session_id, *,
                     mode, on_phase):
        calls.append(mode)
        on_phase("deep", {"model": "gemma4:26b", "reason": "planning"})
        gate.wait(5)
        on_token("Start with #31.")
        return _deep_answer(thinking_s=None)

    p = ChatPanel(_cfg(tmp_path), LIGHT, None, settings=_settings(tmp_path), mic_ok=True,
                  recorder_factory=lambda on_level: FakeRecorder(on_level),
                  transcribe_fn=lambda wav, cfg: "What should I do today?",
                  voice_answer_fn=voice_answer,
                  speaker_factory=lambda *a: FakeSpeaker(*a))
    p.show()
    p.toggle_voice()
    pump(lambda: p.messages and isinstance(p.messages[-1], BotMessage)
         and p.messages[-1].deep)
    assert calls == ["auto"]
    assert p.voice_state == "thinking" and p.orb.state == "deep"
    assert p.caption.text() == "Thinking deeper…"
    gate.set()
    pump(lambda: p.messages[-1].answer is not None)
    assert p.messages[-1].meta.text() == "gemma4:26b · deep · 24 s"
    p.shutdown()
    p.close()


def test_chip_text_reads_like_words():
    from jarvis.gui.chat_panel import chip_text

    assert chip_text("#7") == "#7"
    assert chip_text("commit:02fb2d2abc") == "02fb2d2"
    assert chip_text("fact:next_steps") == "next steps"
    assert chip_text("fact:maybe_done:28") == "maybe done #28"
    assert chip_text("check:rail_budget:5V") == "rail budget 5V"
    assert chip_text("milestone:Core demo") == "Core demo"
