from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path

import pytest

from brain.model_reasoner import ChatMessage
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from conversation.protocol import (
    decode_envelope,
    encode_envelope,
    hello_envelope,
    turn_envelope,
)
from conversation.receipts import ConversationReceiptStore
from conversation.remote import ConversationRequestProcessor, ConversationWebSocketHost
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _processor(tmp_path: Path, provider: FakeProvider) -> ConversationRequestProcessor:
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    return ConversationRequestProcessor(
        engine,
        ConversationReceiptStore(tmp_path / "receipts.db"),
    )


def test_request_processor_deduplicates_same_request(tmp_path: Path):
    provider = FakeProvider(["第一次回复"])
    processor = _processor(tmp_path, provider)
    turn = UserTurn("qq", "private:7", "你好")

    first, first_duplicate = processor.process("qq:bot:1", turn)
    second, second_duplicate = processor.process("qq:bot:1", turn)

    assert first == second
    assert first_duplicate is False
    assert second_duplicate is True
    assert len(provider.calls) == 1
    events = MemoryStore(tmp_path / "memory.db").recent_events(10)
    assert len(events) == 2


def test_request_id_cannot_be_reused_for_different_turn(tmp_path: Path):
    provider = FakeProvider(["reply"])
    processor = _processor(tmp_path, provider)
    processor.process("same", UserTurn("qq", "private:7", "one"))

    with pytest.raises(ValueError, match="different user turn"):
        processor.process("same", UserTurn("qq", "private:7", "two"))


class FakeWebSocket:
    def __init__(self, incoming: list[str]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[str] = []
        self.closed: tuple[int, str] | None = None

    async def recv(self):
        return self.incoming.popleft()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.incoming:
            raise StopAsyncIteration
        return self.incoming.popleft()

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


def test_websocket_host_routes_authenticated_turn(tmp_path: Path):
    provider = FakeProvider(["在呢。"])
    processor = _processor(tmp_path, provider)
    host = ConversationWebSocketHost(processor, shared_secret="secret")
    turn = UserTurn("qq", "private:7", "hikari")
    websocket = FakeWebSocket(
        [
            encode_envelope(
                hello_envelope(
                    adapter_id="qq.main",
                    channel="qq",
                    secret="secret",
                )
            ),
            encode_envelope(turn_envelope(request_id="qq:bot:9", turn=turn)),
        ]
    )

    asyncio.run(host.handle(websocket))  # type: ignore[arg-type]

    assert decode_envelope(websocket.sent[0])["type"] == "hello_ack"
    reply = decode_envelope(websocket.sent[1])
    assert reply["type"] == "reply"
    assert reply["request_id"] == "qq:bot:9"
    assert reply["reply"]["text"] == "在呢。"
    assert websocket.closed is None


def test_websocket_host_rejects_wrong_secret(tmp_path: Path):
    host = ConversationWebSocketHost(
        _processor(tmp_path, FakeProvider(["unused"])),
        shared_secret="right",
    )
    websocket = FakeWebSocket(
        [
            encode_envelope(
                hello_envelope(
                    adapter_id="qq.main",
                    channel="qq",
                    secret="wrong",
                )
            )
        ]
    )

    asyncio.run(host.handle(websocket))  # type: ignore[arg-type]

    assert decode_envelope(websocket.sent[0])["type"] == "error"
    assert websocket.closed == (1008, "unauthorized")
