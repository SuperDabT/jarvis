"""Thin GitHub REST client with ETag-based conditional requests.

ETags are persisted in the ``etags`` table (see :mod:`jarvis.db`), keyed by
the full request URL including sorted query parameters.
"""
from __future__ import annotations

import base64
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from jarvis.db import get_etag, set_etag

API_BASE = "https://api.github.com"
DEFAULT_POLL_INTERVAL = 60
HISTORY_MAX_PAGES = 20  # commit history backfill: at most 2000 commits


class GitHubError(Exception):
    """Unexpected GitHub response (anything but 200/304/404, or a bad body)."""

    def __init__(self, status: int, message: str, url: str | None = None) -> None:
        self.status = status
        self.message = message
        self.url = url
        super().__init__(f"GitHub {status}: {message}" + (f" ({url})" if url else ""))


@dataclass
class FetchResult:
    changed: bool
    status: int
    data: list | dict | None


def decode_file_content(data: dict) -> str:
    """Decode the base64 ``content`` field of a contents-API file payload."""
    encoding = data.get("encoding", "base64")
    content = data.get("content") or ""
    if encoding != "base64":
        raise ValueError(f"unsupported content encoding: {encoding!r}")
    return base64.b64decode(content).decode("utf-8")


def _cache_key(url: httpx.URL) -> str:
    params = sorted(url.params.multi_items())
    return str(url.copy_with(params=params) if params else url.copy_with(query=None))


def _error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        msg = body.get("message") if isinstance(body, dict) else None
    except ValueError:
        msg = None
    msg = msg or resp.text[:200] or resp.reason_phrase
    if resp.status_code in (403, 429):
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset = resp.headers.get("X-RateLimit-Reset")
        if remaining is not None:
            msg += f"; rate limit remaining={remaining}"
        if reset is not None:
            try:
                when = datetime.fromtimestamp(int(reset), UTC).isoformat(timespec="seconds")
                msg += f"; resets at {when} (epoch {reset})"
            except ValueError:
                msg += f"; reset={reset}"
        retry_after = resp.headers.get("Retry-After")
        if retry_after is not None:
            msg += f"; retry after {retry_after}s"
    return msg


