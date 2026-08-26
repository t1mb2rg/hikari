from __future__ import annotations

from collections.abc import Iterable, Mapping

from .models import DurableMemory, MemoryKind, parse_memory_kind
from .store import MemoryStore


class MemoryRecallPolicy:
    """Cheap bounded recall from durable memory into cognition.

    Event types opt in explicitly to one or more memory kinds. The policy does
    not perform semantic search and never mutates memory.
    """

    def __init__(
        self,
        event_kinds: Mapping[str, Iterable[MemoryKind | str]],
        *,
        limit: int = 3,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be positive")

        self.limit = int(limit)
        self.event_kinds = {
            str(event_type): tuple(parse_memory_kind(kind) for kind in kinds)
            for event_type, kinds in event_kinds.items()
        }

    def recall(self, store: MemoryStore, event_type: str) -> list[DurableMemory]:
        kinds = self.event_kinds.get(event_type)
        if not kinds:
            return []

        recalled: list[DurableMemory] = []
        for kind in kinds:
            recalled.extend(store.recent_memories(limit=self.limit, kind=kind))

        recalled.sort(key=lambda memory: memory.id, reverse=True)
        return recalled[: self.limit]


def memories_as_context(memories: Iterable[DurableMemory]) -> list[dict[str, object]]:
    """Return a compact, serializable Reasoner-only representation."""

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
