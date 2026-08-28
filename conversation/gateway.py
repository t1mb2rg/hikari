from __future__ import annotations

from typing import Protocol, runtime_checkable

from .engine import ConversationEngine
from .models import AssistantReply, UserTurn


@runtime_checkable
class ConversationTransport(Protocol):
    """Platform edge for one chat bot or interactive transport."""

    def receive(self) -> UserTurn | None:
        ...

    def send(self, reply: AssistantReply) -> None:
        ...


class ConversationGateway:
    """Route explicit user messages through one shared Hikari conversation core."""

    def __init__(
        self,
        engine: ConversationEngine,
        transport: ConversationTransport,
    ) -> None:
        if not isinstance(engine, ConversationEngine):
            raise TypeError("ConversationGateway requires ConversationEngine")
        if not isinstance(transport, ConversationTransport):
            raise TypeError("ConversationGateway requires ConversationTransport")
        self.engine = engine
        self.transport = transport

    def cycle_once(self) -> AssistantReply | None:
        turn = self.transport.receive()
        if turn is None:
            return None
        reply = self.engine.respond(turn)
        self.transport.send(reply)
        return reply
