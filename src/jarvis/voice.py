"""Voice mode backend: microphone capture, speech-to-text, text-to-speech, playback.

No Qt here: everything works with plain threads and callbacks, so a GUI worker
thread can drive it and forward the callbacks as signals.

- ``Recorder`` runs ``pw-record`` (16 kHz s16 mono to stdout), reports a 0..1
  loudness level, and stops on its own after speech followed by silence.
- ``transcribe`` sends the WAV to Parakeet (OpenAI-compatible endpoint).
- ``Speaker`` turns queued sentences into audio with Kokoro and pipes the PCM
  into ``pw-play``; ``stop()`` cuts it off immediately (barge-in).
- ``SentenceSplitter`` + ``speakable`` turn a streamed answer into clean,
  speakable sentences, and ``voice_answer`` ties it all to ``chat.ask``.

Audio processes are created through injectable factories, so tests never touch
a real microphone or speaker.
"""
from __future__ import annotations

import io
import math
import queue
import re
import sqlite3
import subprocess
import threading
import time
import wave
from array import array
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from jarvis.briefing import SHA_RE
from jarvis.config import Config

VOICE_KEEP_ALIVE = "10m"      # keep the voice model loaded between turns, not forever
MIC_RATE = 16000              # pw-record capture rate (Parakeet wants 16 kHz)
TTS_RATE = 24000              # Kokoro PCM: s16 mono at 24 kHz
# Smaller context than typed chat (6000): prompt evaluation dominates time to first
# word (~1k tokens/s for qwen3:14b here). Facts, milestones, open issues and the
# electrical section still fit; older commits, closed issues and cards are trimmed.
VOICE_CONTEXT_TOKEN_BUDGET = 3000
# Question-independent context first (in the system message), so the warm voice
# model reuses its prompt cache between questions (chat.build_context).
VOICE_STABLE_PREFIX = True
UNVERIFIED_SPOKEN = "Some numbers couldn't be verified."
# Said while the reasoning model loads and thinks (a deep pass can take 30-60 s,
# most of it swapping qwen3 out for gemma4: both don't fit in VRAM together).
THINK_FILLER = "Let me think about that properly."
DOUBLE_CHECK_FILLER = "Let me double-check that."

STT_DOWN = ("Parakeet speech-to-text isn't running at {url}. "
            "Start it with: systemctl --user start parakeet-stt")
TTS_DOWN = ("Kokoro text-to-speech isn't running at {url}. "
            "Start it with: systemctl --user start kokoro-tts")
NO_PW_RECORD = ("pw-record was not found, so Jarvis can't use the microphone. "
                "It is part of PipeWire (package pipewire).")
NO_PW_PLAY = ("pw-play was not found, so Jarvis can't play speech. "
              "It is part of PipeWire (package pipewire).")
NO_MIC = "No microphone input: pw-record stopped right away ({detail})."


class VoiceError(RuntimeError):
    """A voice service or audio device problem; the message says what to do."""


# ---------------------------------------------------------------- audio helpers


def rms_s16(pcm: bytes) -> float:
    """Root mean square of little-endian s16 samples (0..32768)."""
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    a = array("h")
    a.frombytes(pcm[: 2 * n])
    if a.itemsize != 2:  # pragma: no cover - every supported platform has 2-byte shorts
        raise RuntimeError("array('h') is not 16-bit here")
    if not _LITTLE_ENDIAN:  # pragma: no cover
        a.byteswap()
    return math.sqrt(sum(x * x for x in a) / n)


_LITTLE_ENDIAN = array("h", [1]).tobytes() == b"\x01\x00"

LEVEL_FLOOR_DB = -60.0        # at or below: level 0
LEVEL_CEIL_DB = -10.0         # at or above: level 1


def dbfs(rms: float) -> float:
    """RMS of s16 samples in dB relative to full scale (silence -> -120)."""
    return 20 * math.log10(rms / 32768) if rms > 0 else -120.0


def level_from_rms(rms: float) -> float:
    """0..1 loudness for the orb: -60 dBFS -> 0, -10 dBFS -> 1, linear in dB."""
    v = (dbfs(rms) - LEVEL_FLOOR_DB) / (LEVEL_CEIL_DB - LEVEL_FLOOR_DB)
    return min(1.0, max(0.0, v))


