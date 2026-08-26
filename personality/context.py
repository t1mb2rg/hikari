from __future__ import annotations

from typing import Any

from .profile import PersonalityProfile


HIKARI_PERSONALITY_KEY = "_hikari_personality"


def personality_as_context(profile: PersonalityProfile) -> dict[str, Any]:
    """Serialize stable personality state for the Reasoner path.

    This payload is reasoning context, not an environmental observation. Core
    Presence should attach it only to the transient Event passed to Reasoner and
    must not persist it back into event history.
    """

    return {
        "version": profile.version,
        "traits": dict(profile.traits),
    }