class GitHubClient:
    def __init__(
        self,
        repo: str,
        token: str | None,
        conn: sqlite3.Connection,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.repo = repo
        self.conn = conn
        self.poll_interval = DEFAULT_POLL_INTERVAL
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jarvis",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=API_BASE, timeout=20, headers=headers, transport=transport
        )

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- core ------------------------------------------------------------
    def _conditional(
        self, path: str, params: dict | None
    ) -> tuple[httpx.Response, str, Any]:
        """Send a conditional GET. Returns (response, cache_key, parsed_json_or_None).

        Raises GitHubError for statuses other than 200/304/404 or unparsable 200 bodies.
        Does not store the ETag; callers do that once they are done.
        """
        request = self._http.build_request("GET", path, params=params)
        key = _cache_key(request.url)
        saved = get_etag(self.conn, key)
        if saved is not None:
            if saved["etag"]:
                request.headers["If-None-Match"] = saved["etag"]
            if saved["last_modified"]:
                request.headers["If-Modified-Since"] = saved["last_modified"]
        resp = self._http.send(request)
        if resp.status_code in (304, 404):
            return resp, key, None
        if resp.status_code != 200:
            raise GitHubError(resp.status_code, _error_message(resp), key)
        return resp, key, self._parse(resp, key)

    @staticmethod
    def _parse(resp: httpx.Response, key: str) -> Any:
        try:
            return resp.json()
        except ValueError as e:
            raise GitHubError(resp.status_code, f"invalid JSON body: {e}", key) from e

    def _store(self, key: str, resp: httpx.Response) -> None:
        etag = resp.headers.get("ETag")
        last_modified = resp.headers.get("Last-Modified")
        if etag or last_modified:
            set_etag(self.conn, key, etag, last_modified)

    def _get(self, path: str, params: dict | None) -> tuple[FetchResult, httpx.Response]:
        resp, key, data = self._conditional(path, params)
        if resp.status_code in (304, 404):
            return FetchResult(changed=False, status=resp.status_code, data=None), resp
        self._store(key, resp)
        return FetchResult(changed=True, status=200, data=data), resp

    def get(self, path: str, params: dict | None = None) -> FetchResult:
        return self._get(path, params)[0]

    def get_unconditional(self, path: str, params: dict | None = None) -> FetchResult:
        """Plain GET that neither sends nor stores an ETag (dry runs, first sightings)."""
        resp = self._http.get(path, params=params)
        if resp.status_code == 404:
            return FetchResult(changed=False, status=404, data=None)
        if resp.status_code != 200:
            raise GitHubError(resp.status_code, _error_message(resp), str(resp.url))
        return FetchResult(changed=True, status=200, data=self._parse(resp, str(resp.url)))

    def get_paginated(self, path: str, params: dict | None = None) -> FetchResult:
        params = {"per_page": 100, **(params or {})}
        resp, key, data = self._conditional(path, params)
        if resp.status_code in (304, 404):
            return FetchResult(changed=False, status=resp.status_code, data=None)
        if not isinstance(data, list):
            raise GitHubError(resp.status_code, "expected a JSON list for paginated endpoint", key)
        first = resp
        items: list = list(data)
        next_link = resp.links.get("next", {}).get("url")
        while next_link:
            page = self._http.get(next_link)
            if page.status_code != 200:
                raise GitHubError(page.status_code, _error_message(page), next_link)
            page_data = self._parse(page, next_link)
            if not isinstance(page_data, list):
                raise GitHubError(200, "expected a JSON list for paginated endpoint", next_link)
            items.extend(page_data)
            next_link = page.links.get("next", {}).get("url")
        # Store the first page's ETag only after every page succeeded.
        self._store(key, first)
        return FetchResult(changed=True, status=200, data=items)

    # -- convenience -----------------------------------------------------
    def resource_key(self, resource: str) -> str:
        """ETag cache key for a named resource: issues, milestones, commits, events, or a
        file path (e.g. ``hardware.yaml``)."""
        repo = f"/repos/{self.repo}"
        if resource in ("issues", "milestones"):
            path, params = f"{repo}/{resource}", {"per_page": 100, "state": "all"}
        elif resource == "commits":
            path, params = f"{repo}/commits", {"per_page": 30}
        elif resource == "events":
            path, params = f"{repo}/events", None
        else:
            path, params = f"{repo}/contents/{resource.lstrip('/')}", None
        return _cache_key(self._http.build_request("GET", path, params=params).url)

    def issues(self) -> FetchResult:
        """All issues (open and closed). Includes PRs; filter by 'pull_request' key."""
        return self.get_paginated(f"/repos/{self.repo}/issues", {"state": "all"})

    def milestones(self) -> FetchResult:
        return self.get_paginated(f"/repos/{self.repo}/milestones", {"state": "all"})

    def commits(self, per_page: int = 30) -> FetchResult:
        """Most recent commits on the default branch, single page."""
        return self.get(f"/repos/{self.repo}/commits", {"per_page": per_page})

    def commit_history(self, since: str, max_pages: int = HISTORY_MAX_PAGES) -> FetchResult:
        """Every commit on the default branch since ``since`` (ISO 8601), all pages.

        Unconditional (the ``since`` value moves, so an ETag would never match) and
        bounded to ``max_pages`` pages of 100. Used once to backfill the history
        and afterwards only when the first page of commits changed.
        """
        path = f"/repos/{self.repo}/commits"
        resp = self._http.get(path, params={"since": since, "per_page": 100})
        items: list = []
        pages = 0
        while True:
            if resp.status_code == 409:  # empty repository
                return FetchResult(changed=True, status=200, data=items)
            if resp.status_code != 200:
                raise GitHubError(resp.status_code, _error_message(resp), str(resp.url))
            data = self._parse(resp, str(resp.url))
            if not isinstance(data, list):
                raise GitHubError(200, "expected a JSON list of commits", str(resp.url))
            items.extend(data)
            pages += 1
            next_link = resp.links.get("next", {}).get("url")
            if not next_link or pages >= max_pages:
                break
            resp = self._http.get(next_link)
        return FetchResult(changed=True, status=200, data=items)

    def file(self, path: str) -> FetchResult:
        """Contents API payload for one file; status 404 if it does not exist."""
        return self.get(f"/repos/{self.repo}/contents/{path.lstrip('/')}")

    def events(self) -> FetchResult:
        """Recent repo events (single page). Updates ``poll_interval``."""
        result, resp = self._get(f"/repos/{self.repo}/events", None)
        interval = resp.headers.get("X-Poll-Interval")
        if interval:
            try:
                self.poll_interval = int(interval)
            except ValueError:
                pass
        return result
