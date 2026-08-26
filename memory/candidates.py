from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .models import DurableMemory, MemoryKind, parse_memory_kind
from .store import MemoryEvent, MemoryStore


@dataclass(frozen=True)
class MemoryCandidate:
    """A reviewable proposal that has not yet become durable memory."""

    kind: MemoryKind
    content: str
    context: dict[str, Any]
    confidence: float
    salience: float
    source_event_id: int
    reason: str


class MemoryCandidatePolicy:
    """Cheap deterministic gate from event history to memory proposals.

    Event types must be explicitly configured. Importance alone is never enough
    to create a candidate, and proposing a candidate never writes durable memory.
    """

    def __init__(
        self,
        event_kinds: Mapping[str, MemoryKind | str],
        *,
        min_importance: float = 0.8,
    ) -> None:
        threshold = float(min_importance)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("min_importance must be between 0.0 and 1.0")

        self.min_importance = threshold
        self.event_kinds = {
            str(event_type): parse_memory_kind(kind)
            for event_type, kind in event_kinds.items()
        }

    def propose(self, event: MemoryEvent) -> MemoryCandidate | None:
        kind = self.event_kinds.get(event.event_type)
        if kind is None:
            return None

        importance = float(event.importance)
        if importance < self.min_importance:
            return None

        salience = max(0.0, min(1.0, importance))
        return MemoryCandidate(
            kind=kind,
            content=event.content,
            context=dict(event.context),
            confidence=1.0,
            salience=salience,
            source_event_id=event.id,
            reason=(
                f"{event.event_type} is configured for {kind.value} memory; "
                f"importance {importance:.2f} >= {self.min_importance:.2f}"
            ),
        )


def promote_candidate(store: MemoryStore, candidate: MemoryCandidate) -> DurableMemory:
    """Explicitly accept one candidate into durable memory."""

    context = dict(candidate.context)
    context["_hikari_memory_formation"] = {
        "reason": candidate.reason,
        "salience": candidate.salience,
    }

    return store.remember_memory(
        candidate.kind,
        candidate.content,
        context=context,
        confidence=candidate.confidence,
        source_event_id=candidate.source_event_id,
    )
