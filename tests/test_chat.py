from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from jarvis import chat, db
from jarvis.config import Config
from jarvis.ollama import OllamaClient, OllamaError

LOCAL = timezone(timedelta(hours=-7))
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=LOCAL)
REPO = "me/proj"


def cfg(tmp_path: Path) -> Config:
    # No hardware.yaml anywhere: hardware_path points at a missing file.
    return Config(repo=REPO, db_path=tmp_path / "x.db", hardware_path=tmp_path / "none.yaml",
                  ollama_url="http://ollama.test")


def issue(conn, number, title, state="open", milestone=None, labels=(), body="",
          created="2026-09-01T00:00:00Z", updated="2026-10-07T00:00:00Z", closed=None):
    db.upsert_issue(conn, {"number": number, "title": title, "state": state,
                           "labels": list(labels), "milestone_number": milestone, "body": body,
                           "html_url": f"https://github.com/{REPO}/issues/{number}",
                           "created_at": created, "updated_at": updated, "closed_at": closed})


def seeded():
    conn = db.connect(":memory:")
    db.upsert_milestone(conn, {"number": 1, "title": "Core demo", "state": "open",
                               "open_issues": 3, "closed_issues": 1,
                               "due_on": "2026-10-18T07:00:00Z",
                               "html_url": f"https://github.com/{REPO}/milestone/1"})
    issue(conn, 31, "Timestamp each detection when its run closes", milestone=1,
          body="Each detection gets a timestamp when its run closes.")
    issue(conn, 12, "servo_write returns a status", milestone=1,
          body="Out-of-range commands are silently rejected today.")
    issue(conn, 10, "Kalman filter on x/y", milestone=1, body="Smooth the target position.")
    issue(conn, 26, "Core demo plan", body="Day plan. " + "filler words here. " * 30
          + "Then the servo PWM check.")
    issue(conn, 27, "Custom shield PCB", state="closed", closed="2026-10-04T18:09:00Z")
    db.upsert_commit(conn, {"sha": "02fb2d2aaaa1111", "committed_at": "2026-10-06T01:52:25Z",
                            "message": "Report detections as their run closes, with timestamps"})
    db.upsert_commit(conn, {"sha": "e9256c6bbbb2222", "committed_at": "2026-09-10T01:00:00Z",
                            "message": "Start track.c"})
    return conn


# ---------------------------------------------------------------- context


def test_context_has_facts_milestones_titles_and_refs(tmp_path):
    conn = seeded()
    ctx = chat.build_context(conn, cfg(tmp_path), "what's up?", NOW)
    t = ctx.text
    assert "FACTS" in t and "[fact:milestone_deadline:1]" in t
    assert "(uncertain)" in t  # maybe_done:31 from commit 02fb2d2
    assert "[milestone:Core demo] Core demo: open, due Oct 18, 2026 (in 10 days)" in t
    assert "1 of 4 issues closed" in t and "open: #10, #12, #31" in t
    for n in (31, 12, 10, 26):
        assert f"[#{n}]" in t
    assert "[#27] Custom shield PCB | closed" in t
    assert "[commit:02fb2d2]" in t
    assert "Today: Thursday Oct 8, 2026" in t
    assert "No hardware.yaml found" in t
    assert ctx.refs["#31"].url == f"https://github.com/{REPO}/issues/31"
    assert ctx.refs["commit:02fb2d2"].url == f"https://github.com/{REPO}/commit/02fb2d2aaaa1111"
    assert ctx.refs["milestone:Core demo"].url.endswith("/milestone/1")
    assert ctx.refs["fact:activity"].url is None
    assert "body:" not in t  # no keyword overlap -> no bodies
    assert ctx.issue_numbers == {31, 12, 10, 26, 27}


def test_context_stays_under_budget_with_many_issues(tmp_path):
    conn = seeded()
    for n in range(100, 700):
        issue(conn, n, f"Generic task number {n} about widgets and gadgets",
              labels=["misc"], body="x" * 2000)
    for k in range(80):
        db.upsert_commit(conn, {"sha": f"{k:04x}abcdef{k:03d}", "message": f"work item {k}",
                                "committed_at": f"2026-10-0{1 + k % 7}T12:00:00Z"})
    ctx = chat.build_context(conn, cfg(tmp_path), "is #31 done?", NOW)
    assert ctx.tokens <= chat.CONTEXT_TOKEN_BUDGET
    assert "[fact:milestone_deadline:1]" in ctx.text
    assert "[milestone:Core demo]" in ctx.text
    assert "[#31]" in ctx.text and "Each detection gets a timestamp" in ctx.text
    assert ctx.omitted.get("open", 0) > 0
    assert "more not shown" in ctx.text
    # Every ref kept is actually present in the text.
    assert all(f"[{r}]" in ctx.text for r in ctx.refs)