def wav_bytes(pcm: bytes, rate: int = MIC_RATE, channels: int = 1) -> bytes:
    """A complete WAV file (s16) around raw PCM."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class _Proc(Protocol):
    """What Recorder/Speaker need from a subprocess (subprocess.Popen fits)."""

    stdin: Any
    stdout: Any
    returncode: int | None

    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...
    def poll(self) -> int | None: ...


ProcFactory = Callable[[list[str]], _Proc]


def _popen_reader(cmd: list[str]) -> _Proc:
    try:
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as e:
        raise VoiceError(NO_PW_RECORD) from e


def _popen_writer(cmd: list[str]) -> _Proc:
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except FileNotFoundError as e:
        raise VoiceError(NO_PW_PLAY) from e


def _kill(proc: _Proc | None) -> None:
    if proc is None:
        return
    try:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
    except Exception:  # noqa: BLE001 - best effort cleanup
        pass


# ---------------------------------------------------------------- recorder


class Recorder:
    """Record one utterance from the default microphone via ``pw-record``.

    ``record()`` blocks (run it on a worker thread) and returns WAV bytes, or
    None when nobody spoke. It ends:

    - after ``silence_s`` below the speech threshold, once speech has started;
    - at ``max_s`` (the cap; returns what was recorded);
    - after ``no_speech_s`` with no speech at all (returns None);
    - on ``stop()`` from another thread (returns the audio if at least
      ``min_stop_s`` was recorded, else None).

    The speech threshold adapts to the room: the quietest chunk of the first
    ``calib_s`` sets the noise floor, and speech is ``margin_db`` above it
    (clamped to -50..-25 dBFS). ``reason`` tells why it ended: "silence",
    "cap", "no_speech", "stopped" or "eof".
    """

    def __init__(self, *, on_level: Callable[[float], None] | None = None,
                 proc_factory: ProcFactory | None = None, rate: int = MIC_RATE,
                 chunk_ms: int = 50, silence_s: float = 1.2, max_s: float = 30.0,
                 no_speech_s: float = 8.0, calib_s: float = 0.3, margin_db: float = 12.0,
                 speech_chunks: int = 2, min_stop_s: float = 0.3) -> None:
        self.on_level = on_level
        self._factory = proc_factory or _popen_reader
        self.rate = rate
        self.chunk_bytes = max(2, int(rate * chunk_ms / 1000) * 2)
        self.silence_s = silence_s
        self.max_s = max_s
        self.no_speech_s = no_speech_s
        self.calib_s = calib_s
        self.margin_db = margin_db
        self.speech_chunks = speech_chunks
        self.min_stop_s = min_stop_s
        self.reason: str | None = None
        self.threshold_db: float | None = None
        self._stop = threading.Event()
        self._proc: _Proc | None = None
        self._lock = threading.Lock()

    @property
    def command(self) -> list[str]:
        return ["pw-record", "--raw", "--format", "s16", "--rate", str(self.rate),
                "--channels", "1", "-"]

    def stop(self) -> None:
        """End the recording now (safe from any thread)."""
        self._stop.set()
        with self._lock:
            proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.kill()
            except Exception:  # noqa: BLE001
                pass

    def _seconds(self, nbytes: int) -> float:
        return nbytes / (2 * self.rate)

    def record(self) -> bytes | None:
        self._stop.clear()
        self.reason = None
        self.threshold_db = None
        proc = self._factory(self.command)
        with self._lock:
            self._proc = proc
        if self._stop.is_set():  # stop() raced the start
            _kill(proc)
        pcm = bytearray()
        calib: list[float] = []
        loud_run = 0
        quiet_s = 0.0
        speech = False
        try:
            while True:
                chunk = proc.stdout.read(self.chunk_bytes) if proc.stdout else b""
                if self._stop.is_set():
                    self.reason = "stopped"
                    break
                if not chunk:
                    self.reason = "eof"
                    break
                pcm += chunk
                rms = rms_s16(chunk)
                if self.on_level:
                    self.on_level(level_from_rms(rms))
                db = dbfs(rms)
                elapsed = self._seconds(len(pcm))
                chunk_s = self._seconds(len(chunk))
                if self.threshold_db is None:
                    calib.append(db)
                    if elapsed >= self.calib_s:
                        floor = min(calib)
                        self.threshold_db = min(-25.0, max(-50.0, floor + self.margin_db))
                elif db >= self.threshold_db:
                    loud_run += 1
                    quiet_s = 0.0
                    if loud_run >= self.speech_chunks:
                        speech = True
                else:
                    loud_run = 0
                    if speech:
                        quiet_s += chunk_s
                        if quiet_s >= self.silence_s:
                            self.reason = "silence"
                            break
                if elapsed >= self.max_s:
                    self.reason = "cap"
                    break
                if not speech and elapsed >= self.no_speech_s:
                    self.reason = "no_speech"
                    break
        finally:
            stderr = b""
            if self.reason == "eof" and not pcm:
                try:
                    stderr = proc.stderr.read() if getattr(proc, "stderr", None) else b""
                except Exception:  # noqa: BLE001
                    stderr = b""
            _kill(proc)
            with self._lock:
                self._proc = None
            if self.on_level:
                self.on_level(0.0)
        if self.reason == "eof" and not pcm:
            detail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()
            raise VoiceError(NO_MIC.format(detail=detail[-1] if detail else
                                           f"exit code {proc.returncode}"))
        if self.reason == "no_speech":
            return None
        if self.reason in ("stopped", "eof") and not speech:
            if self._seconds(len(pcm)) < max(self.min_stop_s, self.calib_s):
                return None
        if not pcm:
            return None
        return wav_bytes(bytes(pcm), self.rate)


# ---------------------------------------------------------------- speech-to-text


def transcribe(wav: bytes, cfg: Config, *, model: str | None = None, timeout: float = 60,
               transport: httpx.BaseTransport | None = None) -> str:
    """Text of a WAV recording via Parakeet ``/v1/audio/transcriptions``.

    ``model`` None uses the server's default model. Raises VoiceError with a
    message naming the service and how to start it.
    """
    url = cfg.stt_url.rstrip("/")
    data = {"model": model} if model else {}
    try:
        with httpx.Client(base_url=url, timeout=timeout, transport=transport) as http:
            resp = http.post("/v1/audio/transcriptions",
                             files={"file": ("speech.wav", wav, "audio/wav")}, data=data)
    except httpx.TimeoutException as e:
        raise VoiceError(f"Parakeet speech-to-text at {url} timed out.") from e
    except httpx.HTTPError as e:
        raise VoiceError(STT_DOWN.format(url=url)) from e
    if resp.status_code != 200:
        raise VoiceError(f"Parakeet speech-to-text returned {resp.status_code}: "
                         f"{resp.text[:200]}")
    try:
        text = resp.json()["text"]
    except (ValueError, KeyError, TypeError) as e:
        raise VoiceError("Parakeet speech-to-text sent an unreadable reply.") from e
    return str(text or "").strip()


# ---------------------------------------------------------------- text-to-speech


def kokoro_stream(text: str, cfg: Config, *, speed: float = 1.0, timeout: float = 60,
                  transport: httpx.BaseTransport | None = None) -> Iterator[bytes]:
    """Raw PCM (s16 mono, 24 kHz) for ``text`` from Kokoro, streamed in chunks."""
    url = cfg.tts_url.rstrip("/")
    payload = {"model": "kokoro", "input": text, "voice": cfg.voice,
               "response_format": "pcm", "speed": speed, "stream": True}
    try:
        with httpx.Client(base_url=url, timeout=timeout, transport=transport) as http, \
                http.stream("POST", "/v1/audio/speech", json=payload) as resp:
            if resp.status_code != 200:
                body = resp.read().decode("utf-8", "replace")
                raise VoiceError(f"Kokoro text-to-speech returned {resp.status_code}: "
                                 f"{body[:200]}")
            yield from resp.iter_bytes()
    except httpx.TimeoutException as e:
        raise VoiceError(f"Kokoro text-to-speech at {url} timed out.") from e
    except httpx.HTTPError as e:
        raise VoiceError(TTS_DOWN.format(url=url)) from e


Synth = Callable[[str], Iterable[bytes]]

_END = object()  # end of one sentence's audio


class Speaker:
    """Speak queued sentences: Kokoro synthesis -> ``pw-play``, with barge-in.

    Two daemon threads: the synthesizer turns the next queued sentence into PCM
    while the player is still playing the current one (prefetch, so there is no
    gap between sentences), and the player writes PCM into one ``pw-play``
    process per speaking stretch, paced to real time so ``on_level`` follows
    what is actually heard. ``on_state`` gets "speaking" when audio starts and
    "idle" when the queue has drained (or after ``stop()``). Synthesis errors go
    to ``on_error`` (VoiceError message) and drop the rest of the queue.
    """

    FRAME_S = 0.02            # level/pacing granularity
    LEAD_S = 0.12             # how far ahead of real time writes may run

    def __init__(self, cfg: Config | None = None, *, synth: Synth | None = None,
                 proc_factory: ProcFactory | None = None, rate: int = TTS_RATE,
                 speed: float = 1.0,
                 on_level: Callable[[float], None] | None = None,
                 on_state: Callable[[str], None] | None = None,
                 on_error: Callable[[str], None] | None = None,
                 realtime: bool = True) -> None:
        cfg = cfg or Config()
        self._synth: Synth = synth or (lambda text: kokoro_stream(text, cfg, speed=speed))
        self._factory = proc_factory or _popen_writer
        self.rate = rate
        self.on_level = on_level
        self.on_state = on_state
        self.on_error = on_error
        self.realtime = realtime
        self._text_q: queue.Queue[tuple[int, str]] = queue.Queue()
        self._audio_q: queue.Queue[tuple[int, Any]] = queue.Queue(maxsize=256)
        self._gen = 0
        self._pending = 0            # sentences said but not fully played
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()
        self._proc: _Proc | None = None
        self._threads: list[threading.Thread] = []
        self._closed = False
        self.spoken: list[str] = []  # sentences whose audio finished (for tests/logs)

    @property
    def command(self) -> list[str]:
        return ["pw-play", "--raw", "--format", "s16", "--rate", str(self.rate), "--channels", "1", "-"]

    @property
    def speaking(self) -> bool:
        return not self._idle.is_set()

    def _start(self) -> None:
        if self._threads:
            return
        for target, name in ((self._synth_loop, "jarvis-tts"), (self._play_loop, "jarvis-play")):
            t = threading.Thread(target=target, name=name, daemon=True)
            t.start()
            self._threads.append(t)

    def say(self, text: str) -> None:
        """Queue one sentence (already speakable). Returns immediately."""
        text = text.strip()
        if not text or self._closed:
            return
        with self._lock:
            self._pending += 1
            self._idle.clear()
            gen = self._gen
        self._start()
        self._text_q.put((gen, text))

    def stop(self) -> None:
        """Barge-in: silence now, drop everything queued."""
        with self._lock:
            self._gen += 1
            self._pending = 0
            proc, self._proc = self._proc, None
        for q in (self._text_q, self._audio_q):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        _kill(proc)
        self._set_idle()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until everything queued has been played (True) or timeout (False)."""
        return self._idle.wait(timeout)

    def close(self, timeout: float = 2.0) -> None:
        """Stop, end both worker threads and wait for them (up to ``timeout``)."""
        self.stop()
        self._closed = True
        self._text_q.put((-1, ""))
        try:
            self._audio_q.put_nowait((-1, None))
        except queue.Full:  # pragma: no cover - stop() just emptied it
            pass
        deadline = time.monotonic() + timeout
        for t in self._threads:
            if t is not threading.current_thread():
                t.join(max(0.0, deadline - time.monotonic()))

    # -- internals

    def _set_idle(self) -> None:
        was = self._idle.is_set()
        self._idle.set()
        if not was:
            if self.on_level:
                self.on_level(0.0)
            if self.on_state:
                self.on_state("idle")

    def _current(self, gen: int) -> bool:
        return gen == self._gen

    def _put_audio(self, gen: int, item: Any) -> bool:
        while self._current(gen):
            try:
                self._audio_q.put((gen, item), timeout=0.1)
                return True
            except queue.Full:
                continue
        return False

    def _synth_loop(self) -> None:
        while True:
            gen, text = self._text_q.get()
            if gen < 0:
                return
            if not self._current(gen):
                continue
            carry = b""
            try:
                for chunk in self._synth(text):
                    if not self._current(gen):
                        break
                    data = carry + chunk
                    cut = len(data) - len(data) % 2
                    carry = data[cut:]
                    if cut and not self._put_audio(gen, data[:cut]):
                        break
                else:
                    self._put_audio(gen, (_END, text))
            except VoiceError as e:
                if self._current(gen):
                    if self.on_error:
                        self.on_error(str(e))
                    self.stop()
            except Exception as e:  # noqa: BLE001 - never kill the thread
                if self._current(gen):
                    if self.on_error:
                        self.on_error(f"Text-to-speech failed: {e}")
                    self.stop()

    def _open_player(self, gen: int) -> _Proc | None:
        try:
            proc = self._factory(self.command)
        except VoiceError as e:
            if self.on_error:
                self.on_error(str(e))
            self.stop()
            return None
        with self._lock:
            if not self._current(gen):
                _kill(proc)
                return None
            self._proc = proc
        if self.on_state:
            self.on_state("speaking")
        return proc

    def _finish_player(self, gen: int) -> None:
        """Close pw-play's stdin and let it drain (end of a speaking stretch)."""
        with self._lock:
            proc = self._proc if self._current(gen) else None
            self._proc = None
        if proc is None:
            return
        try:
            proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            proc.wait(timeout=10)
        except Exception:  # noqa: BLE001
            _kill(proc)

    def _play_loop(self) -> None:
        bps = 2 * self.rate
        frame = max(2, int(self.rate * self.FRAME_S) * 2)
        clock = 0.0          # monotonic time at which written audio ends
        while True:
            gen, item = self._audio_q.get()
            if gen < 0:
                return
            if not self._current(gen):
                continue
            if isinstance(item, tuple) and item and item[0] is _END:
                with self._lock:
                    if self._current(gen):
                        self._pending -= 1
                        self.spoken.append(item[1])
                    done = self._current(gen) and self._pending <= 0 and self._text_q.empty()
                if done and self._audio_q.empty():
                    if self.realtime:
                        time.sleep(max(0.0, clock - time.monotonic()))
                    self._finish_player(gen)
                    with self._lock:
                        still_done = self._current(gen) and self._pending <= 0
                    if still_done:
                        self._set_idle()
                continue
            with self._lock:
                proc = self._proc if self._current(gen) else None
            if proc is None:
                proc = self._open_player(gen)
                if proc is None:
                    continue
                clock = time.monotonic()
            data: bytes = item
            for k in range(0, len(data), frame):
                if not self._current(gen):
                    break
                piece = data[k:k + frame]
                if self.realtime:
                    now = time.monotonic()
                    clock = max(clock, now)
                    ahead = clock - now - self.LEAD_S
                    if ahead > 0:
                        time.sleep(ahead)
                if self.on_level:
                    self.on_level(level_from_rms(rms_s16(piece)))
                try:
                    proc.stdin.write(piece)
                    proc.stdin.flush()
                except (BrokenPipeError, OSError, ValueError):
                    break  # killed by stop(), or pw-play died
                clock += len(piece) / bps


