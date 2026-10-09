"""Voice in the chat panel, offscreen, with fakes only.

Nothing here records the microphone or plays audio: the recorder, speech-to-text,
the spoken answer and the speaker are all injected fakes. Their callbacks fire
on worker threads like the real ones, so the signal plumbing is exercised.
"""
from __future__ import annotations

import os
import threading
import time

import pytest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from test_gui_window import _offscreen_ok  # noqa: E402

if not _offscreen_ok():
    pytest.skip("PySide6 cannot start on the offscreen platform", allow_module_level=True)

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtGui import QShortcut  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from jarvis import db  # noqa: E402
from jarvis.config import Config  # noqa: E402
from jarvis.gui import voice_turn as vt  # noqa: E402
from jarvis.gui.app import make_app  # noqa: E402
from jarvis.gui.chat_panel import BotMessage, ChatPanel, UserMessage, VoiceNote  # noqa: E402
from jarvis.gui.theme import LIGHT  # noqa: E402
from jarvis.voice import NO_PW_RECORD, STT_DOWN, TTS_DOWN, VoiceError  # noqa: E402
from test_gui_data import REPO  # noqa: E402

WAV = b"RIFF....WAVEfake"


@pytest.fixture(scope="module")
def app():
    return make_app([], scheme="light")


def pump(cond, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        QApplication.processEvents()
        if cond():
            return
        time.sleep(0.003)
    raise AssertionError("condition not reached")


def settle(ms: float = 30) -> None:
    end = time.monotonic() + ms / 1000
    while time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.002)


def _answer(text="Core demo has 9 open issues.", **kw):
    from jarvis.chat import Answer, Source

    base = dict(text=text, sources=[Source("#7", "#7 Create track.c",
                                           f"https://github.com/{REPO}/issues/7"),
                                    Source("commit:02fb2d2abc", "commit 02fb2d2", None)],
                unverified_numbers=[], notes=[], model="qwen3:14b", duration=3.0)
    base.update(kw)
    base.setdefault("cited_sources", list(base["sources"]))  # what the answer cited
    return Answer(**base)


# ---------------------------------------------------------------- fakes


class FakeRecorder:
    def __init__(self, on_level, *, levels=(0.2, 0.6, 0.45), result=WAV, block=False,
                 error=None):
        self.on_level = on_level
        self.levels = levels
        self.result = result
        self.block = block
        self.error = error
        self.released = threading.Event()
        self.stopped = False
        self.thread = None

    def record(self):
        self.thread = threading.current_thread()
        for v in self.levels:
            self.on_level(v)
        if self.block:
            self.released.wait(5)
        if self.error:
            raise VoiceError(self.error)
        return self.result

    def stop(self):
        self.stopped = True
        self.released.set()


class FakeSpeaker:
    def __init__(self, cfg, on_level, on_state, on_error):
        self.on_level, self.on_state, self.on_error = on_level, on_state, on_error
        self.said: list[str] = []
        self._speaking = False
        self.stops = 0
        self.closed = False

    @property
    def speaking(self):
        return self._speaking

    def say(self, text):  # called on the voice_answer worker thread
        self.said.append(text)
        if not self._speaking:
            self._speaking = True
            self.on_state("speaking")
        self.on_level(0.7)

    def finish(self):
        """Playback drained (as the real player thread reports it)."""
        def run():
            self._speaking = False
            self.on_level(0.0)
            self.on_state("idle")
        t = threading.Thread(target=run)
        t.start()
        t.join()

    def fail(self, message):
        def run():
            self.on_error(message)
            self.stop()
        t = threading.Thread(target=run)
        t.start()
        t.join()

    def stop(self):
        self.stops += 1
        if self._speaking:
            self._speaking = False
            self.on_level(0.0)
            self.on_state("idle")

    def close(self):
        self.closed = True
        self.stop()