def test_keyword_relevance_pulls_matching_bodies(tmp_path):
    conn = seeded()
    ctx = chat.build_context(conn, cfg(tmp_path), "Which issues are about the servos?", NOW)
    assert ctx.body_issues[:2] == [12, 26]
    assert "Out-of-range commands are silently rejected" in ctx.text
    assert "keyword hit: servo" in ctx.text
    # #26 matches only late in its body: the excerpt shows the matching part.
    line26 = next(l for l in ctx.text.split("[#26]")[1].splitlines() if "body:" in l)
    assert "servo PWM" in line26
    assert "Smooth the target position" not in ctx.text  # #10 is unrelated


def test_question_about_unknown_or_closed_issue(tmp_path):
    conn = seeded()
    ctx = chat.build_context(conn, cfg(tmp_path), "Is #27 done? and #99?", NOW)
    assert "ISSUES ASKED ABOUT" in ctx.text
    assert "there is no issue #99" in ctx.text
    assert "[#27] Custom shield PCB | closed" in ctx.text


def test_time_window_filters_commits(tmp_path):
    assert chat.time_window("What did I work on in the last two weeks?") == 14
    assert chat.time_window("last week?") == 7
    assert chat.time_window("past 3 days") == 3
    assert chat.time_window("is #31 done") is None
    conn = seeded()
    ctx = chat.build_context(conn, cfg(tmp_path), "what did I do in the last two weeks?", NOW)
    assert "[commit:02fb2d2]" in ctx.text and "[commit:e9256c6]" not in ctx.text
    assert "THE LAST 14 DAYS" in ctx.text and "(Mon, 3 days ago)" in ctx.text


def test_embedding_rank_adds_bodies_and_falls_back(tmp_path):
    conn = seeded()

    class Emb:
        def embed(self, texts, model=None):
            # question ~ Kalman issue only
            return [[1.0, 0.0] if ("Kalman" in t or "smoothing" in t) else [0.0, 1.0]
                    for t in texts]

    chat._EMBED_CACHE.clear()
    ctx = chat.build_context(conn, cfg(tmp_path), "how is target smoothing going?", NOW,
                             embedder=Emb())
    assert ctx.ranking == "keywords+embeddings"
    assert 10 in ctx.body_issues and "Smooth the target position" in ctx.text

    class Down:
        def embed(self, texts, model=None):
            raise OllamaError("cannot reach Ollama")

    chat._EMBED_CACHE.clear()
    ctx = chat.build_context(conn, cfg(tmp_path), "how is target smoothing going?", NOW,
                             embedder=Down())
    assert ctx.ranking == "keywords"


# ---------------------------------------------------------------- streaming


def ndjson(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs).encode()


def stream_client(handler):
    return OllamaClient("http://ollama.test", model="m1", transport=httpx.MockTransport(handler))


def test_chat_stream_yields_chunks_until_done():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, content=ndjson(
            {"message": {"role": "assistant", "content": "Hel"}, "done": False},
            {"message": {"role": "assistant", "content": "lo"}, "done": False},
            {"message": {"role": "assistant", "content": ""}, "done": True, "eval_count": 3},
            {"message": {"content": "IGNORED"}, "done": False},
        ))

    with stream_client(handler) as c:
        out = list(c.chat_stream([{"role": "user", "content": "hi"}], {"num_predict": 5}))
        assert c.last_stream_stats["eval_count"] == 3
    assert out == ["Hel", "lo"]
    body = seen["body"]
    assert body["stream"] is True and body["think"] is False and body["model"] == "m1"
    assert body["options"] == {"temperature": 0.2, "num_predict": 5}


@pytest.mark.parametrize("content", [
    ndjson({"message": {"content": "a"}}, {"error": "model crashed"}),
    ndjson({"message": {"content": "a"}}),  # no done line
    b'{"message": {"content": "a"}}\nnot json\n',
])
def test_chat_stream_errors(content):
    with stream_client(lambda r: httpx.Response(200, content=content)) as c:
        it = c.chat_stream([{"role": "user", "content": "x"}])
        assert next(it) == "a"
        with pytest.raises(OllamaError):
            list(it)


