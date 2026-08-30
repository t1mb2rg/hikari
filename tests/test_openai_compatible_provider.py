from __future__ import annotations

import json

from brain import ChatMessage
from brain.providers import OpenAICompatibleProvider


def test_openai_compatible_provider_builds_request_and_parses_response():
    captured = {}

    def transport(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        captured["timeout"] = timeout
        return json.dumps(
            {
                "choices": [
                    {"message": {"content": "真实模型反馈"}},
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")

    provider = OpenAICompatibleProvider(
        base_url="http://127.0.0.1:8000/v1",
        model="Qwen-Test",
        api_key="secret",
        temperature=0.2,
        timeout=12.0,
        transport=transport,
    )

    result = provider.complete(
        [
            ChatMessage(role="system", content="system"),
            ChatMessage(role="user", content="你好"),
        ]
    )

    assert result == "真实模型反馈"
    assert captured["url"] == "http://127.0.0.1:8000/v1/chat/completions"
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["body"] == {
        "model": "Qwen-Test",
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "你好"},
        ],
        "temperature": 0.2,
    }
    assert captured["timeout"] == 12.0


def test_openai_compatible_provider_supports_local_endpoint_without_key():
    captured = {}

    def transport(req, timeout):
        captured["headers"] = dict(req.header_items())
        return b'{"choices":[{"message":{"content":"ok"}}]}'

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:8000",
        model="local-model",
        transport=transport,
    )

    assert provider.complete([ChatMessage(role="user", content="ping")]) == "ok"
    assert "Authorization" not in captured["headers"]
    assert provider.endpoint == "http://localhost:8000/v1/chat/completions"


def test_openai_compatible_provider_json_mode_is_narrow_and_deterministic():
    captured = {}

    def transport(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return b'{"choices":[{"message":{"content":"{\\"facts\\":[]}"}}]}'

    provider = OpenAICompatibleProvider(
        base_url="http://localhost:8000",
        model="local-model",
        temperature=0.65,
        transport=transport,
    )

    assert provider.complete_json([ChatMessage(role="user", content="extract")]) == (
        '{"facts":[]}'
    )
    assert captured["body"]["temperature"] == 0.0
    assert captured["body"]["response_format"] == {"type": "json_object"}


def test_openai_compatible_provider_rejects_malformed_response():
    provider = OpenAICompatibleProvider(
        base_url="http://localhost:8000",
        model="local-model",
        transport=lambda req, timeout: b'{"unexpected":true}',
    )

    try:
        provider.complete([ChatMessage(role="user", content="ping")])
    except RuntimeError as exc:
        assert "invalid OpenAI-compatible" in str(exc)
    else:
        raise AssertionError("malformed response must fail")
