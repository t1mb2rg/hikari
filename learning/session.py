from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from memory import DurableMemory, MemoryCandidate, MemoryKind, MemoryStore


DEFAULT_REFLECTION_KINDS = (
    MemoryKind.EPISODIC,
    MemoryKind.EXPERIENCE,
)


class Reflector(Protocol):
    def reflect(self, memories: list[DurableMemory]) -> MemoryCandidate | None:
        ...


@dataclass(frozen=True)
class LearningSessionState:
    """Caller-owned watermark for bounded reflection sessions."""

    last_reflected_memory_id: int = 0

    def __post_init__(self) -> None:
        if self.last_reflected_memory_id < 0:
            raise ValueError("last_reflected_memory_id must be non-negative")


@dataclass(frozen=True)
class LearningSessionResult:
    state: LearningSessionState
    candidate: MemoryCandidate | None
    reflected: bool
    considered_memory_ids: tuple[int, ...]
    reason: str


class ReflectionTriggerPolicy:
    """Cheap model-free gate deciding whether accumulated memory is worth reflection."""

    def __init__(
        self,
        *,
        min_new_memories: int = 3,
        max_memories: int = 12,
        eligible_kinds: tuple[MemoryKind, ...] = DEFAULT_REFLECTION_KINDS,
    ) -> None:
        if min_new_memories <= 0:
            raise ValueError("min_new_memories must be positive")
        if max_memories <= 0:
            raise ValueError("max_memories must be positive")
        if max_memories < min_new_memories:
            raise ValueError("max_memories must be >= min_new_memories")
        if not eligible_kinds:
            raise ValueError("eligible_kinds must not be empty")

        self.min_new_memories = int(min_new_memories)
        self.max_memories = int(max_memories)
        self.eligible_kinds = tuple(eligible_kinds)

    def should_reflect(self, memories: list[DurableMemory]) -> bool:
        return len(memories) >= self.min_new_memories


class LearningSession:
    """One bounded opportunity for Hikari to think back over new experience.

    The session only decides whether to invoke reflection and returns a candidate.
    It never reviews or persists a learned conclusion automatically.
    """

    def __init__(
        self,
        *,
        store: MemoryStore,
        reflector: Reflector,
        trigger: ReflectionTriggerPolicy | None = None,
    ) -> None:
        self.store = store
        self.reflector = reflector
        self.trigger = trigger or ReflectionTriggerPolicy()

    def run(self, state: LearningSessionState | None = None) -> LearningSessionResult:
        current_state = state or LearningSessionState()
        memories = self.store.memories_after(
            current_state.last_reflected_memory_id,
            kinds=self.trigger.eligible_kinds,
            limit=self.trigger.max_memories,
        )
        ids = tuple(memory.id for memory in memories)

        if not self.trigger.should_reflect(memories):
            return LearningSessionResult(
                state=current_state,
                candidate=None,
                reflected=False,
                considered_memory_ids=ids,
                reason=(
                    f"{len(memories)} new eligible memories; "
                    f"need {self.trigger.min_new_memories}"
                ),
            )

        candidate = self.reflector.reflect(memories)
        next_state = LearningSessionState(last_reflected_memory_id=memories[-1].id)
        return LearningSessionResult(
            state=next_state,
            candidate=candidate,
            reflected=True,
            considered_memory_ids=ids,
            reason=(
                f"reflected over {len(memories)} new eligible memories; "
                f"watermark advanced to {next_state.last_reflected_memory_id}"
            ),
        )
