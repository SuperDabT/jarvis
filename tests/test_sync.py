from __future__ import annotations

import base64
import json

import httpx
import pytest

from jarvis import db
from jarvis.github import FetchResult, GitHubClient, GitHubError
from jarvis.sync import sync


class FakeClient:
    def __init__(self, conn, **results):
        self.conn = conn
        self.results = results

    def resource_key(self, name):
        return f"key:{name}"

    def _get(self, name):
        r = self.results.get(name, FetchResult(False, 304, None))
        if isinstance(r, Exception):
            raise r
        if r.changed:
            db.set_etag(self.conn, f"key:{name}", '"e"')  # like the real client
        return r

    def issues(self): return self._get("issues")
    def milestones(self): return self._get("milestones")
    def commits(self): return self._get("commits")
    def file(self, path): return self._get(path)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def ok(data):
    return FetchResult(True, 200, data)


ISSUES = [
    {"number": 1, "title": "Servo", "state": "open", "labels": [{"name": "servo"}, {"name": "bug"}],
     "milestone": {"number": 3}, "html_url": "u1"},
    {"number": 2, "title": "A PR", "state": "open", "labels": [], "pull_request": {}},
    {"number": 4, "title": "Plain", "state": "closed", "labels": [], "milestone": None,
     "closed_at": "2026-10-01T00:00:00Z"},
]


def test_issues_normalized_and_prs_skipped(conn):
    out = sync(conn, FakeClient(conn, issues=ok(ISSUES)))
    assert out["issues"] == "changed"
    rows = {r["number"]: r for r in conn.execute("SELECT * FROM issues")}
    assert set(rows) == {1, 4}
    assert json.loads(rows[1]["labels"]) == ["servo", "bug"]
    assert rows[1]["milestone_number"] == 3
    assert rows[4]["milestone_number"] is None


def test_missing_issue_not_deleted(conn):
    sync(conn, FakeClient(conn, issues=ok(ISSUES)))
    sync(conn, FakeClient(conn, issues=ok(ISSUES[:1])))
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 2


def test_unchanged_touches_nothing(conn):
    out = sync(conn, FakeClient(conn))
    assert set(out.values()) == {"unchanged"}
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM sync_log").fetchone()[0] == 4


def test_commits_and_milestones(conn):
    commits = [{"sha": "abc1234", "html_url": "cu", "commit": {
        "message": "first\n\nbody", "author": {"name": "Tal", "date": "d1"},
        "committer": {"date": "d2"}}},
        {"sha": "def", "commit": {"message": "m", "author": {"name": "T", "date": "d3"},
                                  "committer": {}}}]
    ms = [{"number": 3, "title": "M1", "state": "open", "open_issues": 1, "closed_issues": 2,
           "due_on": "2026-11-01T00:00:00Z"}]
    out = sync(conn, FakeClient(conn, commits=ok(commits), milestones=ok(ms)))
    assert out["commits"] == out["milestones"] == "changed"
    c = {r["sha"]: r for r in conn.execute("SELECT * FROM commits")}
    assert c["abc1234"]["message"] == "first\n\nbody"
    assert c["abc1234"]["author"] == "Tal"
    assert c["abc1234"]["committed_at"] == "d2"
    assert c["def"]["committed_at"] == "d3"
    assert conn.execute("SELECT closed_issues FROM milestones").fetchone()[0] == 2


def test_hardware_file_and_missing(conn):
    payload = {"sha": "s1", "encoding": "base64",
               "content": base64.b64encode(b"servos: 2\n").decode()}
    out = sync(conn, FakeClient(conn, **{"hardware.yaml": ok(payload)}))
    assert out["hardware.yaml"] == "changed"
    assert conn.execute("SELECT content, sha FROM repo_files").fetchone()[:] == ("servos: 2\n", "s1")
    out = sync(conn, FakeClient(conn, **{"hardware.yaml": FetchResult(False, 404, None)}))
    assert out["hardware.yaml"] == "missing"
    assert conn.execute("SELECT COUNT(*) FROM repo_files").fetchone()[0] == 0


def test_error_does_not_stop_others(conn):
    out = sync(conn, FakeClient(conn, issues=GitHubError(500, "boom"), milestones=ok([])))
    assert out["issues"].startswith("error: ")
    assert out["milestones"] == "changed"
    assert out["commits"] == "unchanged"


def test_etag_cleared_when_db_write_fails(conn):
    bad = [{"number": 1, "title": "ok", "state": "open"}, {"title": "no number"}]
    out = sync(conn, FakeClient(conn, issues=ok(bad), milestones=ok([])))
    assert out["issues"].startswith("error: ")
    assert db.get_etag(conn, "key:issues") is None
    assert conn.execute("SELECT COUNT(*) FROM issues").fetchone()[0] == 0  # rolled back
    assert db.get_etag(conn, "key:milestones") is not None


def test_resource_key_matches_stored_etag(conn):
    def handler(req):
        return httpx.Response(200, json=[], headers={"ETag": '"x"'})

    with GitHubClient("o/r", None, conn, transport=httpx.MockTransport(handler)) as gh:
        for name, call in [("issues", gh.issues), ("milestones", gh.milestones),
                           ("commits", gh.commits), ("hardware.yaml", lambda: gh.file("hardware.yaml"))]:
            call()
            assert db.get_etag(conn, gh.resource_key(name)) is not None, name
            db.clear_etag(conn, gh.resource_key(name))
            assert db.get_etag(conn, gh.resource_key(name)) is None


# ---------------------------------------------------------------- commit history

from datetime import UTC, datetime  # noqa: E402

