from __future__ import annotations

import httpx

from dayloom.config import LLMConfig
from dayloom.llm.mockvlm import MockVLM
from dayloom.llm.probe import Challenge, make_challenge
from dayloom.llm.provider import VisionClient


def test_probe_passes_against_the_mock_vlm(cfg):
    with MockVLM() as server:
        cfg.llm.base_url = server.base_url
        cfg.llm.model = "mock-vlm"
        client = VisionClient(cfg.llm)
        try:
            result = client.probe()
        finally:
            client.close()
    assert result.ok, result.reason
    assert result.detected == result.expected
    assert result.checks["colors"] is True


def test_probe_rejects_a_blind_endpoint():
    """A text-only endpoint answers fluently without looking at the image."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": '{"colors": ["red","red","red","red","red"]}'}}],
                "usage": {"prompt_tokens": 40, "completion_tokens": 8},
            },
        )

    cfg = LLMConfig(base_url="http://blind.local/v1", model="text-only")
    with VisionClient(cfg, transport=httpx.MockTransport(handler)) as client:
        result = client.probe()
    assert not result.ok
    assert result.checks["colors"] is False
    assert "图片" in result.reason


def test_probe_accepts_a_correct_prose_answer(monkeypatch):
    challenge = make_challenge()
    fixed = Challenge(png=challenge.png, colors=["red", "green", "blue", "yellow", "red"])
    from dayloom.llm import provider as provider_mod

    monkeypatch.setattr(provider_mod, "make_challenge", lambda: fixed)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "颜色依次是红色、绿色、蓝色、黄色、红色"}}],
                "usage": {"prompt_tokens": 820, "completion_tokens": 20},
            },
        )

    cfg = LLMConfig(base_url="http://ok.local/v1", model="vlm")
    with VisionClient(cfg, transport=httpx.MockTransport(handler)) as client:
        result = client.probe()
    assert result.ok, result.reason


def test_challenge_is_not_guessable_all_one_colour():
    for _ in range(30):
        challenge = make_challenge()
        assert len(challenge.colors) == 5
        assert len(set(challenge.colors)) > 1
