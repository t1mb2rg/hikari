from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any

from .models import AssistantReply, UserTurn


CONVERSATION_PROTOCOL = "hikari.conversation.v1"
MAX_WIRE_MESSAGE_BYTES = 1024 * 1024


class ConversationProtocolError(ValueError):
    """Raised when a remote conversation envelope violates the wire contract."""


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConversationProtocolError(f"{name} must be a non-empty string")
    return value.strip()


def decode_envelope(raw: str | bytes) -> dict[str, Any]:
    if isinstance(raw, bytes):
        if len(raw) > MAX_WIRE_MESSAGE_BYTES:
            raise ConversationProtocolError("wire message exceeds size limit")
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConversationProtocolError("wire message must be UTF-8") from exc
    elif not isinstance(raw, str):
        raise ConversationProtocolError("wire message must be text or UTF-8 bytes")

    if len(raw.encode("utf-8")) > MAX_WIRE_MESSAGE_BYTES:
        raise ConversationProtocolError("wire message exceeds size limit")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ConversationProtocolError("wire message must be valid JSON") from exc
    if not isinstance(payload, dict):
        raise ConversationProtocolError("wire envelope must be a JSON object")
    _required_text(payload.get("type"), name="type")
    return payload


def encode_envelope(payload: Mapping[str, object]) -> str:
    if not isinstance(payload, Mapping):
        raise TypeError("payload must be a mapping")
    encoded = json.dumps(dict(payload), ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > MAX_WIRE_MESSAGE_BYTES:
        raise ConversationProtocolError("wire message exceeds size limit")
    return encoded


def hello_envelope(*, adapter_id: str, channel: str, secret: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "hello",
        "protocol": CONVERSATION_PROTOCOL,
        "adapter_id": _required_text(adapter_id, name="adapter_id"),
        "channel": _required_text(channel, name="channel"),
    }
    if secret:
        payload["secret"] = secret
    return payload


def parse_hello(payload: Mapping[str, object]) -> tuple[str, str, str | None]:
    if payload.get("type") != "hello":
        raise ConversationProtocolError("first envelope must be hello")
    if payload.get("protocol") != CONVERSATION_PROTOCOL:
        raise ConversationProtocolError("unsupported conversation protocol")
    adapter_id = _required_text(payload.get("adapter_id"), name="adapter_id")
    channel = _required_text(payload.get("channel"), name="channel")
    secret = payload.get("secret")
    if secret is not None and not isinstance(secret, str):
        raise ConversationProtocolError("secret must be a string when provided")
    return adapter_id, channel, secret


def hello_ack_envelope(*, adapter_id: str) -> dict[str, object]:
    return {
        "type": "hello_ack",
        "protocol": CONVERSATION_PROTOCOL,
        "adapter_id": _required_text(adapter_id, name="adapter_id"),
    }


def turn_envelope(*, request_id: str, turn: UserTurn) -> dict[str, object]:
    if not isinstance(turn, UserTurn):
        raise TypeError("turn must be UserTurn")
    return {
        "type": "turn",
        "request_id": _required_text(request_id, name="request_id"),
        "turn": {
            "channel": turn.channel,
            "conversation_id": turn.conversation_id,
            "text": turn.text,
        },
    }


def parse_turn(payload: Mapping[str, object]) -> tuple[str, UserTurn]:
    if payload.get("type") != "turn":
        raise ConversationProtocolError("expected turn envelope")
    request_id = _required_text(payload.get("request_id"), name="request_id")
    raw_turn = payload.get("turn")
    if not isinstance(raw_turn, Mapping):
        raise ConversationProtocolError("turn must be an object")
    try:
        turn = UserTurn(
            channel=_required_text(raw_turn.get("channel"), name="turn.channel"),
            conversation_id=_required_text(
                raw_turn.get("conversation_id"),
                name="turn.conversation_id",
            ),
            text=_required_text(raw_turn.get("text"), name="turn.text"),
        )
    except ValueError as exc:
        raise ConversationProtocolError(str(exc)) from exc
    return request_id, turn


def reply_envelope(*, request_id: str, reply: AssistantReply, duplicate: bool = False) -> dict[str, object]:
    if not isinstance(reply, AssistantReply):
        raise TypeError("reply must be AssistantReply")
    return {
        "type": "reply",
        "request_id": _required_text(request_id, name="request_id"),
        "duplicate": bool(duplicate),
        "reply": {
            "channel": reply.channel,
            "conversation_id": reply.conversation_id,
            "text": reply.text,
        },
    }


def parse_reply(payload: Mapping[str, object]) -> tuple[str, AssistantReply, bool]:
    if payload.get("type") != "reply":
        raise ConversationProtocolError("expected reply envelope")
    request_id = _required_text(payload.get("request_id"), name="request_id")
    raw_reply = payload.get("reply")
    if not isinstance(raw_reply, Mapping):
        raise ConversationProtocolError("reply must be an object")
    try:
        reply = AssistantReply(
            channel=_required_text(raw_reply.get("channel"), name="reply.channel"),
            conversation_id=_required_text(
                raw_reply.get("conversation_id"),
                name="reply.conversation_id",
            ),
            text=_required_text(raw_reply.get("text"), name="reply.text"),
        )
    except ValueError as exc:
        raise ConversationProtocolError(str(exc)) from exc
    return request_id, reply, bool(payload.get("duplicate", False))


def error_envelope(*, code: str, message: str, request_id: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "error",
        "code": _required_text(code, name="code"),
        "message": _required_text(message, name="message"),
    }
    if request_id:
        payload["request_id"] = request_id
    return payload
