"""Direct conversation boundaries for Hikari."""

from .engine import (
    ASSISTANT_EVENT_TYPE,
    INTERACTIVE_SYSTEM_INSTRUCTIONS,
    USER_EVENT_TYPE,
    ConversationEngine,
)
from .gateway import ConversationGateway, ConversationTransport
from .models import AssistantReply, UserTurn
from .natural import (
    NaturalConversationEngine,
    NaturalConversationOutput,
    parse_natural_conversation_output,
)
from .whiteboard import (
    WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    WhiteboardConversationEngine,
    WhiteboardOutput,
    parse_whiteboard_output,
)

__all__ = [
    "ASSISTANT_EVENT_TYPE",
    "INTERACTIVE_SYSTEM_INSTRUCTIONS",
    "USER_EVENT_TYPE",
    "WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS",
    "AssistantReply",
    "ConversationEngine",
    "ConversationGateway",
    "ConversationTransport",
    "NaturalConversationEngine",
    "NaturalConversationOutput",
    "UserTurn",
    "WhiteboardConversationEngine",
    "WhiteboardOutput",
    "parse_natural_conversation_output",
    "parse_whiteboard_output",
]
