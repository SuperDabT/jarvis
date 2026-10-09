from __future__ import annotations

import json

import httpx
import pytest

from jarvis.ollama import OllamaClient, OllamaError

SCHEMA = {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}


def client_with(handler):
    return OllamaClient("http://ollama.test", model="m1", transport=httpx.MockTransport(handler))


def test_chat_json_payload_and_result():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"message": {"role": "assistant",
                                                     "content": '{"a": "hi"}'}})

    with client_with(handler) as c:
        assert c.chat_json("sys", "usr", SCHEMA) == {"a": "hi"}
    body = seen["body"]
    assert seen["path"] == "/api/chat"
    assert body["stream"] is False and body["format"] == SCHEMA
    assert body["model"] == "m1" and body["options"]["temperature"] == 0.2
    assert body["think"] is False
    assert body["messages"] == [{"role": "system", "content": "sys"},
                                {"role": "user", "content": "usr"}]


def test_think_rejected_is_retried_without_it():
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "think" in body:
            return httpx.Response(400, json={"error": "\"m1\" does not support thinking"})
        return httpx.Response(200, json={"message": {"content": '{"a": "x"}'}})

    with client_with(handler) as c:
        assert c.chat_json("s", "u", SCHEMA) == {"a": "x"}
        assert c.chat_json("s", "u", SCHEMA) == {"a": "x"}
    assert ["think" in b for b in bodies] == [True, False, False]


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(404, json={"error": "model not found"}),
    httpx.Response(200, json={"message": {"content": "not json"}}),
    httpx.Response(200, json={"message": {"content": "[1, 2]"}}),
    httpx.Response(200, text="<html>"),
])
def test_bad_responses_raise(response):
    with client_with(lambda r: response) as c, pytest.raises(OllamaError):
        c.chat_json("s", "u", SCHEMA)


def test_connection_failure_raises():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with client_with(handler) as c, pytest.raises(OllamaError):
        c.chat_json("s", "u", SCHEMA)
