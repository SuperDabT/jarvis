"""Voice backend tests: no network, no microphone, no speakers (fake processes/HTTP)."""
from __future__ import annotations

import io
import math
import struct
import threading
import time
import wave
from pathlib import Path

import httpx
import pytest

from jarvis import chat, voice
from jarvis.config import Config

from test_chat import NOW, FakeClient, cfg, seeded

RATE = 16000


def tone(seconds: float, amp: int, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return struct.pack(f"<{n}h", *(int(amp * math.sin(2 * math.pi * 220 * k / rate))
                                   for k in range(n)))


def noise_floor(seconds: float, amp: int = 30) -> bytes:
    n = int(seconds * RATE)
    return struct.pack(f"<{n}h", *((amp if k % 2 else -amp) for k in range(n)))


# ---------------------------------------------------------------- fake processes


class FakeStdout:
    def __init__(self, data: bytes, on_read=None):
        self._io = io.BytesIO(data)
        self.on_read = on_read
        self.reads = 0

    def read(self, n=-1):
        self.reads += 1
        if self.on_read:
            self.on_read(self.reads)
        return self._io.read(n)


class FakeReader:
    def __init__(self, data: bytes, stderr: bytes = b"", returncode=0, on_read=None):
        self.stdout = FakeStdout(data, on_read)
        self.stderr = io.BytesIO(stderr)
        self.stdin = None
        self.returncode = None
        self._rc = returncode
        self.killed = False
        self.cmd = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = self._rc
        return self.returncode


def reader_factory(proc):
    def make(cmd):
        proc.cmd = cmd
        return proc
    return make


# ---------------------------------------------------------------- level + wav


def test_level_mapping_is_monotonic_and_bounded():
    vals = [voice.level_from_rms(r) for r in (0, 1, 10, 30, 100, 300, 1000, 3000, 10000, 32768)]
    assert vals == sorted(vals)
    assert vals[0] == 0.0 and vals[-1] == 1.0
    assert all(0.0 <= v <= 1.0 for v in vals)
    assert 0.2 < voice.level_from_rms(1000) < 0.8   # about -30 dBFS: mid-scale


def test_rms_and_wav_header():
    pcm = struct.pack("<4h", 1000, -1000, 1000, -1000)
    assert voice.rms_s16(pcm) == pytest.approx(1000)
    assert voice.rms_s16(b"") == 0.0
    data = voice.wav_bytes(pcm, 16000)
    assert data[:4] == b"RIFF" and data[8:16] == b"WAVEfmt "
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == \
            (1, 2, 16000, 4)
        assert w.readframes(4) == pcm


# ---------------------------------------------------------------- recorder


def test_recorder_stops_after_silence_following_speech():
    pcm = noise_floor(0.5) + tone(1.0, 8000) + noise_floor(3.0)
    proc = FakeReader(pcm)
    levels = []
    rec = voice.Recorder(on_level=levels.append, proc_factory=reader_factory(proc))
    wav = rec.record()
    assert rec.reason == "silence"
    assert proc.cmd == ["pw-record", "--raw", "--format", "s16", "--rate", "16000", "--channels", "1", "-"]
    assert proc.killed
    with wave.open(io.BytesIO(wav)) as w:
        secs = w.getnframes() / w.getframerate()
    # 0.5 s floor + 1 s speech + ~1.2 s silence, not the whole 4.5 s
    assert 2.5 <= secs <= 2.9
    assert max(levels) > 0.8 and levels[-1] == 0.0
    assert -50 <= rec.threshold_db <= -25


def test_recorder_no_speech_times_out():
    proc = FakeReader(noise_floor(12))
    rec = voice.Recorder(proc_factory=reader_factory(proc))
    assert rec.record() is None
    assert rec.reason == "no_speech"


def test_recorder_cap():
    proc = FakeReader(noise_floor(0.3) + tone(5, 8000))
    rec = voice.Recorder(proc_factory=reader_factory(proc), max_s=2.0)
    wav = rec.record()
    assert rec.reason == "cap"
    with wave.open(io.BytesIO(wav)) as w:
        assert w.getnframes() == pytest.approx(2.0 * RATE, abs=RATE * 0.06)


def test_recorder_ignores_constant_room_noise():
    # A loud but steady room: the adaptive floor keeps it from counting as speech.
    proc = FakeReader(noise_floor(3, amp=600) + tone(0.6, 9000) + noise_floor(2, amp=600))
    rec = voice.Recorder(proc_factory=reader_factory(proc))
    assert rec.record() is not None
    assert rec.reason == "silence"


def test_recorder_stop_from_another_thread():
    rec = None

    def on_read(k):
        if k == 30:
            rec.stop()

    proc = FakeReader(noise_floor(0.3) + tone(10, 8000), on_read=on_read)
    rec = voice.Recorder(proc_factory=reader_factory(proc))
    wav = rec.record()
    assert rec.reason == "stopped" and proc.killed
    assert wav is not None


def test_recorder_no_mic_error():
    proc = FakeReader(b"", stderr=b"error: no source found\n", returncode=1)
    rec = voice.Recorder(proc_factory=reader_factory(proc))
    with pytest.raises(voice.VoiceError, match="No microphone.*no source found"):
        rec.record()


def test_recorder_missing_pw_record(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("pw-record")
    monkeypatch.setattr(voice.subprocess, "Popen", boom)
    with pytest.raises(voice.VoiceError, match="pw-record was not found"):
        voice.Recorder().record()


# ---------------------------------------------------------------- transcribe


def vcfg(tmp_path: Path | None = None) -> Config:
    return Config(stt_url="http://stt.test", tts_url="http://tts.test", voice="bm_george")


def test_transcribe_success_sends_multipart():
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = req.read()
        return httpx.Response(200, json={"text": " Core demo is due October 18th. "})

    out = voice.transcribe(b"RIFFfake", vcfg(), transport=httpx.MockTransport(handler))
    assert out == "Core demo is due October 18th."
    assert seen["path"] == "/v1/audio/transcriptions"
    assert b'name="file"; filename="speech.wav"' in seen["body"] and b"RIFFfake" in seen["body"]


def test_transcribe_service_down_and_errors():
    def down(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(voice.VoiceError) as e:
        voice.transcribe(b"x", vcfg(), transport=httpx.MockTransport(down))
    assert "Parakeet speech-to-text isn't running" in str(e.value)
    assert "systemctl --user start parakeet-stt" in str(e.value)
    bad = httpx.MockTransport(lambda r: httpx.Response(415, json={"detail": "bad audio"}))
    with pytest.raises(voice.VoiceError, match="returned 415"):
        voice.transcribe(b"x", vcfg(), transport=bad)


def test_kokoro_stream_payload_and_down():
    seen = {}

    def handler(req):
        import json
        seen.update(json.loads(req.read()))
        return httpx.Response(200, content=b"\x01\x02\x03\x04")

    out = b"".join(voice.kokoro_stream("Hello there.", vcfg(),
                                       transport=httpx.MockTransport(handler)))
    assert out == b"\x01\x02\x03\x04"
    assert seen["voice"] == "bm_george" and seen["response_format"] == "pcm"
    assert seen["model"] == "kokoro" and seen["input"] == "Hello there."

    def down(req):
        raise httpx.ConnectError("refused")
    with pytest.raises(voice.VoiceError, match="systemctl --user start kokoro-tts"):
        list(voice.kokoro_stream("x", vcfg(), transport=httpx.MockTransport(down)))


# ---------------------------------------------------------------- speaker


class FakeStdin:
    def __init__(self, proc):
        self.proc = proc
        self.closed = False

    def write(self, b):
        if self.proc.gate is not None:
            self.proc.gate.wait(5)
        if self.proc.killed or self.closed:
            raise BrokenPipeError
        self.proc.data += b
        return len(b)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakePlayer:
    def __init__(self, gate=None):
        self.data = b""
        self.killed = False
        self.gate = gate
        self.stdin = FakeStdin(self)
        self.stdout = None
        self.returncode = None
        self.cmd = None

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.returncode = self.returncode if self.returncode is not None else 0
        return self.returncode


class Players:
    def __init__(self, gate=None):
        self.gate = gate
        self.made: list[FakePlayer] = []

    def __call__(self, cmd):
        p = FakePlayer(self.gate)
        p.cmd = cmd
        self.made.append(p)
        return p


def pcm_for(text: str) -> bytes:
    # Distinct loud audio per sentence: the text bytes, as samples.
    return b"".join(struct.pack("<h", 4000 + ord(c)) for c in text) * 4


def test_speaker_plays_in_order_with_levels_and_states():
    players = Players()
    synthed, levels, states = [], [], []

    def synth(text):
        synthed.append(text)
        data = pcm_for(text)
        yield data[:3]   # odd split: the speaker must keep samples aligned
        yield data[3:]

    sp = voice.Speaker(synth=synth, proc_factory=players, on_level=levels.append,
                       on_state=states.append, realtime=False)
    for s in ("First sentence.", "Second one.", "Third."):
        sp.say(s)
    assert sp.wait(5)
    assert synthed == ["First sentence.", "Second one.", "Third."]
    assert sp.spoken == synthed
    assert len(players.made) == 1          # one pw-play for the whole stretch
    p = players.made[0]
    assert p.cmd == ["pw-play", "--raw", "--format", "s16", "--rate", "24000", "--channels", "1", "-"]
    assert p.data == b"".join(pcm_for(s) for s in synthed)
    assert p.stdin.closed and not p.killed
    assert states == ["speaking", "idle"]
    assert any(v > 0 for v in levels) and levels[-1] == 0.0
    sp.close()
    assert not any(t.is_alive() for t in sp._threads)  # both workers end on close


def test_speaker_prefetches_next_sentence_while_playing():
    gate = threading.Event()
    players = Players(gate)
    synthed = []
    second_synth_while_playing = threading.Event()

    def synth(text):
        synthed.append(text)
        if text == "Two." and not gate.is_set():
            second_synth_while_playing.set()
        yield pcm_for(text)

    sp = voice.Speaker(synth=synth, proc_factory=players, realtime=False)
    sp.say("One.")
    sp.say("Two.")
    assert second_synth_while_playing.wait(5)   # first sentence still blocked in pw-play
    gate.set()
    assert sp.wait(5)
    assert sp.spoken == ["One.", "Two."]


def test_speaker_stop_kills_playback_and_clears_queue():
    gate = threading.Event()
    players = Players(gate)
    states = []

    def synth(text):
        for _ in range(50):
            yield pcm_for(text)

    sp = voice.Speaker(synth=synth, proc_factory=players, on_state=states.append,
                       realtime=False)
    for s in ("A long first sentence.", "Second.", "Third."):
        sp.say(s)
    deadline = time.monotonic() + 5
    while not players.made and time.monotonic() < deadline:
        time.sleep(0.01)
    assert sp.speaking
    sp.stop()
    gate.set()
    assert players.made[0].killed
    assert not sp.speaking and states[-1] == "idle"
    time.sleep(0.2)
    assert sp.spoken == []                       # nothing finished, nothing else played
    assert len(players.made) == 1
    # Still usable after barge-in.
    gate2 = Players()
    sp._factory = gate2
    sp.say("After barge-in.")
    assert sp.wait(5)
    assert sp.spoken == ["After barge-in."]


def test_speaker_tts_error_reports_and_drops_queue():
    errors = []
    players = Players()

    def synth(text):
        raise voice.VoiceError("Kokoro text-to-speech isn't running")
        yield b""  # pragma: no cover

    sp = voice.Speaker(synth=synth, proc_factory=players, on_error=errors.append,
                       realtime=False)
    sp.say("One.")
    sp.say("Two.")
    assert sp.wait(5)
    assert errors and "Kokoro" in errors[0]
    assert players.made == []


def test_speaker_realtime_pacing_and_levels():
    players = Players()
    levels = []
    one_second = struct.pack("<h", 8000) * 6000   # 0.25 s at 24 kHz

    sp = voice.Speaker(synth=lambda t: [one_second], proc_factory=players,
                       on_level=levels.append, realtime=True)
    t0 = time.monotonic()
    sp.say("Quarter second.")
    assert sp.wait(5)
    took = time.monotonic() - t0
    assert 0.2 <= took < 1.5            # paced to roughly real time
    assert len(levels) >= 10            # one level per 20 ms frame


# ---------------------------------------------------------------- sentence splitter


def split_stream(text: str, step: int = 3, **kw) -> list[str]:
    sp = voice.SentenceSplitter(**kw)
    out = []
    for k in range(0, len(text), step):
        out += sp.feed(text[k:k + step])
    return out + sp.flush()


def test_splitter_basic_and_streaming():
    text = "Core demo is due Oct 18, in 10 days. Two issues may already be done! Want more?"
    assert split_stream(text) == ["Core demo is due Oct 18, in 10 days.",
                                  "Two issues may already be done!", "Want more?"]
    assert split_stream(text, step=1) == split_stream(text, step=50)


def test_splitter_abbreviations_decimals_versions():
    text = ("Use the bench supply, e.g. the 5 V one, for the test. The rail draws 1.55 A "
            "at peak per v1.0 of the yaml. Issue #31. was closed Oct. 4 by Dr. Smith. "
            "See track.c for the filter.")
    assert split_stream(text) == [
        "Use the bench supply, e.g. the 5 V one, for the test.",
        "The rail draws 1.55 A at peak per v1.0 of the yaml.",
        "Issue #31. was closed Oct. 4 by Dr. Smith.",
        "See track.c for the filter.",
    ]


def test_splitter_lists_newlines_and_min_length():
    text = "Left for Core demo:\n- #7 track.c filter\n- #8 baseline\n\nOk.\nThat is all for now."
    out = split_stream(text)
    # "Left for Core demo:" and "- #8 baseline Ok." are too short alone: joined.
    assert out == ["Left for Core demo: - #7 track.c filter",
                   "- #8 baseline Ok. That is all for now."]
    assert all(len(s) >= 15 for s in out)


def test_splitter_waits_for_lookahead_and_flushes_tail():
    sp = voice.SentenceSplitter()
    assert sp.feed("The rail is 1.") == []          # could be 1.55
    assert sp.feed("55 A at peak. And") == ["The rail is 1.55 A at peak."]
    assert sp.flush() == ["And"]
    assert sp.flush() == []


# ---------------------------------------------------------------- speakable


@pytest.mark.parametrize(("raw", "spoken"), [
    ("#31 is open.", "Issue 31 is open."),
    ("Issue #31 is open.", "Issue 31 is open."),
    ("It may be done: commit 02fb2d2 looks like it.", "It may be done: commit 0 2 f b looks like it."),
    ("See 02fb2d2.", "See commit 0 2 f b."),
    ("The rail draws 1.55 A of 2 A.", "The rail draws 1.55 amps of 2 amps."),
    ("The 5V rail and the 3.3 V rail.", "The 5 volt rail and the 3.3 volt rail."),
    ("Servo stall is 900 mA.", "Servo stall is 900 milliamps."),
    ("Needs 0.9/day to finish.", "Needs 0.9 per day to finish."),
    ("**Due** Oct 18 → in `10` days.", "Due Oct 18 to in 10 days."),
    ("- #7 Create track.c", "Issue 7 Create track.c"),
    ("Open: #7, #8, #9 and #10.", "Open: issues 7, 8, 9 and 10."),
    ("The rail is 5 V.", "The rail is 5 volts."),
    ("Tie the 5V ground to GND.", "Tie the 5 volt ground to GND."),
    ("1. First item", "First item"),
    ("## Status", "Status"),
    ("Done [#31] [commit:02fb2d2].", "Done."),
    ("Answer.\nSOURCES: #31, commit:02fb2d2", "Answer."),
    ("Answer.\n\nNote: #99 isn't an issue in this repo.", "Answer."),
    ("Answer.\nUnverified numbers: 6 (not in Jarvis's data).", "Answer."),
    ("Started track.c (Sep 30 to Oct 5, e.g. e9256c6).",
     "Started track.c (Sep 30 to Oct 5, for example commit e 9 2 5)."),
    ("1 A", "1 amp"),
])
def test_speakable(raw, spoken):
    assert voice.speakable(raw) == spoken


def test_condense_long_issue_lists():
    long = ("Open issues include #7 Create track.c with a filter, #8 Measure baseline, "
            "#9 State machine: survey / track / lost, and #28 Show detections.")
    assert voice.condense_issue_list(long) == "Open issues include issues 7, 8, 9 and 28."
    short = "#7 and #28 may already be done."
    assert voice.condense_issue_list(short) == short
    assert voice.speakable(voice.condense_issue_list(long)) == \
        "Open issues include issues 7, 8, 9 and 28."


def test_speakable_keeps_dates_and_plain_numbers():
    assert voice.speakable("Core demo is due Oct 18, in 10 days, with 0 of 9 closed.") == \
        "Core demo is due Oct 18, in 10 days, with 0 of 9 closed."


# ---------------------------------------------------------------- voice_answer


class RecordingSpeaker:
    def __init__(self, log):
        self.log = log
        self.said = []

    def say(self, text):
        self.said.append(text)
        self.log.append(("say", text))


def test_voice_answer_speaks_sentences_as_they_stream(tmp_path):
    conn = seeded()
    log = []
    chunks = ["Core demo is due Oct 18, in 10 days. ", "#31 is open but may ",
              "already be done (commit 02fb2d2).\n", "SOURCES: #31, milestone:Core demo"]

    class LoggingClient(FakeClient):
        def chat_stream(self, messages, options=None):
            self.messages = messages
            for c in self.chunks:
                log.append(("chunk", c))
                yield c

    client = LoggingClient(chunks)
    speaker = RecordingSpeaker(log)
    shown = []
    ans = voice.voice_answer(conn, cfg(tmp_path), "What's left for Core demo?", [], client,
                             speaker, on_token=shown.append, now=NOW)
    assert speaker.said == ["Core demo is due Oct 18, in 10 days.",
                            "Issue 31 is open but may already be done (commit 0 2 f b)."]
    # The first sentence is spoken before the second chunk even arrives.
    assert log.index(("say", speaker.said[0])) < log.index(("chunk", chunks[1]))
    assert "SOURCES" not in "".join(shown)
    assert ans.unverified_numbers == []
    assert chat.VOICE_RULES.strip() in client.messages[0]["content"]
    # prompt-cache friendly: the stable project data is in the system message
    assert "CONTEXT, part 1" in client.messages[0]["content"]
    assert client.messages[-1]["content"].startswith("CONTEXT, part 2:")


def test_voice_answer_speaks_unverified_note_at_end(tmp_path):
    conn = seeded()
    client = FakeClient(["Core demo has 17 issues left, plus 23 more. ", "SOURCES: milestone:Core demo"])
    speaker = RecordingSpeaker([])
    ans = voice.voice_answer(conn, cfg(tmp_path), "What's left?", [], client, speaker, now=NOW)
    assert ans.unverified_numbers
    assert speaker.said[-1] == voice.UNVERIFIED_SPOKEN
    assert "Unverified" not in " ".join(speaker.said[:-1])


def test_voice_answer_strips_think_and_speaks_model_error(tmp_path):
    conn = seeded()
    client = FakeClient(["<think>", "Let me count: 3+4=7.", "</think>\n\n",
                         "Core demo is due Oct 18, in 10 days.\nSOURCES: milestone:Core demo"])
    speaker = RecordingSpeaker([])
    shown = []
    ans = voice.voice_answer(conn, cfg(tmp_path), "When is Core demo due?", [], client,
                             speaker, on_token=shown.append, now=NOW)
    assert speaker.said == ["Core demo is due Oct 18, in 10 days."]
    assert "think" not in "".join(shown) and "3+4" not in ans.text
    assert ans.unverified_numbers == []

    down = FakeClient([], error="cannot reach Ollama at http://ollama.test: refused")
    speaker = RecordingSpeaker([])
    ans = voice.voice_answer(conn, cfg(tmp_path), "Anything?", [], down, speaker, now=NOW)
    assert ans.error and speaker.said and "isn't reachable" in speaker.said[0]


def test_voice_client_uses_voice_model_and_keep_alive():
    c = voice.voice_client(Config(ollama_url="http://ollama.test", voice_model="qwen3:14b"))
    try:
        assert c.model == "qwen3:14b" and c.keep_alive == "10m"
    finally:
        c.close()
