"""Minimal Ollama chat client returning schema-constrained JSON."""
from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_URL = "http://localhost:11434"
DEFAULT_MODEL = "gemma4:26b"


class OllamaError(RuntimeError):
    """Connection failure, non-200 response, or a reply that is not valid JSON."""


class OllamaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        model: str = DEFAULT_MODEL,
        timeout: float = 180,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._think_supported = True

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
