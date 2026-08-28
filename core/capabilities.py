from __future__ import annotations

from copy import deepcopy


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
        "summary": "Presence can proactively reach the user through the authorized Windows notification path when Attention decides an observed event warrants it.",
    },
    "actions": {
        "available": True,
        "summary": "The wider Hikari system has typed action proposal, deterministic authorization, and bounded adapters. Direct conversation alone does not automatically grant action authority.",
    },
    "forge": {
        "available": True,
        "summary": "The wider Hikari system has a confirmation-gated Forge engineering boundary for verified code changes. This conversation path cannot invoke Forge unless an explicit authorized action path is attached.",
    },
    "current_chat_authority": {
        "shell": False,
        "filesystem": False,
        "browser": False,
        "forge": False,
        "arbitrary_tools": False,
        "summary": "This direct chat path currently provides cognition, context, personality, and memory continuity, but no direct execution tools.",
    },
}


def describe_capabilities() -> dict[str, object]:
    """Return Hikari's bounded, user-facing self model.

    The manifest describes system-level capabilities separately from the
    authority available to the current conversation path. Keeping those two
    concepts distinct prevents a chat model from either inventing powers or
    incorrectly claiming that Hikari has no memory/presence at all.
    """

    return deepcopy(_CAPABILITY_MANIFEST)
