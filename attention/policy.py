from __future__ import annotations

from dataclasses import dataclass
from numbers import Real
from typing import Mapping

from events.models import Event


@dataclass(frozen=True)
class AttentionDecision:
    """Cheap judgment about whether an event deserves deeper cognition."""

    should_intervene: bool
    importance: float
    reason: str


class AttentionPolicy:
    """Deterministic M0 attention gate.

    The policy is intentionally cheap. It never calls a model. Event adapters may
    provide an ``importance_hint`` in context, while deployment configuration may
    define default weights for known event types.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.7,
        event_importance: Mapping[str, float] | None = None,
        default_importance: float = 0.0,
    ) -> None:
        self.threshold = self._clamp(threshold)
        self.event_importance = {
            event_type: self._clamp(value)
            for event_type, value in (event_importance or {}).items()
        }
        self.default_importance = self._clamp(default_importance)

    def evaluate(self, event: Event) -> AttentionDecision:
        configured = self.event_importance.get(
            event.event_type,
            self.default_importance,
        )
        hint = event.context.get("importance_hint")

        if isinstance(hint, Real) and not isinstance(hint, bool):
            importance = self._clamp(float(hint))
            reason = "sensor importance hint"
        else:
            importance = configured
            reason = "event type policy"

        return AttentionDecision(
            should_intervene=importance >= self.threshold,
            importance=importance,
            reason=reason,
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, float(value)))
