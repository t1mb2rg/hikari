from __future__ import annotations

from .maintainer import (
    is_maintainer_authority,
    is_push_authority,
    is_read_only_authority,
    project_maintainer_authority,
    project_push_authority,
)
from .session import (
    EngineeringAuthority,
    EngineeringProtocolError,
    EngineeringTurn,
)


SUPPORTED_ENGINEERING_EFFECTS = frozenset(
    {
        "inspect_project",
        "run_project_command",
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    }
)

# These effects may be replayed after Worker ownership changes because Hikari can make
# the retry deterministic and bounded. ``run_project_command`` is deliberately absent:
# a command may have completed an irreversible local side effect before the old Worker
# died, so its outcome is uncertain and automatic replay would risk duplicate execution.
RESTART_REPLAY_SAFE_EFFECTS = frozenset(
    {
        "inspect_project",
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    }
)

_EFFECT_PREFIX = "Requested effect: "


def authority_for_effect(effect: str) -> EngineeringAuthority:
    """Return the deterministic turn authority for one already-resolved engineering effect.

    This function never interprets natural language. Conversation/planning may resolve an
    effect, but the low-level authority envelope remains Hikari-owned deterministic code.
    """

    normalized = effect.strip()
    if normalized == "inspect_project":
        return EngineeringAuthority.read_only()
    if normalized == "run_project_command":
        return EngineeringAuthority(
            repository_read=True,
            run_commands=True,
        )
    if normalized == "maintain_project":
        return project_maintainer_authority()
    if normalized in {"push_engineering_branch", "open_or_update_draft_pr"}:
        return project_push_authority()
    raise EngineeringProtocolError(f"unsupported engineering effect: {normalized!r}")


def turn_effect(turn: EngineeringTurn) -> str:
    """Read the deterministic effect from one Bridge-authored EngineeringTurn.

    The last marker wins because user/semantic text may mention the same literal phrase
    earlier in the context. Legacy turns without the marker are mapped from their narrow
    authority profile so restart recovery can retain the pre-M7-C behavior safely.
    """

    if not isinstance(turn, EngineeringTurn):
        raise TypeError("turn_effect requires EngineeringTurn")

    _, marker, tail = turn.context.rpartition(_EFFECT_PREFIX)
    if marker:
        effect = tail.split(".", 1)[0].splitlines()[0].strip()
        if effect in SUPPORTED_ENGINEERING_EFFECTS:
            return effect
        return effect

    authority = turn.authority
    if is_maintainer_authority(authority):
        return "maintain_project"
    if is_push_authority(authority):
        # Before Draft PR support, blank publish turns meant push. Preserve that legacy
        # interpretation rather than guessing a newer effect during crash recovery.
        return "push_engineering_branch"
    if is_read_only_authority(authority):
        return "inspect_project"
    if (
        authority.repository_read
        and not authority.repository_write
        and authority.run_commands
        and not authority.run_tests
        and not authority.network
        and not authority.publish
        and not authority.outside_repo
    ):
        return "run_project_command"
    return ""
