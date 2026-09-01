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


def describe_capabilities(
    environment: Mapping[str, str] | None = None,
    *,
    operational_state: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Return Hikari's bounded factual self model plus current runtime state.

    Static capability, current chat authority, and point-in-time operational
    observation are deliberately separate. Production Conversation calls this
    without an explicit environment and receives a live cached operational
    snapshot. Explicit-environment callers (primarily deterministic tests) do
    not perform host/network probes unless they provide a snapshot themselves.
    """

    capabilities = deepcopy(_CAPABILITY_MANIFEST)
    self_state = describe_self_state(environment)
    engineering = self_state["engineering"]
    engineering_read_enabled = bool(
        isinstance(engineering, Mapping)
        and engineering.get("conversation_read_only_enabled") is True
    )

    capabilities["current_chat_authority"] = {
        "direct_shell": False,
        "direct_filesystem": False,
        "browser": False,
        "arbitrary_tools": False,
        "engineering_read_session": engineering_read_enabled,
        "engineering_write_session": False,
        "summary": (
            "This conversation can create a bounded read-only EngineeringSession when the "
            "Engineering Runtime is enabled. Repository inspection is completed by Hikari's "
            "separate internal worker; the chat model itself has no direct shell or filesystem "
            "sense."
            if engineering_read_enabled
            else (
                "This direct chat path currently provides cognition, context, personality, and "
                "memory continuity, but no direct shell/filesystem access and no attached "
                "EngineeringSession authority."
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