def test_chat_stream_http_errors_and_think_retry():
    with stream_client(lambda r: httpx.Response(404, json={"error": "no model"})) as c:
        with pytest.raises(OllamaError, match="404"):
            list(c.chat_stream([]))

    def refuse(request):
        raise httpx.ConnectError("refused")

    with stream_client(refuse) as c:
        with pytest.raises(OllamaError, match="cannot reach"):
            list(c.chat_stream([]))

    bodies = []

    def think_handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "does not support thinking"})
        return httpx.Response(200, content=ndjson({"message": {"content": "ok"}, "done": True}))

    with stream_client(think_handler) as c:
        assert list(c.chat_stream([])) == ["ok"]
    assert ["think" in b for b in bodies] == [True, False]


def test_sources_filter_holds_back_sources_line():
    f = chat.SourcesLineFilter()
    chunks = ["#31 is ", "open.\nSo", "me more.\n", "SOU", "RCES: #3", "1, commit:02fb2d2"]
    shown = "".join(f.feed(c) for c in chunks) + f.flush()
    assert shown == "#31 is open.\nSome more.\n"
    f = chat.SourcesLineFilter()
    shown = "".join(f.feed(c) for c in ["Done.\n", "**Sources:** #1"]) + f.flush()
    assert shown == "Done.\n"
    f = chat.SourcesLineFilter()
    assert f.feed("SOURCE") == "" and f.flush() == "SOURCE"


# ---------------------------------------------------------------- validation


def ctx_for(tmp_path, question="Is #31 done?"):
    return chat.build_context(seeded(), cfg(tmp_path), question, NOW)


def test_sources_parsing_drops_unknown_refs(tmp_path):
    ctx = ctx_for(tmp_path)
    raw = ("#31 is open but may already be done: commit 02fb2d2 looks like the same work.\n\n"
           "SOURCES: #31, commit:02fb2d2, fact:maybe_done:31, #77, made-up, milestone:core demo")
    ans = chat.validate_answer(raw, ctx, "Is #31 done?")
    assert ans.text.endswith("looks like the same work.")
    assert "SOURCES" not in ans.text
    refs = [s.ref for s in ans.sources]
    assert refs[:4] == ["#31", "commit:02fb2d2", "fact:maybe_done:31", "milestone:Core demo"]
    assert ans.dropped_refs == ["#77", "made-up"]
    assert ans.notes == [] and ans.unverified_numbers == []


def test_inline_refs_count_as_sources_without_sources_line(tmp_path):
    ctx = ctx_for(tmp_path)
    ans = chat.validate_answer("#12 and #31 are open.", ctx, "q")
    assert [s.ref for s in ans.sources] == ["#12", "#31"]


def test_invented_issue_and_sha_get_notes(tmp_path):
    ctx = ctx_for(tmp_path)
    ans = chat.validate_answer("#99 was closed by commit abc1234.\nSOURCES: #99", ctx, "q")
    assert "Note: #99 isn't an issue in this repo." in ans.notes
    assert any("abc1234" in n and "synced commits" in n for n in ans.notes)
    assert "Note: #99" in ans.display_text


def test_unverified_numbers_detected(tmp_path):
    ctx = ctx_for(tmp_path, "What's left for Core demo?")
    raw = ("Core demo is due Oct 18, in 10 days, with 1 of 4 closed; about 42 hours of work "
           "remain, due Oct 30, and two weeks.\nSOURCES: milestone:Core demo")
    ans = chat.validate_answer(raw, ctx, "What's left for Core demo?")
    assert ans.unverified_numbers == ["Oct 30", "42"]
    assert any(n.startswith("Unverified numbers: Oct 30, 42") for n in ans.notes)
    # Numbers the user typed are fine.
    ans = chat.validate_answer("Nothing about 42 here.", ctx, "what about 42?")
    assert ans.unverified_numbers == []


# ---------------------------------------------------------------- ask


class FakeClient:
    model = "fake-model"

    def __init__(self, chunks=None, error=None):
        self.chunks = chunks or []
        self.error = error
        self.messages = None

    def chat_stream(self, messages, options=None):
        self.messages = messages
        yield from self.chunks
        if self.error:
            raise OllamaError(self.error)

    def embed(self, texts, model=None):
        raise OllamaError("no embeddings here")


