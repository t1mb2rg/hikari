from __future__ import annotations

import json

import pytest

from attention import AttentionPolicy
from brain import ChatMessage, ModelReasoner, SimpleReasoner
from core.presence import PresencePipeline
from core.runtime import ResidentPresenceRuntime
from events import Event
from examples.watch_git import build_reasoner
from memory.store import MemoryStore
from personality import load_personality


class _RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages):
        captured = list(messages)
        self.calls.append(captured)
        return "我看到了这个变化，值得你留意一下。"


class _CollectingSink:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def deliver(self, feedback) -> None:
        self.texts.append(feedback.text)


class _BatchSensor:
    name = "test"

    def __init__(self, batches: list[list[Event]]) -> None:
        self.batches = list(batches)

    def poll(self) -> list[Event]:
        if not self.batches:
            return []
        return self.batches.pop(0)


def test_build_reasoner_keeps_simple_default_and_builds_model_from_runtime_env():
    assert isinstance(build_reasoner("simple", environment={}), SimpleReasoner)

    reasoner = build_reasoner(
        "model",
        environment={
            "HIKARI_MODEL_BASE_URL": "https://example.invalid",
            "HIKARI_MODEL_NAME": "test-model",
            "HIKARI_MODEL_API_KEY": "runtime-only-secret",
        },
    )

    assert isinstance(reasoner, ModelReasoner)
    assert reasoner.provider.base_url == "https://example.invalid"
    assert reasoner.provider.model == "test-model"


@pytest.mark.parametrize(
    "environment, missing",
    [
        ({}, "HIKARI_MODEL_BASE_URL"),
        ({"HIKARI_MODEL_BASE_URL": "https://example.invalid"}, "HIKARI_MODEL_NAME"),
    ],
)
def test_model_reasoner_fails_before_runtime_when_required_env_is_missing(environment, missing):
    with pytest.raises(ValueError, match=missing):
        build_reasoner("model", environment=environment)


def test_resident_model_cognition_is_quiet_until_attention_intervenes(tmp_path):
    provider = _RecordingProvider()
    sink = _CollectingSink()
    pipeline = PresencePipeline(
        memory=MemoryStore(tmp_path / "memory.db"),
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={
                "test.quiet": 0.2,
                "test.important": 0.9,
            },
        ),
        reasoner=ModelReasoner(provider),
        feedback_sink=sink,
        personality_profile=load_personality(),
    )
    runtime = ResidentPresenceRuntime(
        [
            _BatchSensor(
                [
                    [],
                    [Event(event_type="test.quiet", source="test", content="后台噪声")],
                    [Event(event_type="test.important", source="test", content="一个重要变化")],
                ]
            )
        ],
        pipeline,
    )

    assert runtime.cycle_once().events == ()
    assert provider.calls == []

    quiet = runtime.cycle_once()
    assert len(quiet.events) == 1
    assert quiet.interventions[0].feedback is None
    assert provider.calls == []

    important = runtime.cycle_once()
    assert len(important.events) == 1
    assert len(provider.calls) == 1
    assert sink.texts == ["我看到了这个变化，值得你留意一下。"]

    messages = provider.calls[0]
    assert "Simplified Chinese" in messages[0].content
    payload = json.loads(messages[1].content)
    assert payload["event"]["content"] == "一个重要变化"
    assert payload["context"]["_hikari_personality"]["traits"]["warmth"] > 0
