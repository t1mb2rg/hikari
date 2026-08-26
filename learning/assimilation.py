from __future__ import annotations

from collections.abc import Iterable

from memory import DurableMemory, MemoryKind, MemoryStore


LEARNED_CONTEXT_KEY = "_hikari_learned"
DEFAULT_ASSIMILATION_KINDS = (
    MemoryKind.USER_MODEL,
    MemoryKind.SEMANTIC,
)


class LearningAssimilationPolicy:
    """Bounded recall of accepted learned memory into future cognition.

    Only durable memories are eligible here. Reflection candidates and review
    decisions never enter this path until they have been explicitly accepted and
    written to MemoryStore.
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.75,
        limit: int = 4,
        eligible_kinds: tuple[MemoryKind, ...] = DEFAULT_ASSIMILATION_KINDS,
    ) -> None:
        confidence = float(min_confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")
        if limit <= 0:
            raise ValueError("limit must be positive")
        if not eligible_kinds:
            raise ValueError("eligible_kinds must not be empty")

        self.min_confidence = confidence
        self.limit = int(limit)
        self.eligible_kinds = tuple(eligible_kinds)

    def recall(self, store: MemoryStore) -> list[DurableMemory]:
        learned: list[DurableMemory] = []
        for kind in self.eligible_kinds:
            learned.extend(store.recent_memories(limit=self.limit, kind=kind))

        learned = [
            memory
            for memory in learned
            if memory.confidence >= self.min_confidence
        ]
        learned.sort(key=lambda memory: memory.id, reverse=True)
        return learned[: self.limit]


def learned_memories_as_context(
    memories: Iterable[DurableMemory],
) -> list[dict[str, object]]:
    """Serialize accepted learned memory for Reasoner-only context."""

    return [
        {
            "id": memory.id,
            "kind": memory.kind.value,
            "content": memory.content,
            "confidence": memory.confidence,
            "created_at": memory.created_at,
        }
        for memory in memories
    ]
