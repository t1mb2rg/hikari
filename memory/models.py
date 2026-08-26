from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MemoryKind(StrEnum):
    """Stable semantic categories for durable Hikari memory."""

    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    USER_MODEL = "user_model"
    EXPERIENCE = "experience"


@dataclass(frozen=True)
class DurableMemory:
    id: int
    kind: MemoryKind
    content: str
    context: dict[str, Any]
    confidence: float
    source_event_id: int | None
    created_at: str


def parse_memory_kind(value: MemoryKind | str) -> MemoryKind:
    if isinstance(value, MemoryKind):
        return value

    try:
        return MemoryKind(str(value))
    except ValueError as exc:
        allowed = ", ".join(kind.value for kind in MemoryKind)
        raise ValueError(f"Unknown memory kind {value!r}; expected one of: {allowed}") from exc
