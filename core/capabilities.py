from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy

from .delegation import hikari_engineering_capabilities, hikari_project_mandate
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


def describe_capabilities(
    environment: Mapping[str, str] | None = None,
    *,
    operational_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return Hikari's factual capability, delegation, and current runtime state.

    Static capability, standing project delegation, current chat authority, and point-in-time
    operational observation are separate facts. Production Conversation calls this without an
    explicit environment and receives a live cached operational snapshot. Explicit-environment
    callers (primarily deterministic tests) do not perform host/network probes unless they provide
    a snapshot themselves.
    """

    capabilities = deepcopy(_CAPABILITY_MANIFEST)
    self_state = describe_self_state(environment)
    engineering = self_state["engineering"]
    engineering_read_enabled = bool(
        isinstance(engineering, Mapping)
        and engineering.get("conversation_read_only_enabled") is True
    )
    engineering_maintainer_enabled = bool(
        isinstance(engineering, Mapping)
        and engineering.get("conversation_maintainer_session_enabled") is True
    )

    capability_model = hikari_engineering_capabilities(engineering_read_enabled)
    capabilities["capability_model"] = {
        key: value.to_mapping() for key, value in capability_model.items()
    }
    capabilities["project_mandates"] = {
        "hikari": hikari_project_mandate(engineering_read_enabled).to_mapping(),
    }
    capabilities["current_chat_authority"] = {
        "direct_shell": False,
        "direct_filesystem": False,
        "browser": False,
        "arbitrary_tools": False,
        "engineering_read_session": engineering_read_enabled,
        "engineering_write_session": engineering_maintainer_enabled,
        "summary": (
            "The Conversation model itself has no direct shell or filesystem sense. It can route "
            "work into Hikari's Engineering Runtime. Inside the standing Hikari-project maintainer "
            "mandate, routine read/edit/test/engineering-branch commit work can run without per-action "
            "approval. Standing delegation and actual implementation remain separate facts."
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
