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
