from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from collections.abc import Iterable

from .candidates import MemoryCandidate, promote_candidate
from .models import DurableMemory, MemoryKind
from .store import MemoryStore


class MemoryReviewDecision(StrEnum):
    """Outcome of reviewing a memory candidate."""

    ACCEPT = "accept"
    REJECT = "reject"
    DEFER = "defer"


@dataclass(frozen=True)
class MemoryReview:
    candidate: MemoryCandidate
    decision: MemoryReviewDecision
    reason: str


class MemoryReviewPolicy:
    """Conservative deterministic reviewer for M2.

    The policy only decides whether a candidate is ready for durable memory.
    It never writes memory itself and can later be replaced by a richer reviewer.
    """

    def __init__(
        self,
        *,
        accepted_kinds: Iterable[MemoryKind] | None = None,
        min_salience: float = 0.9,
        min_confidence: float = 0.8,
    ) -> None:
        self.accepted_kinds = frozenset(accepted_kinds or tuple(MemoryKind))
        self.min_salience = self._validate_threshold("min_salience", min_salience)
        self.min_confidence = self._validate_threshold("min_confidence", min_confidence)

    @staticmethod
    def _validate_threshold(name: str, value: float) -> float:
        parsed = float(value)
        if not 0.0 <= parsed <= 1.0:
            raise ValueError(f"{name} must be between 0.0 and 1.0")
        return parsed

    def review(self, candidate: MemoryCandidate) -> MemoryReview:
        if candidate.kind not in self.accepted_kinds:
            return MemoryReview(
                candidate=candidate,
                decision=MemoryReviewDecision.REJECT,
                reason=f"{candidate.kind.value} memory is not enabled for this reviewer",
            )

        if candidate.salience < self.min_salience or candidate.confidence < self.min_confidence:
            return MemoryReview(
                candidate=candidate,
                decision=MemoryReviewDecision.DEFER,
                reason=(
                    "candidate is not strong enough yet; "
                    f"salience {candidate.salience:.2f}/{self.min_salience:.2f}, "
                    f"confidence {candidate.confidence:.2f}/{self.min_confidence:.2f}"
                ),
            )

        return MemoryReview(
            candidate=candidate,
            decision=MemoryReviewDecision.ACCEPT,
            reason=(
                "candidate meets durable-memory review thresholds; "
                f"salience {candidate.salience:.2f}, confidence {candidate.confidence:.2f}"
            ),
        )


def apply_memory_review(store: MemoryStore, review: MemoryReview) -> DurableMemory | None:
    """Persist an accepted review; reject/defer are intentionally no-ops."""

    if review.decision is not MemoryReviewDecision.ACCEPT:
        return None

    context = dict(review.candidate.context)
    context["_hikari_memory_review"] = {
        "decision": review.decision.value,
        "reason": review.reason,
    }
    candidate = replace(review.candidate, context=context)
    return promote_candidate(store, candidate)