from jarvis.sync import HISTORY_KEY  # noqa: E402

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _commit(sha, date):
    return {"sha": sha, "commit": {"message": f"m {sha}", "author": {"name": "T", "date": date},
                                   "committer": {"date": date}}}


class HistoryClient(FakeClient):
    def __init__(self, conn, history=None, **results):
        super().__init__(conn, **results)
        self.history = history or []
        self.calls = []

    def commit_history(self, since):
        self.calls.append(since)
        if isinstance(self.history, Exception):
            raise self.history
        return FetchResult(True, 200, self.history)


def test_first_sync_backfills_history_once(conn):
    old = [_commit("old1", "2026-05-01T10:00:00Z"), _commit("old2", "2026-07-02T10:00:00Z")]
    gh = HistoryClient(conn, history=old)
    out = sync(conn, gh, now=NOW)
    assert gh.calls == ["2026-04-08T12:00:00Z"]  # 183 days back
    assert out["commits"] == "changed"  # first page was a 304, but history added commits
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2
    assert db.get_sync_state(conn, HISTORY_KEY) == "2026-04-08T12:00:00Z"
    # Next sync: nothing changed upstream -> no history request at all.
    gh2 = HistoryClient(conn, history=old)
    out = sync(conn, gh2, now=NOW)
    assert gh2.calls == [] and out["commits"] == "unchanged"


def test_backfill_with_nothing_new_is_unchanged(conn):
    db.upsert_commit(conn, _commit_row_for("a1", "2026-10-01T00:00:00Z"))
    gh = HistoryClient(conn, history=[_commit("a1", "2026-10-01T00:00:00Z")])
    out = sync(conn, gh, now=NOW)
    assert out["commits"] == "unchanged"
    assert db.get_sync_state(conn, HISTORY_KEY) is not None


def _commit_row_for(sha, date):
    return {"sha": sha, "message": "m", "author": "T", "committed_at": date, "html_url": None}


def test_changed_first_page_fetches_incrementally_since_newest(conn):
    db.set_sync_state(conn, HISTORY_KEY, "2026-04-08T12:00:00Z")
    db.upsert_commit(conn, _commit_row_for("a1", "2026-10-05T09:30:00Z"))
    page = [_commit("n1", "2026-10-08T08:00:00Z")]
    gap = [_commit("n0", "2026-10-07T08:00:00Z"), *page]
    gh = HistoryClient(conn, history=gap, commits=ok(page))
    out = sync(conn, gh, now=NOW)
    assert gh.calls == ["2026-10-04T09:30:00Z"]  # newest stored minus a day
    assert out["commits"] == "changed"
    shas = {r[0] for r in conn.execute("SELECT sha FROM commits")}
    assert shas == {"a1", "n0", "n1"}


def test_history_error_is_retried_next_sync(conn):
    gh = HistoryClient(conn, history=GitHubError(403, "rate limited"))
    out = sync(conn, gh, now=NOW)
    assert out["commits"].startswith("error: history")
    assert db.get_sync_state(conn, HISTORY_KEY) is None
    gh2 = HistoryClient(conn, history=[_commit("x", "2026-09-01T00:00:00Z")])
    out = sync(conn, gh2, now=NOW)
    assert gh2.calls and out["commits"] == "changed"


def test_history_error_keeps_changed_first_page(conn):
    db.set_sync_state(conn, HISTORY_KEY, "2026-04-08T12:00:00Z")
    db.upsert_commit(conn, _commit_row_for("a1", "2026-10-05T09:30:00Z"))
    gh = HistoryClient(conn, history=GitHubError(500, "boom"),
                       commits=ok([_commit("n1", "2026-10-08T08:00:00Z")]))
    out = sync(conn, gh, now=NOW)
    assert out["commits"] == "changed"
    assert conn.execute("SELECT COUNT(*) FROM commits").fetchone()[0] == 2


def test_client_commit_history_follows_pages(conn):
    seen = []

    def handler(req):
        seen.append(dict(req.url.params))
        if req.url.params.get("page") == "2":
            return httpx.Response(200, json=[_commit("c3", "2026-09-01T00:00:00Z")])
        nxt = f"{req.url.copy_with(params={**dict(req.url.params), 'page': '2'})}"
        return httpx.Response(200, json=[_commit("c1", "2026-10-01T00:00:00Z"),
                                         _commit("c2", "2026-09-15T00:00:00Z")],
                              headers={"Link": f'<{nxt}>; rel="next"'})

    with GitHubClient("o/r", None, conn, transport=httpx.MockTransport(handler)) as gh:
        res = gh.commit_history("2026-04-08T12:00:00Z")
    assert [c["sha"] for c in res.data] == ["c1", "c2", "c3"]
    assert seen[0] == {"since": "2026-04-08T12:00:00Z", "per_page": "100"}
    assert len(seen) == 2


def test_client_commit_history_page_cap_and_empty_repo(conn):
    def handler(req):
        if req.url.params.get("since") == "empty":
            return httpx.Response(409, json={"message": "Git Repository is empty."})
        page = int(req.url.params.get("page", "1"))
        nxt = f"{req.url.copy_with(params={**dict(req.url.params), 'page': str(page + 1)})}"
        return httpx.Response(200, json=[_commit(f"p{page}", "2026-09-01T00:00:00Z")],
                              headers={"Link": f'<{nxt}>; rel="next"'})

    with GitHubClient("o/r", None, conn, transport=httpx.MockTransport(handler)) as gh:
        assert len(gh.commit_history("x", max_pages=3).data) == 3
        assert gh.commit_history("empty").data == []
        with pytest.raises(GitHubError):
            GitHubClient("o/r", None, conn, transport=httpx.MockTransport(
                lambda r: httpx.Response(403, json={"message": "nope"}))).commit_history("x")
