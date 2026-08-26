from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from attention.policy import AttentionDecision
from events.models import Event


HIKARI_EMOTION_KEY = "_hikari_emotion"
EMOTION_DIMENSIONS = (
    "curiosity",
    "concern",
    "satisfaction",
    "frustration",
)


@dataclass(frozen=True)
class EmotionState:
    """Fast-changing, model-independent affective state.

    Emotion is intentionally separate from PersonalityProfile. Personality is a
    slow-changing baseline; EmotionState is transient context that may tint
    expression but must not replace factual or safety judgment.
    """

    levels: dict[str, float]
    version: str = "0.1.0"

    def __post_init__(self) -> None:
        expected = set(EMOTION_DIMENSIONS)
        actual = set(self.levels)
        missing = expected - actual
        unknown = actual - expected
        if missing:
            raise ValueError(f"missing emotion dimensions: {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"unknown emotion dimensions: {', '.join(sorted(unknown))}")

        normalized: dict[str, float] = {}
        for name in EMOTION_DIMENSIONS:
            value = float(self.levels[name])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"emotion dimension {name!r} must be between 0.0 and 1.0")
            normalized[name] = value

        object.__setattr__(self, "levels", normalized)

    def describe(self) -> dict[str, object]:
        return {
            "version": self.version,
            "levels": dict(self.levels),
        }


DEFAULT_EMOTION_STATE = EmotionState(
    levels={
        "curiosity": 0.45,
        "concern": 0.15,
        "satisfaction": 0.25,
        "frustration": 0.05,
    }
)


class EmotionPolicy:
    """Pure deterministic transition policy for transient emotion.

    Each event first settles the current state toward a baseline, then applies
    configured deltas scaled by Attention importance. No model call is used and
    no event type carries implicit emotional meaning unless explicitly mapped.
    """

    def __init__(
        self,
        transitions: Mapping[str, Mapping[str, float]] | None = None,
        *,
        baseline: EmotionState = DEFAULT_EMOTION_STATE,
        settle_rate: float = 0.10,
    ) -> None:
        if not 0.0 <= float(settle_rate) <= 1.0:
            raise ValueError("settle_rate must be between 0.0 and 1.0")

        normalized: dict[str, dict[str, float]] = {}
        for event_type, deltas in (transitions or {}).items():
            unknown = set(deltas) - set(EMOTION_DIMENSIONS)
            if unknown:
                raise ValueError(
                    f"unknown emotion dimensions in transition: {', '.join(sorted(unknown))}"
                )
            normalized[event_type] = {
                name: float(value)
                for name, value in deltas.items()
            }

        self.transitions = normalized
        self.baseline = baseline
        self.settle_rate = float(settle_rate)

    def transition(
        self,
        state: EmotionState,
        event: Event,
        decision: AttentionDecision,
    ) -> EmotionState:
        deltas = self.transitions.get(event.event_type, {})
        importance = max(0.0, min(1.0, float(decision.importance)))
        next_levels: dict[str, float] = {}

        for name in EMOTION_DIMENSIONS:
            current = state.levels[name]
            target = self.baseline.levels[name]
            settled = current + (target - current) * self.settle_rate
            shifted = settled + deltas.get(name, 0.0) * importance
            next_levels[name] = max(0.0, min(1.0, shifted))

        return EmotionState(levels=next_levels, version=state.version)


def emotion_as_context(state: EmotionState) -> dict[str, object]:
    """Serialize transient emotion for the Reasoner-only context path."""

    return state.describe()
