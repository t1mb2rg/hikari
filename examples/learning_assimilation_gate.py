from __future__ import annotations

import os
from pathlib import Path
import tempfile

from attention import AttentionPolicy
from brain import ModelReasoner
from brain.providers import OpenAICompatibleProvider
from core.presence import PresencePipeline
from events import Event
from learning import LearningAssimilationPolicy
from memory import (
    MemoryCandidate,
    MemoryKind,
    MemoryReviewPolicy,
    apply_memory_review,
)
from memory.store import MemoryStore
from personality import load_personality


class SilentSink:
    def deliver(self, feedback) -> None:
        pass


def _build_pipeline(store: MemoryStore, reasoner: ModelReasoner) -> PresencePipeline:
    return PresencePipeline(
        memory=store,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"hikari.learning_gate": 0.95},
        ),
        reasoner=reasoner,
        feedback_sink=SilentSink(),
        assimilation_policy=LearningAssimilationPolicy(),
        personality_profile=load_personality(),
    )


def _accept_gate_learning(store: MemoryStore) -> None:
    candidate = MemoryCandidate(
        kind=MemoryKind.USER_MODEL,
        content=(
            "The user prefers short milestone loops: when a gate is fully green, "
            "close it and move directly to the next bounded task instead of lingering "
            "on redundant validation."
        ),
        context={"gate": "m4-03"},
        confidence=0.96,
        salience=0.96,
        source_event_id=None,
        reason="M4-03 physical gate accepted learning",
    )
    review = MemoryReviewPolicy().review(candidate)
    learned = apply_memory_review(store, review)
    if learned is None:
        raise RuntimeError("gate learning was not accepted by MemoryReviewPolicy")


def main() -> None:
    base_url = os.environ.get("HIKARI_MODEL_BASE_URL")
    model = os.environ.get("HIKARI_MODEL_NAME")
    api_key = os.environ.get("HIKARI_MODEL_API_KEY")

    if not base_url or not model:
        raise SystemExit(
            "Set HIKARI_MODEL_BASE_URL and HIKARI_MODEL_NAME before running the gate."
        )

    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        temperature=0.0,
    )
    reasoner = ModelReasoner(provider)
    event = Event(
        event_type="hikari.learning_gate",
        source="manual-gate",
        content=(
            "A Hikari development milestone has just passed its full test suite. "
            "Respond to the user in concise natural Chinese with your recommendation "
            "for what should happen next."
        ),
    )

    with tempfile.TemporaryDirectory(prefix="hikari-m4-03-") as tmp:
        root = Path(tmp)
        before_store = MemoryStore(root / "before.db")
        after_store = MemoryStore(root / "after.db")
        _accept_gate_learning(after_store)

        before = _build_pipeline(before_store, reasoner).handle(event)
        after = _build_pipeline(after_store, reasoner).handle(event)

    print("=== BEFORE LEARNING ===")
    print(before.feedback.text if before.feedback else "<no feedback>")
    print()
    print("=== AFTER ACCEPTED LEARNING ===")
    print(after.feedback.text if after.feedback else "<no feedback>")


if __name__ == "__main__":
    main()