# ---------------------------------------------------------------- sentence splitting


_ABBREV = frozenset("""
e.g i.e etc vs approx cf mr mrs ms dr prof st no nos fig ca resp incl
jan feb mar apr jun jul aug sep sept oct nov dec mon tue wed thu fri sat sun
""".split())


class SentenceSplitter:
    """Group streamed tokens into whole sentences for speech.

    A sentence ends at . ! ? (followed by whitespace) or a newline. Periods in
    abbreviations ("e.g.", "Oct."), decimals ("1.55"), versions ("v1.0") and
    file names ("track.c") don't end one. Pieces shorter than ``min_chars`` are
    joined with the next piece, so it never speaks fragments like "Yes." or a
    bare list marker; ``flush()`` returns whatever is left at the end.
    """

    def __init__(self, min_chars: int = 20) -> None:
        self.min_chars = min_chars
        self._buf = ""
        self._pending = ""   # a too-short sentence waiting for more

    def _emit(self, piece: str, out: list[str]) -> None:
        piece = piece.strip()
        if not piece:
            return
        joined = f"{self._pending} {piece}".strip() if self._pending else piece
        if len(joined) < self.min_chars:
            self._pending = joined
            return
        self._pending = ""
        out.append(joined)

    def _is_break(self, text: str, i: int) -> bool:
        """Does the terminator at text[i] end a sentence? (text[i+1] exists.)"""
        ch, nxt = text[i], text[i + 1]
        if ch == "\n":
            return True
        if not nxt.isspace() and nxt not in "\"')]":
            return False
        if ch in "!?":
            return True
        # ch == "."
        m = re.search(r"(\S+)$", text[:i])
        word = (m.group(1) if m else "").lstrip("(\"'[").lower()
        if word in _ABBREV:
            return False
        if len(word) == 1 and word.isalpha():  # initials, "A. Smith"
            return False
        return not (i > 0 and text[i - 1] == ".")  # "..." mid-thought

    def feed(self, token: str) -> list[str]:
        self._buf += token
        out: list[str] = []
        start = 0
        i = 0
        text = self._buf
        while i < len(text) - 1:
            if text[i] in ".!?\n" and self._is_break(text, i):
                j = i + 1
                while j < len(text) and text[j] in "\"')]":
                    j += 1
                self._emit(text[start:j], out)
                start = j
                i = j
                continue
            i += 1
        self._buf = text[start:]
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        rest, self._buf = self._buf, ""
        piece = rest.strip()
        joined = f"{self._pending} {piece}".strip() if self._pending else piece
        self._pending = ""
        if joined:
            out.append(joined)
        return out


