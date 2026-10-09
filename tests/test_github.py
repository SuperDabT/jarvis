from __future__ import annotations

import base64

import httpx
import pytest

from jarvis.db import connect, get_etag
from jarvis.github import FetchResult, GitHubClient, GitHubError, decode_file_content

REPO = "owner/repo"
ISSUES_KEY = "https://api.github.com/repos/owner/repo/issues?per_page=100&state=all"


@pytest.fixture
def conn():
    c = connect(":memory:")
    yield c
    c.close()


def make_client(conn, handler, token=None):
    return GitHubClient(REPO, token, conn, transport=httpx.MockTransport(handler))


def test_conditional_get_roundtrip(conn):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.headers.get("If-None-Match") == '"abc"':
            return httpx.Response(304)
        return httpx.Response(
            200, json=[{"sha": "1"}], headers={"ETag": '"abc"', "Last-Modified": "Mon, 01 Jan 2026 00:00:00 GMT"}
        )

    with make_client(conn, handler) as gh:
        r1 = gh.commits()
        assert r1 == FetchResult(changed=True, status=200, data=[{"sha": "1"}])
        assert "If-None-Match" not in seen[0].headers
        key = "https://api.github.com/repos/owner/repo/commits?per_page=30"
        row = get_etag(conn, key)
        assert row["etag"] == '"abc"'
        assert row["last_modified"] == "Mon, 01 Jan 2026 00:00:00 GMT"

        r2 = gh.commits()
        assert seen[1].headers["If-None-Match"] == '"abc"'
        assert seen[1].headers["If-Modified-Since"] == "Mon, 01 Jan 2026 00:00:00 GMT"
        assert r2 == FetchResult(changed=False, status=304, data=None)


def test_etag_not_stored_on_invalid_json(conn):
    def handler(req):
        return httpx.Response(200, content=b"not json{", headers={"ETag": '"x"'})

    with make_client(conn, handler) as gh:
        with pytest.raises(GitHubError):
            gh.get("/repos/owner/repo/commits")
    assert conn.execute("SELECT COUNT(*) FROM etags").fetchone()[0] == 0


def test_404_returns_unchanged_without_etag(conn):
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found"}, headers={"ETag": '"n"'})

    with make_client(conn, handler) as gh:
        r = gh.file("hardware.yaml")
    assert r == FetchResult(changed=False, status=404, data=None)
    assert conn.execute("SELECT COUNT(*) FROM etags").fetchone()[0] == 0


def test_500_raises_without_etag(conn):
    def handler(req):
        return httpx.Response(500, json={"message": "boom"}, headers={"ETag": '"e"'})

    with make_client(conn, handler) as gh:
        with pytest.raises(GitHubError) as ei:
            gh.get("/repos/owner/repo")
    assert ei.value.status == 500
    assert "boom" in str(ei.value)
    assert conn.execute("SELECT COUNT(*) FROM etags").fetchone()[0] == 0


def test_query_params_use_distinct_keys(conn):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json=[], headers={"ETag": f'"{req.url.params["per_page"]}"'})

    with make_client(conn, handler) as gh:
        gh.commits(per_page=10)
        gh.commits(per_page=20)
        gh.commits(per_page=10)
    keys = {r["url"]: r["etag"] for r in conn.execute("SELECT * FROM etags")}
    assert keys == {
        "https://api.github.com/repos/owner/repo/commits?per_page=10": '"10"',
        "https://api.github.com/repos/owner/repo/commits?per_page=20": '"20"',
    }
    assert "If-None-Match" not in seen[1].headers
    assert seen[2].headers["If-None-Match"] == '"10"'


def test_cache_key_sorts_params(conn):
    def handler(req):
        return httpx.Response(200, json={}, headers={"ETag": '"s"'})

    with make_client(conn, handler) as gh:
        gh.get("/x", params={"b": "2", "a": "1"})
    assert get_etag(conn, "https://api.github.com/x?a=1&b=2") is not None