def test_ask_streams_validates_and_persists(tmp_path):
    conn = seeded()
    client = FakeClient(["#31 is open but ", "may already be done (commit 02fb2d2).\n",
                         "SOURCES: #31, ", "commit:02fb2d2"])
    shown = []
    ans = chat.ask(conn, cfg(tmp_path), "Is #31 done?", [], client, on_token=shown.append,
                   now=NOW, session_id="s1")
    assert "".join(shown) == "#31 is open but may already be done (commit 02fb2d2).\n"
    assert ans.text == "#31 is open but may already be done (commit 02fb2d2)."
    assert [s.ref for s in ans.sources] == ["#31", "commit:02fb2d2"]
    assert ans.model == "fake-model" and ans.error is None
    assert ans.first_token_s is not None and ans.duration >= ans.first_token_s
    sys_msg, user_msg = client.messages[0], client.messages[-1]
    assert sys_msg["role"] == "system" and "SOURCES:" in sys_msg["content"]
    assert user_msg["content"].startswith("CONTEXT:\n") and user_msg["content"].endswith(
        "QUESTION: Is #31 done?")
    rows = conn.execute("SELECT * FROM chat_messages ORDER BY id").fetchall()
    assert [(r["session_id"], r["role"]) for r in rows] == [("s1", "user"), ("s1", "assistant")]
    assert rows[0]["content"] == "Is #31 done?" and rows[1]["content"] == ans.text
    meta = json.loads(rows[1]["sources_json"])
    assert meta["sources"][0]["ref"] == "#31" and meta["notes"] == []
    assert chat.load_history(conn, "s1") == [("user", "Is #31 done?"), ("assistant", ans.text)]
    assert chat.load_history(conn, "other") == []


def test_ask_ollama_down_returns_message(tmp_path):
    conn = seeded()
    client = FakeClient(error="cannot reach Ollama at http://ollama.test: refused")
    ans = chat.ask(conn, cfg(tmp_path), "Is #31 done?", [], client, now=NOW, session_id="s")
    assert ans.text == ("The local model isn't reachable (Ollama at http://ollama.test). "
                        "Try again once it's running.")
    assert ans.error and ans.sources == []
    roles = [r["role"] for r in conn.execute("SELECT role FROM chat_messages")]
    assert roles == ["user"]  # failed answers are not stored as history


def test_ask_real_client_unreachable(tmp_path):
    def refuse(request):
        raise httpx.ConnectError("refused")

    client = stream_client(refuse)
    ans = chat.ask(seeded(), cfg(tmp_path), "hi", [], client, now=NOW)
    assert "isn't reachable" in ans.text


def test_ask_cut_off_mid_stream_keeps_partial(tmp_path):
    client = FakeClient(["#31 is open"], error="Ollama error: crashed")
    ans = chat.ask(seeded(), cfg(tmp_path), "Is #31 done?", [], client, now=NOW)
    assert ans.text == "#31 is open" and ans.error
    assert any("cut off" in n for n in ans.notes)


def test_history_trimming_and_messages(tmp_path):
    hist = []
    for k in range(10):
        hist += [("user", f"q{k}"), ("assistant", f"a{k}")]
    trimmed = chat.trim_history(hist)
    assert len(trimmed) == 2 * chat.HISTORY_TURNS and trimmed[0] == ("user", "q4")
    assert chat.trim_history([("assistant", "x"), ("user", "q"), ("system", "s")]) == [
        ("user", "q")]
    client = FakeClient(["ok"])
    chat.ask(seeded(), cfg(tmp_path), "and #12?", hist, client, now=NOW)
    roles = [m["role"] for m in client.messages]
    assert roles == ["system"] + ["user", "assistant"] * chat.HISTORY_TURNS + ["user"]
    assert client.messages[1]["content"] == "q4"


def test_history_issue_mentions_shape_context(tmp_path):
    conn = seeded()
    ctx = chat.build_context(conn, cfg(tmp_path), "and its body?", NOW,
                             history=[("user", "is #12 done?"), ("assistant", "No.")])
    assert "Out-of-range commands are silently rejected" in ctx.text


def test_chat_messages_table_in_schema(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chat_messages)")}
    assert cols == {"id", "session_id", "role", "content", "sources_json", "created_at"}
    db.connect(tmp_path / "s.db")  # idempotent


def test_no_note_when_answer_already_says_issue_is_missing(tmp_path):
    ctx = ctx_for(tmp_path, "Is #99 done?")
    for text in ("There is no issue #99 in the synced data.", "#99 does not exist in this repo."):
        assert chat.validate_answer(text, ctx, "Is #99 done?").notes == []
    assert chat.validate_answer("#99 is done.", ctx, "q").notes
