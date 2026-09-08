from __future__ import annotations

from .maintainer import project_maintainer_authority, project_push_authority
from .session import EngineeringAuthority, EngineeringProtocolError


SUPPORTED_ENGINEERING_EFFECTS = frozenset(
    {
        "inspect_project",
        "run_project_command",
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    }
)


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