def test_pagination_follows_next_and_concatenates(conn):
    seen = []

    def handler(req):
        seen.append(req)
        page = req.url.params.get("page", "1")
        if page == "1":
            return httpx.Response(
                200,
                json=[{"number": 1}, {"number": 2}],
                headers={
                    "ETag": '"p1"',
                    "Link": '<https://api.github.com/repos/owner/repo/issues?state=all&per_page=100&page=2>; rel="next", '
                    '<https://api.github.com/repos/owner/repo/issues?state=all&per_page=100&page=2>; rel="last"',
                },
            )
        return httpx.Response(200, json=[{"number": 3}], headers={"ETag": '"p2"'})

    with make_client(conn, handler) as gh:
        r = gh.issues()
    assert r.changed and r.status == 200
    assert [i["number"] for i in r.data] == [1, 2, 3]
    assert len(seen) == 2
    assert seen[0].url.params["per_page"] == "100"
    assert seen[0].url.params["state"] == "all"
    rows = conn.execute("SELECT url, etag FROM etags").fetchall()
    assert [(r["url"], r["etag"]) for r in rows] == [(ISSUES_KEY, '"p1"')]


def test_pagination_304_on_first_page_stops(conn):
    from jarvis.db import set_etag

    set_etag(conn, ISSUES_KEY, '"p1"')
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(304)

    with make_client(conn, handler) as gh:
        r = gh.issues()
    assert r == FetchResult(changed=False, status=304, data=None)
    assert len(seen) == 1
    assert seen[0].headers["If-None-Match"] == '"p1"'


def test_pagination_failure_on_later_page_does_not_store_etag(conn):
    def handler(req):
        if req.url.params.get("page") == "2":
            return httpx.Response(500, json={"message": "boom"})
        return httpx.Response(
            200,
            json=[{"number": 1}],
            headers={"ETag": '"p1"', "Link": '<https://api.github.com/repos/owner/repo/issues?page=2>; rel="next"'},
        )

    with make_client(conn, handler) as gh:
        with pytest.raises(GitHubError):
            gh.issues()
    assert get_etag(conn, ISSUES_KEY) is None


def test_authorization_header_only_with_token(conn):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(200, json={})

    with make_client(conn, handler) as gh:
        gh.get("/a")
    with make_client(conn, handler, token="t0k") as gh:
        gh.get("/b")
    assert "Authorization" not in seen[0].headers
    assert seen[1].headers["Authorization"] == "Bearer t0k"
    for req in seen:
        assert req.headers["Accept"] == "application/vnd.github+json"
        assert req.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert req.headers["User-Agent"] == "jarvis"


def test_rate_limit_403_mentions_reset(conn):
    def handler(req):
        return httpx.Response(
            403,
            json={"message": "API rate limit exceeded"},
            headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1767225600"},
        )

    with make_client(conn, handler) as gh:
        with pytest.raises(GitHubError) as ei:
            gh.issues()
    msg = str(ei.value)
    assert ei.value.status == 403
    assert "rate limit exceeded" in msg
    assert "remaining=0" in msg
    assert "2026-01-01T00:00:00+00:00" in msg
    assert "1767225600" in msg


def test_decode_file_content_roundtrip():
    text = "parts:\n  servo: {volts: 5.0, amps: 1.2}\n  µ-controller: STM32\n"
    encoded = base64.b64encode(text.encode()).decode()
    # GitHub wraps base64 at 60 chars with newlines
    wrapped = "\n".join(encoded[i : i + 60] for i in range(0, len(encoded), 60))
    assert decode_file_content({"content": wrapped, "encoding": "base64"}) == text


def test_poll_interval_from_events(conn):
    calls = {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[], headers={"X-Poll-Interval": "90", "ETag": '"ev"'})
        return httpx.Response(304, headers={"X-Poll-Interval": "120"})

    with make_client(conn, handler) as gh:
        assert gh.poll_interval == 60
        r = gh.events()
        assert r.changed and gh.poll_interval == 90
        r = gh.events()
        assert not r.changed and gh.poll_interval == 120