# ---------------------------------------------------------------- speakable text


_UNITS = {
    "mA": ("milliamp", "milliamps"), "uA": ("microamp", "microamps"),
    "µA": ("microamp", "microamps"), "A": ("amp", "amps"),
    "mV": ("millivolt", "millivolts"), "V": ("volt", "volts"),
    "mW": ("milliwatt", "milliwatts"), "W": ("watt", "watts"),
    "kΩ": ("kilohm", "kilohms"), "Ω": ("ohm", "ohms"), "ohm": ("ohm", "ohms"),
    "MHz": ("megahertz", "megahertz"), "kHz": ("kilohertz", "kilohertz"),
    "Hz": ("hertz", "hertz"), "ms": ("millisecond", "milliseconds"),
    "us": ("microsecond", "microseconds"), "µs": ("microsecond", "microseconds"),
    "°C": ("degree Celsius", "degrees Celsius"), "mAh": ("milliamp hour", "milliamp hours"),
}
_UNIT_RE = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s?(" + "|".join(sorted(map(re.escape, _UNITS), key=len,
                                                      reverse=True)) + r")(?![A-Za-z0-9])")
_PER = {"day": "day", "d": "day", "week": "week", "wk": "week", "h": "hour", "hr": "hour",
        "hour": "hour", "s": "second", "sec": "second", "min": "minute", "month": "month"}