class Rig:
    """A ChatPanel wired to fakes, plus everything the fakes saw."""

    def __init__(self, tmp_path, *, recorder_kw=None, transcript="What's left for Core demo?",
                 stt_error=None, answer=None, gate=False, sentences=("Core demo has nine "
                                                                     "open issues left.",),
                 late_sentence=None, answer_error=None, mic_ok=True, window=False):
        self.cfg = Config(repo=REPO, db_path=tmp_path / "jarvis.db",
                          hardware_path=tmp_path / "none.yaml")
        db.connect(self.cfg.db_path).close()
        self.settings = QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat)
        self.recorders: list[FakeRecorder] = []
        self.recorder_kw = dict(recorder_kw or {})
        self.transcribed: list[bytes] = []
        self.transcript = transcript
        self.stt_error = stt_error
        self.stt_gate = threading.Event()
        self.stt_gate.set()
        self.answer = answer or _answer()
        self.gate = threading.Event()
        if not gate:
            self.gate.set()
        self.sentences = sentences
        self.late_sentence = late_sentence
        self.answer_error = answer_error
        self.calls: list[tuple] = []
        self.speakers: list[FakeSpeaker] = []
        self.asked: list[tuple] = []
        kw = dict(recorder_factory=self._recorder, transcribe_fn=self._transcribe,
                  voice_answer_fn=self._voice_answer, speaker_factory=self._speaker,
                  mic_ok=mic_ok, settings=self.settings)
        if window:
            from jarvis.gui.window import MainWindow

            self.conn = db.connect(self.cfg.db_path)
            self.win = MainWindow(self.cfg, self.conn, auto_refresh=False, watch=False,
                                  start="projects",
                                  ask_fn=self._ask, voice=kw, scheme="light")
            self.panel = self.win.chat
        else:
            self.win = None
            self.panel = ChatPanel(self.cfg, LIGHT, self._ask, "qwen3:14b", **kw)
            self.panel.resize(380, 760)
        self.states: list[str] = []
        self.orb_states: list[str] = []
        self.panel.voice_state_changed.connect(self.states.append)
        self.panel.orb.state_changed.connect(self.orb_states.append)

    # fakes
    def _recorder(self, on_level):
        r = FakeRecorder(on_level, **self.recorder_kw)
        self.recorders.append(r)
        return r

    def _transcribe(self, wav, cfg):
        self.transcribed.append(wav)
        self.stt_gate.wait(5)
        if self.stt_error:
            raise VoiceError(self.stt_error)
        return self.transcript

    def _voice_answer(self, conn, cfg, question, history, speaker, on_token, session_id):
        self.calls.append((question, list(history), session_id, conn is not None))
        if self.answer_error:
            raise RuntimeError(self.answer_error)
        for s in self.sentences:
            on_token(s + " ")
            speaker.say(s)
        self.gate.wait(5)
        if self.late_sentence:
            on_token(self.late_sentence)
            speaker.say(self.late_sentence)
        return self.answer

    def _speaker(self, cfg, on_level, on_state, on_error):
        s = FakeSpeaker(cfg, on_level, on_state, on_error)
        self.speakers.append(s)
        return s

    def _ask(self, conn, cfg, question, history, on_token, session_id):
        self.asked.append((question, list(history), session_id))
        on_token("typed ")
        self.gate.wait(5)
        return _answer("Typed answer.")

    @property
    def speaker(self) -> FakeSpeaker:
        return self.speakers[0]

    def close(self):
        if self.win is not None:
            self.win.close()
            self.conn.close()
        else:
            self.panel.close()


@pytest.fixture
def rig(app, tmp_path):
    made: list[Rig] = []

    def make(**kw) -> Rig:
        r = Rig(tmp_path, **kw)
        made.append(r)
        return r

    yield make
    for r in made:
        r.gate.set()
        r.stt_gate.set()
        r.close()


def _notes(panel) -> list[VoiceNote]:
    return [m for m in panel.messages if isinstance(m, VoiceNote)]


# ---------------------------------------------------------------- pure state machine


