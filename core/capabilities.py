from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .operational_state import capture_operational_state
from .self_state import describe_self_state


_CAPABILITY_MANIFEST = {
    "presence": {
        "available": True,
        "summary": "Can remain resident in the Windows background, start at user logon, observe configured sensors, and decide when something deserves attention.",
    },
    "memory": {
        "available": True,
        "summary": "Has persistent local event/conversation memory plus reviewed durable-memory layers; recall is bounded rather than dumping the whole store into every prompt.",
    },
    "conversation": {
        "available": True,
        "summary": "Can hold persistent multi-turn conversations through the shared ConversationEngine; external chat-platform adapters can reuse the same identity and memory.",
    },
    "ambient_context": {
        "available": True,
        "summary": "Can receive bounded host/time/input-activity/foreground context when available. Ambient context is background evidence, not a reason to narrate what the user is doing every turn.",
    },
    "notifications": {
        "available": True,
        "summary": "Presence can proactively reach the user through authorized delivery paths when policy decides an observed event warrants it.",
    },
    "actions": {
        "available": True,
        "summary": "The wider Hikari system has typed action proposal, deterministic authorization, and bounded adapters. Direct conversation alone does not automatically grant arbitrary action authority.",
    },
    "engineering_runtime": {
        "available": True,
        "relationship": "internal_hikari_capability",
        "summary": (
            "Hikari owns durable EngineeringSession state and a separate Engineering Worker "
            "fault domain for repository work. This is an internal capability, not an external "
            "Forge service or callback boundary."
        ),
    },
}


def _engineering_capability_model(engineering_enabled: bool) -> dict[str, object]:
    """Return machine-readable engineering capabilities, independent of one task request.

    ``available`` describes implemented system capability. ``delegated`` describes whether the
    standing Hikari-project mandate lets the Engineering Runtime use that capability without
    asking for per-action approval. Execution code still enforces its own session authority.
    """

    return {
        "engineering.repository.read": {
            "available": engineering_enabled,
            "delegated": engineering_enabled,
            "scope": "project_repository",
        },
        "engineering.repository.write": {
            "available": False,
            "delegated": False,
            "scope": "project_repository",
            "gap": "bounded_write_execution_not_implemented_yet",
        },
        "engineering.commands.run": {
            "available": False,
            "delegated": False,
            "scope": "project_worktree",
            "gap": "command_execution_not_implemented_yet",
        },
        "engineering.tests.run": {
            "available": False,
            "delegated": False,
            "scope": "project_worktree",
            "gap": "test_execution_not_implemented_yet",
        },
        "engineering.git.commit": {
            "available": False,
            "delegated": False,
            "scope": "engineering_branch",
            "gap": "commit_execution_not_implemented_yet",
        },
        "engineering.git.push_non_protected": {
            "available": False,
            "delegated": False,
            "scope": "engineering_branch",
            "gap": "push_execution_not_implemented_yet",
        },
        "engineering.git.open_or_update_draft_pr": {
            "available": False,
            "delegated": False,
            "scope": "engineering_branch",
            "gap": "github_publish_execution_not_implemented_yet",
        },
        "engineering.git.merge_protected": {
            "available": False,
            "delegated": False,
            "scope": "protected_branch",
            "escalation_required": True,
        },
        "engineering.git.force_push": {
            "available": False,
            "delegated": False,
            "scope": "protected_or_shared_history",
            "escalation_required": True,
        },
        "engineering.secrets.modify": {
            "available": False,
            "delegated": False,
            "scope": "secret_configuration",
            "escalation_required": True,
        },
        "engineering.production.deploy": {
            "available": False,
            "delegated": False,
            "scope": "external_or_production_system",
            "escalation_required": True,
        },
    }


def _hikari_project_mandate(engineering_enabled: bool) -> dict[str, object]:
    """Standing delegation for Hikari's own repository.

    The mandate is intentionally broader than the currently implemented worker. A capability may
    be delegated in principle while still unavailable in implementation; that is a real capability
    gap, not a request for per-turn approval.
    """

    return {
        "project_id": "hikari",
        "role": "maintainer",
        "active": engineering_enabled,
        "scope": "configured_hikari_repository",
        "delegated_outcomes": (
            "inspect",
            "edit_project_files",
            "run_project_commands",
            "run_tests",
            "create_engineering_branch",
            "commit_engineering_changes",
            "push_non_protected_engineering_branch",
            "open_or_update_draft_pr",
            "diagnose_and_retry_project_failures",
        ),
        "escalate": (
            "merge_protected_branch",
            "force_push_shared_history",
            "change_or_expose_secrets",
            "production_or_external_deployment",
            "destructive_data_migration",
            "permission_boundary_expansion",
            "project_north_star_change",
            "material_external_cost",
        ),
        "principle": (
            "Within the delegated project scope, Hikari should complete ordinary engineering work "
            "without asking for approval at every step. Escalation is for boundary changes or "
            "high-impact external effects, not routine edits, tests, commits, or draft PR upkeep."
        ),
    }


def describe_capabilities(
    environment: Mapping[str, str] | None = None,
    *,
    operational_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return Hikari's bounded factual self model plus current runtime state.

    Static capability, standing delegation, current chat authority, and point-in-time operational
    observation are deliberately separate. Production Conversation calls this without an explicit
    environment and receives a live cached operational snapshot. Explicit-environment callers
    (primarily deterministic tests) do not perform host/network probes unless they provide a
    snapshot themselves.
    """

    capabilities = deepcopy(_CAPABILITY_MANIFEST)
    self_state = describe_self_state(environment)
    engineering = self_state["engineering"]
    engineering_read_enabled = bool(
        isinstance(engineering, Mapping)
        and engineering.get("conversation_read_only_enabled") is True
    )

    capabilities["capability_model"] = _engineering_capability_model(engineering_read_enabled)
    capabilities["project_mandates"] = {
        "hikari": _hikari_project_mandate(engineering_read_enabled),
    }
    capabilities["current_chat_authority"] = {
        "direct_shell": False,
        "direct_filesystem": False,
        "browser": False,
        "arbitrary_tools": False,
        "engineering_read_session": engineering_read_enabled,
        "engineering_write_session": False,
        "summary": (
            "The Conversation model itself has no direct shell or filesystem sense. It can route "
            "work into Hikari's Engineering Runtime. Standing project delegation and actual worker "
            "capabilities are separate facts: a delegated outcome may still be unavailable until "
            "the Engineering Runtime implements it."
            if engineering_read_enabled
            else (
                "This direct chat path currently provides cognition, context, personality, and "
                "memory continuity, but Engineering Runtime is not enabled for this runtime."
            )
        ),
    }
    capabilities["self_state"] = self_state

    if operational_state is not None:
        capabilities["operational_state"] = dict(operational_state)
    elif environment is None:
        capabilities["operational_state"] = capture_operational_state()
    else:
        capabilities["operational_state"] = {
            "version": 1,
            "overall": "unknown",
            "components": {},
            "epistemic_rule": (
                "No point-in-time operational probe was requested for this explicit environment. "
                "Do not infer runtime health from static capability configuration."
            ),
        }
    return capabilities
