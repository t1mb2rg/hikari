from __future__ import annotations

import json

import pytest

from attention import AttentionDecision
from brain import ChatMessage, ModelCognitionError, ModelReasoner
from events import Event


class RecordingProvider:
    def __init__(self, response: str = "我注意到了，这件事值得你看一眼。") -> None:
        self.response = response
        self.messages: list[ChatMessage] = []

    def complete(self, messages):
        self.messages = list(messages)
        return self.response


class FailingProvider:
    def complete(self, messages):
        raise OSError("temporary network failure with provider detail")


def _decision() -> AttentionDecision:
    return AttentionDecision(
        should_intervene=True,
        importance=0.9,
        reason="event type policy",
    )


def test_model_reasoner_passes_structured_context_to_provider():
    provider = RecordingProvider()
    reasoner = ModelReasoner(provider)
    event = Event(
        event_type="test.important",
        source="fake",
        content="A meaningful change happened",
        context={
            "_hikari_context": {"providers": {"time": {"hour": 22}}},
            "_hikari_personality": {
                "version": "0.1.0",
                "traits": {"warmth": 0.85, "directness": 0.8},
            },
            "_hikari_recall": [
                {"kind": "episodic", "content": "A related memory"},
            ],
        },
    )

    feedback = reasoner.reason(event, _decision())

    assert feedback.text == provider.response
    assert feedback.event_type == "test.important"
    assert feedback.importance == 0.9
    assert [message.role for message in provider.messages] == ["system", "user"]

    payload = json.loads(provider.messages[1].content)
    assert payload["event"]["content"] == "A meaningful change happened"
    assert payload["attention"]["importance"] == 0.9
    assert payload["context"]["_hikari_context"]["providers"]["time"]["hour"] == 22
    assert payload["context"]["_hikari_personality"]["traits"]["warmth"] == 0.85
    assert payload["context"]["_hikari_recall"][0]["content"] == "A related memory"


def test_model_reasoner_classifies_provider_failure_without_copying_provider_message():
    reasoner = ModelReasoner(FailingProvider())

    with pytest.raises(ModelCognitionError) as captured:
        reasoner.reason(
            Event(event_type="test", source="fake", content="Something"),
            _decision(),
        )

    assert captured.value.provider_error_type == "OSError"
    assert "provider detail" not in str(captured.value)
    assert isinstance(captured.value.__cause__, OSError)


def test_model_reasoner_rejects_empty_provider_output_as_cognition_failure():
    provider = RecordingProvider("   ")
    reasoner = ModelReasoner(provider)

    with pytest.raises(ModelCognitionError) as captured:
        reasoner.reason(
            Event(event_type="test", source="fake", content="Something"),
            _decision(),
        )

    assert "empty feedback" in str(captured.value)
    assert captured.value.provider_error_type == "EmptyResponse"