def test_state_machine_transitions():
    t = vt.VoiceTurn()
    for event in ("start", "recorded", "transcript", "speech_started", "speech_paused",
                  "speech_started", "done"):
        t.fire(event)
    assert t.trail == ["idle", "listening", "transcribing", "thinking", "speaking", "thinking",
                       "speaking", "idle"]
    with pytest.raises(vt.InvalidTransition):
        t.fire("recorded")
    for state in ("listening", "transcribing", "thinking", "speaking"):
        assert vt.TRANSITIONS[(state, "error")] == "idle"
        assert vt.TRANSITIONS[(state, "cancel")] == "idle"
    assert ("idle", "cancel") not in vt.TRANSITIONS
    t.fire("start")
    t.fire("no_speech")
    assert t.state == "idle"
    t.fire("start")
    t.fire("recorded")
    t.fire("transcript")
    t.muted = t.answer_done = True
    assert not t.active
    t.fire("done")
    assert not t.muted and not t.answer_done  # reset for the next turn


def test_barge_in_table():
    a = vt.barge_in_action
    assert a("idle", "mic") == "start" and a("idle", "esc") is None
    assert a("listening", "mic") == "stop_recording" and a("listening", "esc") == "cancel"
    assert a("transcribing", "mic") == a("transcribing", "esc") == "cancel"
    assert a("thinking", "mic") == a("thinking", "esc") == "mute"
    assert a("speaking", "mic") == a("speaking", "esc") == "stop_speech"
    assert a("thinking", "esc", muted=True) is None
    assert a("speaking", "mic", muted=True) is None


def test_turn_speaker_drops_after_mute():
    said = []
    s = vt.TurnSpeaker(type("S", (), {"say": lambda self, t: said.append(t),
                                      "speaking": True})())
    s.say("one")
    assert s.speaking
    s.mute()
    s.say("two")
    assert said == ["one"] and not s.speaking


# ---------------------------------------------------------------- happy path


def test_full_voice_turn(rig):
    r = rig(recorder_kw={"block": True}, gate=True)
    p = r.panel
    p.show()
    assert p.mic_btn.isEnabled() and p.mic_btn.toolTip() == "Talk to Jarvis (Ctrl+Space)"
    p.mic_btn.click()
    # listening: staged orb, caption, active mic, typed send off
    assert p.voice_state == "listening" and p.orb_staged and p.orb.expanded
    assert p.orb.state == "listening" and p.mic_btn.active
    assert p.caption.isVisible() and p.caption.text().startswith("Listening…")
    assert "Ctrl+Space" in p.caption.text()
    p.composer.edit.setPlainText("typed meanwhile")
    assert not p.composer.send_btn.isEnabled() and not p.new_btn.isEnabled()
    pump(lambda: abs(p.orb.level - 0.45) < 1e-6)  # mic levels reach the orb
    assert r.recorders[0].thread is not threading.main_thread()
    # the recorder ends on its own (silence) -> transcribing
    r.stt_gate.clear()
    r.recorders[0].released.set()
    pump(lambda: p.voice_state == "transcribing")
    assert p.orb.state == "thinking" and p.caption.text() == "Transcribing…"
    assert not p.mic_btn.active
    r.stt_gate.set()
    # transcript -> user bubble with a mic, bot streams, speech starts
    pump(lambda: p.voice_state == "speaking")
    assert r.transcribed == [WAV]
    user, bot = p.messages
    assert isinstance(user, UserMessage) and user.voice and user.glyph is not None
    assert user.text == "What's left for Core demo?"
    assert isinstance(bot, BotMessage)
    pump(lambda: bot.streamed)
    assert bot.streamed.startswith("Core demo has nine")
    assert p.orb.state == "speaking"
    pump(lambda: abs(p.orb.level - 0.7) < 1e-6)  # speaker level drives the orb
    assert p.caption.text().startswith("Speaking")
    assert r.speaker.said == ["Core demo has nine open issues left."]
    assert not p.composer.send_btn.isEnabled()
    # the model returns: the bubble shows the validated answer + chips
    r.gate.set()
    pump(lambda: bot.answer is not None)
    assert "Core demo has 9 open issues." in bot.body.text()
    assert [b.text() for b in bot.chip_buttons] == ["#7", "02fb2d2"]
    assert p.voice_state == "speaking"  # still talking
    # playback drains -> idle, then the orb leaves its stage
    r.speaker.finish()
    pump(lambda: p.voice_state == "idle")
    assert p.orb.state == "idle" and p.orb.level == 0.0
    assert not p.caption.isVisible()
    assert p.composer.send_btn.isEnabled() and p.new_btn.isEnabled()
    pump(lambda: not p.orb_staged)
    assert not p.orb.expanded
    assert r.states == ["listening", "transcribing", "thinking", "speaking", "idle"]
    assert r.orb_states[:4] == ["listening", "thinking", "speaking", "idle"]
    # same history + session as typed chat
    assert p.history == [("user", "What's left for Core demo?"),
                         ("assistant", "Core demo has 9 open issues.")]
    assert r.calls[0][2] == p.session_id and r.calls[0][3]
    p.ask("And v1.0?")
    pump(lambda: not p.busy)
    assert r.asked[0][1] == p.history[:2] and r.asked[0][2] == p.session_id


