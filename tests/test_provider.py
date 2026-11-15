from __future__ import annotations

import json

import httpx
import pytest

from dayloom.config import LLMConfig
from dayloom.llm.provider import (
    LLMFormatError,
    LLMHTTPError,
    Part,
    VisionClient,
    extract_json,
)


def make_client(handler, **kwargs) -> VisionClient:
    cfg = LLMConfig(base_url="http://test.local/v1", model="test-model", api_key="k")
    return VisionClient(cfg, transport=httpx.MockTransport(handler), **kwargs)


def test_extract_json_variants():
    assert extract_json('{"a": 1}') == {"a": 1}
    assert extract_json('```json\n{"a": 2}\n```') == {"a": 2}
    assert extract_json('好的，结果如下：{"a": 3} 以上') == {"a": 3}


def test_extract_json_rejects_prose():
    with pytest.raises(LLMFormatError):
        extract_json("这里没有任何 JSON")


def test_chat_roundtrip():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "你好"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2},
            },
        )

    with make_client(handler) as client:
        completion = client.chat([Part.of_text("hi")], system="sys")
    assert completion.text == "你好"
    assert completion.usage.prompt_tokens == 10
    assert captured["model"] == "test-model"
    assert captured["messages"][0] == {"role": "system", "content": "sys"}


def test_image_parts_become_data_urls():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    with make_client(handler) as client:
        client.chat([Part.of_text("看图"), Part.of_image(b"\xff\xd8fake", "image/jpeg")])
    content = captured["messages"][-1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_response_format_is_dropped_when_rejected():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body)
        if "response_format" in body:
            return httpx.Response(400, text='{"error": "response_format is not supported"}')
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"ok": true}'}}]}
        )

    with make_client(handler) as client:
        payload, _usage = client.complete_json([Part.of_text("x")])
        assert payload == {"ok": True}
        assert "response_format" not in seen[-1]
        assert client.profile.supports_json_mode is False


def test_http_errors_are_not_retried_forever():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, text="unauthorized")

    with make_client(handler) as client, pytest.raises(LLMHTTPError) as excinfo:
        client.chat([Part.of_text("x")])
    assert excinfo.value.status == 401
    assert calls["n"] == 1


def test_json_repair_retry():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        content = "这不是 JSON" if calls["n"] == 1 else '{"fixed": true}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    with make_client(handler) as client:
        payload, _usage = client.complete_json([Part.of_text("x")])
    assert payload == {"fixed": True}
    assert calls["n"] == 2
