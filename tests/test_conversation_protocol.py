from __future__ import annotations

import pytest

from conversation.models import AssistantReply, UserTurn
from conversation.protocol import (
    CONVERSATION_PROTOCOL,
    ConversationProtocolError,
    decode_envelope,
    encode_envelope,
    hello_envelope,
    parse_hello,
    parse_reply,
    parse_turn,
    reply_envelope,
    turn_envelope,
)


def test_hello_round_trip():
    payload = hello_envelope(adapter_id="qq.main", channel="qq", secret="secret")
    decoded = decode_envelope(encode_envelope(payload))

    adapter_id, channel, secret = parse_hello(decoded)

    assert decoded["protocol"] == CONVERSATION_PROTOCOL
    assert adapter_id == "qq.main"
    assert channel == "qq"
    assert secret == "secret"


def test_turn_and_reply_round_trip():
    turn = UserTurn(channel="qq", conversation_id="private:7", text="你好")
    request_id, parsed_turn = parse_turn(
        decode_envelope(
            encode_envelope(turn_envelope(request_id="qq:bot:1", turn=turn))
        )
    )
    assert request_id == "qq:bot:1"
    assert parsed_turn == turn

    reply = AssistantReply(channel="qq", conversation_id="private:7", text="在呢。")
    response_id, parsed_reply, duplicate = parse_reply(
        decode_envelope(
            encode_envelope(
                reply_envelope(
                    request_id=request_id,
                    reply=reply,
                    duplicate=True,
                )
            )
        )
    )
    assert response_id == request_id
    assert parsed_reply == reply
    assert duplicate is True


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '{"hello":"world"}',
    ],
)
def test_invalid_wire_message_is_rejected(raw: str):
    with pytest.raises(ConversationProtocolError):
        decode_envelope(raw)


def test_protocol_rejects_empty_turn_fields():
    with pytest.raises(ConversationProtocolError, match="turn.text"):
        parse_turn(
            {
                "type": "turn",
                "request_id": "r1",
                "turn": {
                    "channel": "qq",
                    "conversation_id": "private:7",
                    "text": "   ",
                },
            }
        )