def test_answer_done_before_speech_starts_waits_for_speaker(rig):
    r = rig()
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.messages and p.messages[-1].answer is not None)
    # the fake started speaking during the answer; still speaking -> state speaking
    assert p.voice_state == "speaking"
    r.speaker.finish()
    pump(lambda: p.voice_state == "idle")


# ---------------------------------------------------------------- no speech / errors


def test_no_speech_says_didnt_catch_that(rig):
    r = rig(recorder_kw={"result": None})
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "idle")
    assert [n.text for n in _notes(p)] == ["Didn't catch that."]
    assert not any(isinstance(m, UserMessage) for m in p.messages)
    assert r.transcribed == [] and r.calls == []
    assert r.states == ["listening", "idle"]


def test_empty_transcript_is_no_speech(rig):
    r = rig(transcript="   ")
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "idle")
    assert [n.text for n in _notes(p)] == ["Didn't catch that."]
    assert r.calls == [] and r.states == ["listening", "transcribing", "idle"]


def test_stt_down_shows_hint_and_typed_chat_still_works(rig):
    msg = STT_DOWN.format(url="http://127.0.0.1:5092")
    r = rig(stt_error=msg)
    p = r.panel
    p.toggle_voice()
    pump(lambda: _notes(p))
    note = _notes(p)[0]
    assert note.error and "Start it with: systemctl --user start parakeet-stt" in note.text
    assert note.label.property("level") == "error"
    assert "<code" in note.label.text() and "systemctl --user start parakeet-stt" in \
        note.label.text()
    assert p.voice_state == "idle" and "error" in r.orb_states
    assert not any(isinstance(m, UserMessage) for m in p.messages)
    assert p.mic_btn.isEnabled()
    p.ask("typed works")
    pump(lambda: not p.busy)
    assert r.asked and p.messages[-1].answer is not None


def test_no_mic_error_from_recorder(rig):
    r = rig(recorder_kw={"error": "No microphone input: pw-record stopped right away (x)."})
    p = r.panel
    p.toggle_voice()
    pump(lambda: _notes(p))
    assert "No microphone input" in _notes(p)[0].text and p.voice_state == "idle"
    assert p.orb.state == "error"


def test_speaker_error_keeps_text_answer(rig):
    r = rig(gate=True)
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "speaking")
    r.speaker.fail(TTS_DOWN.format(url="http://127.0.0.1:8880"))
    pump(lambda: _notes(p))
    assert "systemctl --user start kokoro-tts" in _notes(p)[0].text
    assert p.voice_state == "thinking" and not p.voice_active  # speech off, text continues
    r.gate.set()
    pump(lambda: p.voice_state == "idle")
    bot = next(m for m in p.messages if isinstance(m, BotMessage))
    assert "Core demo has 9 open issues." in bot.body.text()


def test_answer_failure_shows_inline(rig):
    r = rig(answer_error="socket closed")
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "idle")
    bot = next(m for m in p.messages if isinstance(m, BotMessage))
    assert "socket closed" in bot.body.text() and bot.body.objectName() == "botError"
    assert p.history == []


# ---------------------------------------------------------------- barge-in


def test_mic_while_listening_submits_what_was_said(rig):
    r = rig(recorder_kw={"block": True})
    p = r.panel
    p.toggle_voice()
    p.toggle_voice()  # Ctrl+Space / mic again: stop and submit
    assert r.recorders[0].stopped
    pump(lambda: r.transcribed == [WAV])
    pump(lambda: p.voice_state in ("speaking", "idle") and r.calls)


