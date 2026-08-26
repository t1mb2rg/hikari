from __future__ import annotations

from dataclasses import dataclass

from .context import ContextSnapshot


@dataclass(frozen=True)
class UserState:
    """Conservative interpretation derived from already-captured context."""

    engagement: str
    interruptibility: str
    confidence: float
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "engagement": self.engagement,
            "interruptibility": self.interruptibility,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


class UserStateInferer:
    """Cheap, deterministic M1 user-state inference.

    This layer combines evidence without claiming presence, intent, focus,
    emotion, or absence from a single signal. Unknown is an intentional result.
    """

    def infer(self, snapshot: ContextSnapshot) -> UserState:
        providers = snapshot.providers
        activity = providers.get("input_activity", {})
        foreground = providers.get("foreground", {})
        schedule = providers.get("schedule", {})
        time_context = providers.get("time", {})

        recent_input = activity.get("recent_input")
        foreground_available = foreground.get("available") is True
        current_schedule = schedule.get("current", [])

        evidence: list[str] = []
        if recent_input is True:
            evidence.append("recent local input observed")
        elif recent_input is False:
            evidence.append("no recent local input observed")
        else:
            evidence.append("recent input signal unavailable")

        if foreground_available:
            title = str(foreground.get("title", "")).strip()
            evidence.append(
                f"foreground window available: {title}" if title else "foreground window available"
            )
        elif "foreground" in providers:
            evidence.append("foreground window unavailable")

        if current_schedule:
            evidence.append("schedule has current item(s)")

        if "hour" in time_context:
            evidence.append(f"local hour: {time_context['hour']}")

        if recent_input is True and foreground_available:
            engagement = "interactive"
            confidence = 0.8
        elif recent_input is False:
            engagement = "passive_or_unknown"
            confidence = 0.4 if foreground_available else 0.3
        else:
            engagement = "unknown"
            confidence = 0.2 if foreground_available else 0.1

        # Recent interaction can suggest availability, but an active schedule item
        # is enough uncertainty to keep this conservative in M1.
        if recent_input is True and foreground_available and not current_schedule:
            interruptibility = "likely_available"
        else:
            interruptibility = "unknown"

        return UserState(
            engagement=engagement,
            interruptibility=interruptibility,
            confidence=confidence,
            evidence=tuple(evidence),
        )
