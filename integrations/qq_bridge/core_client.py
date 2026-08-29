from __future__ import annotations

import asyncio
from typing import Any

from conversation.models import AssistantReply, UserTurn
from conversation.protocol import (
    CONVERSATION_PROTOCOL,
    ConversationProtocolError,
    decode_envelope,
    encode_envelope,
    hello_envelope,
    parse_reply,
    turn_envelope,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed


class ConversationCoreClient:
    """Persistent serialized client from one platform bridge into Hikari core."""

    def __init__(
        self,
        url: str,
        *,
        adapter_id: str,
        channel: str,
        shared_secret: str | None = None,
    ) -> None:
        self.url = url.strip()
        self.adapter_id = adapter_id.strip()
        self.channel = channel.strip()
        self.shared_secret = shared_secret.strip() if shared_secret else None
        if not self.url.startswith(("ws://", "wss://")):
            raise ValueError("conversation core url must use ws:// or wss://")
        if not self.adapter_id:
            raise ValueError("adapter_id must not be empty")
        if not self.channel:
            raise ValueError("channel must not be empty")
        self._connection: ClientConnection | None = None
        self._lock = asyncio.Lock()

    async def _open(self) -> ClientConnection:
        connection = await connect(
            self.url,
            open_timeout=10,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            max_size=1024 * 1024,
            proxy=None,
        )
        await connection.send(
            encode_envelope(
                hello_envelope(
                    adapter_id=self.adapter_id,
                    channel=self.channel,
                    secret=self.shared_secret,
                )
            )
        )
        raw = await connection.recv()
        payload = decode_envelope(raw)
        if payload.get("type") == "error":
            await connection.close()
            raise RuntimeError(str(payload.get("message", "conversation host rejected bridge")))
        if payload.get("type") != "hello_ack":
            await connection.close()
            raise ConversationProtocolError("conversation host did not acknowledge hello")
        if payload.get("protocol") != CONVERSATION_PROTOCOL:
            await connection.close()
            raise ConversationProtocolError("conversation host protocol mismatch")
        if payload.get("adapter_id") != self.adapter_id:
            await connection.close()
            raise ConversationProtocolError("conversation host adapter acknowledgement mismatch")
        return connection

    async def _ensure_connection(self) -> ClientConnection:
        if self._connection is None:
            self._connection = await self._open()
        return self._connection

    async def _reset_connection(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                await connection.close()
            except Exception:
                pass

    async def request(self, request_id: str, turn: UserTurn) -> AssistantReply:
        if not isinstance(turn, UserTurn):
            raise TypeError("turn must be UserTurn")
        if turn.channel != self.channel:
            raise ValueError("turn channel must match bridge channel")

        async with self._lock:
            last_error: BaseException | None = None
            for attempt in range(2):
                try:
                    connection = await self._ensure_connection()
                    await connection.send(
                        encode_envelope(turn_envelope(request_id=request_id, turn=turn))
                    )
                    raw = await connection.recv()
                    payload: dict[str, Any] = decode_envelope(raw)
                    if payload.get("type") == "error":
                        raise RuntimeError(
                            str(payload.get("message", "conversation host request failed"))
                        )
                    response_id, reply, _duplicate = parse_reply(payload)
                    if response_id != request_id:
                        raise ConversationProtocolError("conversation reply request_id mismatch")
                    if reply.channel != turn.channel:
                        raise ConversationProtocolError("conversation reply channel mismatch")
                    if reply.conversation_id != turn.conversation_id:
                        raise ConversationProtocolError("conversation reply target mismatch")
                    return reply
                except (ConnectionClosed, OSError, TimeoutError) as exc:
                    last_error = exc
                    await self._reset_connection()
                    if attempt == 0:
                        continue
                    break
            raise RuntimeError("conversation host is unavailable") from last_error

    async def close(self) -> None:
        async with self._lock:
            await self._reset_connection()