def test_esc_while_listening_cancels(rig):
    r = rig(recorder_kw={"block": True})
    p = r.panel
    p.toggle_voice()
    assert p.stop_voice() is True
    assert r.recorders[0].stopped and p.voice_state == "idle"
    settle(50)
    assert r.transcribed == [] and not _notes(p)  # the late recording is ignored
    assert p.stop_voice() is False  # nothing left to stop


def test_barge_in_while_transcribing_cancels(rig):
    r = rig()
    r.stt_gate.clear()
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "transcribing")
    p.toggle_voice()
    assert p.voice_state == "idle"
    r.stt_gate.set()
    settle(60)
    assert r.calls == [] and not any(isinstance(m, UserMessage) for m in p.messages)


def test_barge_in_while_thinking_mutes_speech_but_text_finishes(rig):
    r = rig(gate=True, sentences=(), late_sentence="This should never be spoken.")
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "thinking" and r.calls)
    assert p.voice_active
    p.toggle_voice()  # mute
    assert p.voice_state == "thinking" and not p.voice_active
    assert r.speaker.stops == 1 and not p.caption.isVisible()
    assert not p.composer.send_btn.isEnabled()  # still one turn at a time
    r.gate.set()
    pump(lambda: p.voice_state == "idle")
    assert r.speaker.said == []
    bot = next(m for m in p.messages if isinstance(m, BotMessage))
    assert "Core demo has 9 open issues." in bot.body.text()
    pump(lambda: not p.orb_staged)


def test_barge_in_while_speaking_stops_speaker(rig):
    r = rig()
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "speaking" and r.speaker.speaking
         and p.messages[-1].answer is not None)
    p.toggle_voice()
    assert r.speaker.stops == 1 and not r.speaker.speaking
    assert p.voice_state == "idle" and p.orb.state == "idle"


def test_barge_in_while_speaking_and_streaming(rig):
    r = rig(gate=True, late_sentence="Muted sentence.")
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "speaking")
    assert p.stop_voice()  # Esc
    assert p.voice_state == "thinking" and not p.voice_active
    r.gate.set()
    pump(lambda: p.voice_state == "idle")
    assert "Muted sentence." not in r.speaker.said


# ---------------------------------------------------------------- auto-listen


def test_auto_listen_rearms_and_persists(rig, tmp_path):
    r = rig()
    p = r.panel
    assert not p.auto_listen  # off by default
    p.auto_listen_action.trigger()  # the header menu's checkbox
    assert p.auto_listen
    assert vt.read_bool(QSettings(str(tmp_path / "ui.ini"), QSettings.Format.IniFormat),
                        vt.AUTO_LISTEN_KEY)
    r.recorder_kw = {"block": True}
    p.toggle_voice()
    r.recorders[0].released.set()
    pump(lambda: p.voice_state == "speaking" and p.messages[-1].answer is not None)
    r.speaker.finish()
    pump(lambda: p.voice_state == "idle")
    assert p.orb_staged  # stays on stage for the follow-up
    pump(lambda: p.voice_state == "listening")
    assert len(r.recorders) == 2
    p.stop_voice()
    # a panel made later reads the saved setting
    q = ChatPanel(r.cfg, LIGHT, r._ask, settings=QSettings(str(tmp_path / "ui.ini"),
                                                            QSettings.Format.IniFormat),
                  mic_ok=True, speaker_factory=r._speaker, recorder_factory=r._recorder)
    assert q.auto_listen
    q.close()


def test_no_auto_listen_after_barge_in(rig):
    r = rig()
    p = r.panel
    p.set_auto_listen(True)
    p.toggle_voice()
    pump(lambda: p.voice_state == "speaking" and p.messages[-1].answer is not None)
    p.stop_voice()
    settle(600)
    assert p.voice_state == "idle" and len(r.recorders) == 1


# ---------------------------------------------------------------- availability / exclusion