_PER_RE = re.compile(r"(\d)\s?/\s?(" + "|".join(_PER) + r")\b")
_NOTE_LINE_RE = re.compile(r"^\s*(?:Note|Unverified numbers)\s*:.*$", re.M | re.I)
_SOURCES_LINE_RE = re.compile(r"^\s*[*_`]*SOURCES?\s*[*_`]*\s*:.*$", re.M | re.I)


_SINGULAR = {many: one for one, many in _UNITS.values()}
_ADJ_NOUNS = ("rail|rails|supply|supplies|line|lines|regulator|regulators|battery|input|"
              "inputs|output|outputs|side|bus|domain|pin|pins|limit|budget|fuse|"
              "adapter|source|servo|servos|motor|motors|part|parts|device|devices|ground|gnd|"
              "connector|jack|level|logic|tolerance|range|margin|load|loads")


def _spell_sha(m: re.Match[str]) -> str:
    return "commit " + " ".join(m.group(0)[:4].lower())


def speakable(text: str) -> str:
    """Text cleaned up to be read aloud.

    Drops the SOURCES line, notes, markdown, bullets and bracketed refs;
    "#31" -> "issue 31"; a commit sha -> "commit 0 2 f b" (first 4 characters);
    units -> words ("1.55 A" -> "1.55 amps", "5V" -> "5 volts"); "0.9/day" ->
    "0.9 per day"; arrows and symbols -> words or nothing.
    """
    s = _SOURCES_LINE_RE.sub("", text)
    s = _NOTE_LINE_RE.sub("", s)
    s = re.sub(r"\[([^\]\n]+)\]\((?:[^)\s]+)\)", r"\1", s)          # [text](url)
    s = re.sub(r"\[(?:#\d+|(?:commit|milestone|card|check|fact):[^\]\n]*)\]", "", s)
    s = re.sub(r"^\s{0,3}#{1,6}\s+", "", s, flags=re.M)              # headings
    s = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", s, flags=re.M)      # bullets
    s = re.sub(r"(\*\*|__|`+|~~)", "", s)
    s = re.sub(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)", "", s)        # *em* _em_
    s = re.sub(r"\((?:uncertain|keyword hit[^)]*)\)", "", s, flags=re.I)
    s = re.sub(r"\be\.g\.,?", "for example", s, flags=re.I)
    s = re.sub(r"\bi\.e\.,?", "that is", s, flags=re.I)
    s = re.sub(r"\bvs\.?(?=\s)", "versus", s)
    s = re.sub(r"\s*(?:→|->|⇒|=>|⟶)\s*", " to ", s)
    s = re.sub(r"[←⇐↔⟵]", " ", s)
    s = re.sub(r"[~≈]\s?(?=\d)", "about ", s)
    s = s.replace("&", " and ").replace("—", ", ").replace("–", " to ")
    s = re.sub(r"\bissues?\s+#(\d+)", r"issue \1", s, flags=re.I)
    s = re.sub(r"#(\d+)", r"issue \1", s)
    s = re.sub(r"\bcommits?\s+(?=[0-9a-f]{7,40}\b)", "", s, flags=re.I)
    s = SHA_RE.sub(_spell_sha, s)
    s = _PER_RE.sub(lambda m: f"{m.group(1)} per {_PER[m.group(2)]}", s)

    def unit(m: re.Match[str]) -> str:
        num, u = m.group(1), m.group(2)
        one, many = _UNITS[u]
        return f"{num} {one if num == '1' else many}"

    s = _UNIT_RE.sub(unit, s)
    # "5 volts rail" -> "5 volt rail" (a unit used as an adjective is singular).
    s = re.sub(r"\b(\d+(?:\.\d+)?) (" + "|".join(sorted({m for _, m in _UNITS.values()
                                                       if not m.endswith("hertz")}, key=len,
                                                      reverse=True))
               + r")(?=[-\s](?:" + _ADJ_NOUNS + r")\b)",
               lambda m: f"{m.group(1)} {_SINGULAR[m.group(2)]}", s)
    # "issue 7, issue 8 and issue 9" -> "issues 7, 8 and 9"
    s = re.sub(r"\bissue (\d+)((?:,? (?:and |or )?issue \d+)+)",
               lambda m: "issues " + m.group(1) + re.sub(r"issue ", "", m.group(2)), s)
    s = re.sub(r"[ \t]*\n[ \t]*", " ", s)
    s = re.sub(r"\s+([,.;:!?])", r"\1", s)
    s = re.sub(r"\(\s*\)", "", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip(" \t,;:")
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------- one voice turn

MAX_SPOKEN_ISSUES = 3
_ISSUE_MENTION_RE = re.compile(r"#(\d+)")


def condense_issue_list(sentence: str, limit: int = MAX_SPOKEN_ISSUES) -> str:
    """A sentence naming more than ``limit`` issues, as just the issue numbers.

    "Open issues include #7 Create track.c..., #8 Measure..., ... and #28 Show..."
    -> "Open issues include issues 7, 8, ... and 28." Reading every title aloud takes
    far too long; the full list stays on screen.
    """
    nums = _ISSUE_MENTION_RE.findall(sentence)
    if len(nums) <= limit:
        return sentence
    head = sentence[: _ISSUE_MENTION_RE.search(sentence).start()].rstrip(" (:-")
    head = re.sub(r"\b(?:issues?|and)$", "", head, flags=re.I).rstrip(" ,:")
    listed = ", ".join(nums[:-1]) + " and " + nums[-1]
    return f"{head} issues {listed}.".strip()



def voice_client(cfg: Config) -> Any:
    """An OllamaClient for the voice model that keeps it loaded for 10 minutes."""
    from jarvis.ollama import OllamaClient
    return OllamaClient(cfg.ollama_url, cfg.voice_model, keep_alive=VOICE_KEEP_ALIVE)


def voice_answer(conn: sqlite3.Connection, cfg: Config, question: str,
                 history: Sequence[tuple[str, str]], client: Any | None, speaker: Any,
                 on_token: Callable[[str], None] | None = None, now: datetime | None = None,
                 *, session_id: str | None = None,
                 on_sentence: Callable[[str], None] | None = None,
                 budget_tokens: int | None = None,
                 stable_prefix: bool = VOICE_STABLE_PREFIX,
                 mode: str | None = None, deep_client: Any | None = None,
                 on_phase: Callable[[str, dict], None] | None = None,
                 deep_think: bool | None = None) -> Any:
    """Answer ``question`` aloud: chat.ask(brevity="voice") with sentence-by-sentence speech.

    Streamed display text goes to ``on_token`` (as with typed chat; replace it
    with ``answer.display_text`` at the end). Each complete sentence is cleaned
    by ``speakable`` and handed to ``speaker.say`` as soon as it has streamed,
    and to ``on_sentence``. When the answer has unverified numbers, "Some numbers
    couldn't be verified." is spoken last. If the model failed before saying
    anything, its error message is spoken. ``client`` None -> ``voice_client(cfg)``.
    Returns the chat.Answer. Blocks until the model is done (not until speech
    ends; use ``speaker.wait()`` for that). Safe on a worker thread.

    ``mode`` "auto" or "think" uses ``escalate.answer_with_escalation`` with
    ``client`` (the voice model) as the fast tier and ``deep_client`` (None ->
    cfg.model with reasoning, kept loaded 10 min) as the deep tier; ``on_phase``
    gets its phase events. Speech while escalating: the fast answer is spoken as it
    streams (a weak one is usually empty, so nothing is said); when the deep pass
    starts, a filler is spoken at once: "Let me double-check that." if part of the
    fast answer was already spoken, else "Let me think about that properly.". The
    fast answer's unspoken remainder is dropped. A template fallback answer is
    spoken as a whole. ``mode`` None = one plain pass on ``client`` (as before).
    ``deep_think`` is whether the deep tier reasons (None = only in "think" mode:
    voice auto escalates to gemma without reasoning, which answers in ~10 s where
    its reasoning never finished inside the 20 s voice cap).
    """
    from jarvis import chat

    if mode is not None:
        return _voice_escalated(conn, cfg, question, history, client, speaker,
                                on_token=on_token, now=now, session_id=session_id,
                                on_sentence=on_sentence, budget_tokens=budget_tokens,
                                stable_prefix=stable_prefix, mode=mode,
                                deep_client=deep_client, on_phase=on_phase,
                                deep_think=(mode == "think") if deep_think is None
                                else bool(deep_think))

    splitter = SentenceSplitter()
    spoke = False

    def speak(sentences: list[str]) -> None:
        nonlocal spoke
        for s in sentences:
            text = speakable(condense_issue_list(s))
            if not re.search(r"[A-Za-z0-9]", text):
                continue
            spoke = True
            speaker.say(text)
            if on_sentence:
                on_sentence(text)

    def token(t: str) -> None:
        if on_token:
            on_token(t)
        speak(splitter.feed(t))

    from jarvis import escalate, intent

    local = now if now is not None and now.tzinfo else (now or datetime.now(UTC)).astimezone()
    route = intent.route(question, local, conn)
    direct = escalate._direct_answer(conn, question, local, route)
    if direct is not None:          # a lookup code answers (agenda, capture, nothing recalled)
        direct.intent, direct.route_by = route.intent, route.by
        if session_id:
            chat.save_message(conn, session_id, "user", question.strip())
            chat.save_message(conn, session_id, "assistant", direct.text, {"tier": "direct"})
        token(direct.text)
        speak(splitter.flush())
        return direct
    own = client is None
    if client is None:
        client = voice_client(cfg)
    try:
        ans = chat.ask(conn, cfg, question, history, client, on_token=token, now=now,
                       session_id=session_id, brevity="voice",
                       budget_tokens=budget_tokens or VOICE_CONTEXT_TOKEN_BUDGET,
                       stable_prefix=stable_prefix, route=route)
    finally:
        if own:
            client.close()
    speak(splitter.flush())
    if not spoke and ans.text.strip():
        speak([ans.text])
    if ans.unverified_numbers:
        speak([UNVERIFIED_SPOKEN])
    return ans


def _voice_escalated(conn: sqlite3.Connection, cfg: Config, question: str,
                     history: Sequence[tuple[str, str]], client: Any | None, speaker: Any,
                     *, on_token: Callable[[str], None] | None, now: datetime | None,
                     session_id: str | None, on_sentence: Callable[[str], None] | None,
                     budget_tokens: int | None, stable_prefix: bool, mode: str,
                     deep_client: Any | None,
                     on_phase: Callable[[str, dict], None] | None,
                     deep_think: bool = True) -> Any:
    from jarvis import chat, escalate

    state = {"splitter": SentenceSplitter(), "spoke": False, "spoke_pass": False}

    def say(text: str) -> None:
        speaker.say(text)
        if on_sentence:
            on_sentence(text)

    def speak(sentences: list[str]) -> None:
        for s in sentences:
            text = speakable(condense_issue_list(s))
            if not re.search(r"[A-Za-z0-9]", text):
                continue
            state["spoke"] = state["spoke_pass"] = True
            say(text)

    def token(t: str) -> None:
        if on_token:
            on_token(t)
        speak(state["splitter"].feed(t))

    def phase(name: str, info: dict) -> None:
        if name == "escalating":
            info = {**info, "spoken": state["spoke"]}
        elif name == "discard_fast":
            state["splitter"] = SentenceSplitter()  # drop the fast answer's remainder
        elif name == "deep":
            say(DOUBLE_CHECK_FILLER if state["spoke"] else THINK_FILLER)
            state["spoke_pass"] = False
        elif name == "fallback":
            state["splitter"] = SentenceSplitter()
            state["spoke_pass"] = False
        if on_phase:
            on_phase(name, info)

    own = client is None
    if client is None:
        client = voice_client(cfg)
    try:
        ans = escalate.answer_with_escalation(
            conn, cfg, question, history, mode=mode, fast_client=client,
            deep_client=deep_client, on_token=token, on_phase=phase, brevity="voice",
            session_id=session_id, now=now,
            fast_budget=budget_tokens or VOICE_CONTEXT_TOKEN_BUDGET,
            deep_budget=chat.CONTEXT_TOKEN_BUDGET, stable_prefix=stable_prefix,
            deep_think=deep_think)
    finally:
        if own:
            client.close()
    speak(state["splitter"].flush())
    if not state["spoke_pass"] and ans.text.strip():
        speak([ans.text])
    if ans.unverified_numbers:
        speak([UNVERIFIED_SPOKEN])
    return ans
