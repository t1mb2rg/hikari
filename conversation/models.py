from __future__ import annotations

from dataclasses import dataclass


def _required_text(value: str, *, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


@dataclass(frozen=True)
class UserTurn:
    """One explicit user message arriving through a conversation channel."""

    channel: str
    conversation_id: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required_text(self.channel, name="channel"))
        object.__setattr__(
            self,
            "conversation_id",
            _required_text(self.conversation_id, name="conversation_id"),
        )
        object.__setattr__(self, "text", _required_text(self.text, name="text"))


@dataclass(frozen=True)
class AssistantReply:
    """One Hikari reply routed back to the conversation that produced it."""

    channel: str
    conversation_id: str
    text: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required_text(self.channel, name="channel"))
        object.__setattr__(
            self,
            "conversation_id",
            _required_text(self.conversation_id, name="conversation_id"),
        )
        object.__setattr__(self, "text", _required_text(self.text, name="text"))