def test_mic_disabled_without_pw_record(rig, monkeypatch):
    r = rig(mic_ok=False)
    p = r.panel
    assert not p.mic_btn.isEnabled() and "pw-record" in p.mic_btn.toolTip()
    assert p.mic_btn.toolTip() == NO_PW_RECORD
    p.toggle_voice()
    assert p.voice_state == "idle" and r.recorders == []
    from jarvis.gui import chat_panel

    monkeypatch.setattr(chat_panel.shutil, "which", lambda name: None)
    assert chat_panel.mic_available() is False
    monkeypatch.setattr(chat_panel.shutil, "which", lambda name: "/usr/bin/" + name)
    assert chat_panel.mic_available() is True


def test_typed_and_voice_are_mutually_exclusive(rig):
    r = rig(gate=True)
    p = r.panel
    p.ask("typed question")
    assert p.busy and not p.mic_btn.isEnabled()
    p.toggle_voice()
    assert p.voice_state == "idle" and r.recorders == []
    r.gate.set()
    pump(lambda: not p.busy)
    assert p.mic_btn.isEnabled()
    r.gate.clear()
    p.toggle_voice()
    pump(lambda: p.voice_state in ("thinking", "speaking"))
    n = len(p.messages)
    p.ask("typed while voice")  # ignored
    p.suggestion_buttons[0].click()  # ignored
    p.new_chat()  # ignored
    assert len(p.messages) == n and len(r.asked) == 1
    assert not p.composer.send_btn.isEnabled()
    r.gate.set()


# ---------------------------------------------------------------- close


def test_close_stops_everything_and_leaves_no_threads(rig):
    r = rig(recorder_kw={"block": True})
    p = r.panel
    p.show()
    p.toggle_voice()
    pump(lambda: r.recorders and r.recorders[0].thread is not None)
    # a speaker exists from an earlier turn
    p._ensure_speaker()
    p.close()
    assert r.recorders[0].stopped and r.speaker.closed
    assert p.voice_threads() == []
    assert not [t for t in threading.enumerate() if t.name.startswith("jarvis-voice")]
    p.toggle_voice()  # nothing after close
    assert len(r.recorders) == 1


def test_window_close_shuts_voice_down(rig):
    r = rig(window=True, gate=True)
    p = r.panel
    p.toggle_voice()
    pump(lambda: p.voice_state == "speaking")
    r.gate.set()
    r.win.close()
    assert r.speaker.closed and p.voice_threads() == []


# ---------------------------------------------------------------- window shortcuts


def test_ctrl_space_and_esc_priority(rig):
    from test_gui_data import seed

    r = rig(window=True, recorder_kw={"block": True})
    win, p = r.win, r.panel
    ids = seed(r.conn, n_cards=2)
    win.reload(force=True)
    win.show()
    QApplication.processEvents()
    shortcuts = {s.key().toString(): s for s in win.findChildren(QShortcut)}
    assert "Ctrl+Space" in shortcuts
    space = shortcuts["Ctrl+Space"]
    assert space.context() == Qt.ShortcutContext.WindowShortcut
    win.set_chat_open(False)
    space.activated.emit()  # opens the chat and starts listening
    assert win.chat.isVisible() and p.voice_state == "listening"
    win.graph.lock(f"card:{ids[1]}")
    assert win.graph.lock_id == f"card:{ids[1]}"
    esc = shortcuts["Esc"]
    esc.activated.emit()  # voice first
    assert p.voice_state == "idle" and win.graph.lock_id == f"card:{ids[1]}"
    esc.activated.emit()  # then the locked node
    assert win.graph.lock_id is None and win.chat.isVisible()
    esc.activated.emit()  # then the chat
    QApplication.processEvents()
    assert not win.chat.isVisible()


def test_ctrl_space_works_while_typing(rig):
    from PySide6.QtTest import QTest

    r = rig(window=True, recorder_kw={"block": True})
    win, p = r.win, r.panel
    win.show()
    win.activateWindow()
    QTest.qWaitForWindowActive(win, 1000)
    p.focus_input()
    QApplication.processEvents()
    if QApplication.focusWidget() is not p.composer.edit:
        pytest.skip("offscreen platform gave no keyboard focus")
    QTest.keyClick(p.composer.edit, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    QApplication.processEvents()
    assert p.voice_state == "listening"
    assert p.composer.edit.toPlainText() == ""  # the shortcut, not a typed space
    p.stop_voice()
