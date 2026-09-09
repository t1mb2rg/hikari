from __future__ import annotations

from dataclasses import dataclass, field


CONVERSATION_SCOPES = frozenset({"private", "shared"})


def _required_text(value: str, *, name: str) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    return text


@dataclass(frozen=True)
class UserTurn:
    """One explicit user message arriving through a conversation channel.

    ``actor_id`` identifies the transport principal that authored the turn when the
    adapter can prove it. ``scope`` distinguishes the primary/private conversation
    boundary from a shared multi-user space. They are excluded from legacy dataclass
    equality so existing three-field callers remain compatible; durable replay paths
    must use ``same_wire_turn`` when principal/scope identity matters.
    """

    channel: str
    conversation_id: str
    text: str
    actor_id: str | None = field(default=None, compare=False)
    scope: str = field(default="private", compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required_text(self.channel, name="channel"))
        object.__setattr__(
            self,
            "conversation_id",
            _required_text(self.conversation_id, name="conversation_id"),
        )
        object.__setattr__(self, "text", _required_text(self.text, name="text"))
        actor_id = (
            str(self.actor_id).strip()
            if self.actor_id is not None and str(self.actor_id).strip()
            else None
        )
        scope = str(self.scope).strip().casefold()
        if scope not in CONVERSATION_SCOPES:
            raise ValueError("scope must be private or shared")
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "scope", scope)

    @property
    def is_shared(self) -> bool:
        return self.scope == "shared"

    def same_wire_turn(self, other: object) -> bool:
        if not isinstance(other, UserTurn):
            return False
        return (
            self.channel,
            self.conversation_id,
            self.text,
            self.actor_id,
            self.scope,
        ) == (
            other.channel,
            other.conversation_id,
            other.text,
            other.actor_id,
            other.scope,
        )


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
