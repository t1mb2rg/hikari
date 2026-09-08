from __future__ import annotations

from .engineering_bridge import ConversationEngineeringBridge


class PersistentConversationEngineeringBridge(ConversationEngineeringBridge):
    """Compatibility name for the M7-B-capable Conversation engineering bridge.

    Persistent multi-effect goal intake now lives in the unified bridge used by production.
    This subclass intentionally adds no second routing policy or authority model.
    """

    pass
