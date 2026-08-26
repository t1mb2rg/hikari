from __future__ import annotations

import os

from attention import AttentionDecision
from brain import ModelReasoner
from brain.providers import OpenAICompatibleProvider
from events import Event
from personality import HIKARI_PERSONALITY_KEY, load_personality, personality_as_context


def main() -> None:
    base_url = os.environ.get("HIKARI_MODEL_BASE_URL")
    model = os.environ.get("HIKARI_MODEL_NAME")
    api_key = os.environ.get("HIKARI_MODEL_API_KEY")

    if not base_url or not model:
        raise SystemExit(
            "Set HIKARI_MODEL_BASE_URL and HIKARI_MODEL_NAME before running the gate."
        )

    personality = load_personality()
    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
    )
    reasoner = ModelReasoner(provider)

    event = Event(
        event_type="hikari.physical_gate",
        source="manual-gate",
        content="这是光第一次跨过真实模型认知边界。请注意到这件事，并自然地告诉我你看到了什么。",
        context={
            HIKARI_PERSONALITY_KEY: personality_as_context(personality),
        },
    )
    decision = AttentionDecision(
        should_intervene=True,
        importance=0.95,
        reason="explicit physical gate",
    )

    feedback = reasoner.reason(event, decision)
    print(feedback.text)


if __name__ == "__main__":
    main()
