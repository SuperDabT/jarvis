"""Minimal Ollama chat client returning schema-constrained JSON."""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b"
DEFAULT_EMBED_MODEL = "qwen3-embedding:0.6b"


class OllamaError(RuntimeError):
    """Connection failure, non-200 response, or a reply that is not valid JSON."""


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 180,
        transport: httpx.BaseTransport | None = None,
        keep_alive: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        # Sent as "keep_alive" with chat_stream requests (e.g. "10m"); None = server default.
        self.keep_alive = keep_alive
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._think_supported = True
        # The final NDJSON object of the last chat_stream (timings, eval_count), or None.
        self.last_stream_stats: dict[str, Any] | None = None

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _post(self, payload: dict[str, Any]) -> httpx.Response:
        try:
            return self._http.post("/api/chat", json=payload)
        except httpx.HTTPError as e:
            raise OllamaError(f"cannot reach Ollama at {self.base_url}: {e}") from e

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": False,
            "format": schema,
            "options": {"temperature": 0.2},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if self._think_supported:
            payload["think"] = False
        resp = self._post(payload)
        # Older servers or non-thinking models may reject the "think" field; retry without it.
        if resp.status_code == 400 and "think" in resp.text.lower() and "think" in payload:
            self._think_supported = False
            payload.pop("think")
            resp = self._post(payload)
        if resp.status_code != 200:
            raise OllamaError(f"Ollama returned {resp.status_code}: {resp.text[:200]}")
        try:
            content = resp.json()["message"]["content"]
            out = json.loads(content)
        except (ValueError, KeyError, TypeError) as e:
            raise OllamaError(f"unparseable reply from Ollama: {e}") from e
        if not isinstance(out, dict):
            raise OllamaError("Ollama reply is not a JSON object")
        return out

    def embed(self, texts: list[str], model: str = DEFAULT_EMBED_MODEL) -> list[list[float]]:
        """One embedding vector per text via /api/embed (raises OllamaError)."""
        if not texts:
            return []
        try:
            resp = self._http.post("/api/embed", json={"model": model, "input": texts})
        except httpx.HTTPError as e:
            raise OllamaError(f"cannot reach Ollama at {self.base_url}: {e}") from e
        if resp.status_code != 200:
            raise OllamaError(f"Ollama embed returned {resp.status_code}: {resp.text[:200]}")
        try:
            vecs = resp.json()["embeddings"]
        except (ValueError, KeyError, TypeError) as e:
            raise OllamaError(f"unparseable embed reply from Ollama: {e}") from e
        if not isinstance(vecs, list) or len(vecs) != len(texts):
            raise OllamaError("Ollama embed reply has the wrong number of vectors")
        return [[float(x) for x in v] for v in vecs]

    def chat_stream(
        self, messages: list[dict[str, str]], options: dict[str, Any] | None = None,
        keep_alive: str | None = None,
    ) -> Iterator[str]:
        """Stream a plain-text chat reply as content chunks (POST /api/chat, stream=true).

        Ollama answers with NDJSON: one object per line carrying ``message.content``,
        the last one with ``done: true`` (its stats are kept in ``last_stream_stats``).
        A line with ``error`` (e.g. the model crashed mid-reply), a non-200 status, a
        connection failure, or a stream that ends without ``done`` raises OllamaError.
        Thinking is disabled like ``chat_json`` (retried without it if rejected).
        ``keep_alive`` (or the client's ``keep_alive``) is passed on when set.
        """
        self.last_stream_stats = None
        payload: dict[str, Any] = {
            "model": self.model,
            "stream": True,
            "options": {"temperature": 0.2, **(options or {})},
            "messages": messages,
        }
        if (ka := keep_alive or self.keep_alive) is not None:
            payload["keep_alive"] = ka
        for _attempt in range(2):
            if self._think_supported:
                payload["think"] = False
            else:
                payload.pop("think", None)
            try:
                with self._http.stream("POST", "/api/chat", json=payload) as resp:
                    if resp.status_code != 200:
                        text = resp.read().decode("utf-8", "replace")
                        if (resp.status_code == 400 and "think" in payload
                                and "think" in text.lower()):
                            self._think_supported = False
                            continue
                        raise OllamaError(f"Ollama returned {resp.status_code}: {text[:200]}")
                    yield from self._iter_ndjson(resp.iter_lines())
                    return
            except httpx.HTTPError as e:
                raise OllamaError(f"cannot reach Ollama at {self.base_url}: {e}") from e
        raise OllamaError("Ollama rejected the request twice")

    def _iter_ndjson(self, lines: Iterator[str]) -> Iterator[str]:
        for line in lines:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as e:
                raise OllamaError(f"unparseable stream line from Ollama: {line[:120]}") from e
            if not isinstance(obj, dict):
                raise OllamaError("Ollama stream line is not a JSON object")
            if obj.get("error"):
                raise OllamaError(f"Ollama error: {obj['error']}")
            msg = obj.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
            if content:
                yield content
            if obj.get("done"):
                self.last_stream_stats = {k: v for k, v in obj.items() if k != "message"}
                return
        raise OllamaError("Ollama stream ended before the reply was done")
