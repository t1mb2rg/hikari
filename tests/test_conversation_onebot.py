from __future__ import annotations

from email.message import Message
import hashlib
import hmac
from pathlib import Path
from urllib.request import Request

import pytest

from conversation.models import AssistantReply
from conversation.onebot import (
    OneBotTransport,
    normalize_onebot_private_event,
    parse_allowed_user_ids,
    verify_onebot_signature,
)


def test_parse_allowed_user_ids_is_trimmed_and_deduplicated():
    assert parse_allowed_user_ids(" 123,456,123 ,, ") == frozenset({"123", "456"})
    assert parse_allowed_user_ids(None) == frozenset()


def test_normalize_onebot_private_text_event():
    turn = normalize_onebot_private_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 123456,
            "message": "  你好 hikari  ",
        },
        allowed_user_ids=frozenset({"123456"}),
    )

    assert turn is not None
    assert turn.channel == "qq"
    assert turn.conversation_id == "private:123456"
    assert turn.text == "你好 hikari"


def test_normalize_onebot_segmented_text_event():
    turn = normalize_onebot_private_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": "42",
            "message": [
                {"type": "text", "data": {"text": "你"}},
                {"type": "text", "data": {"text": "好"}},
            ],
        },
        allowed_user_ids=frozenset({"42"}),
    )

    assert turn is not None
    assert turn.text == "你好"


@pytest.mark.parametrize(
    "event",
    [
        {"post_type": "notice", "message_type": "private", "user_id": 1, "message": "hi"},
        {"post_type": "message", "message_type": "group", "user_id": 1, "message": "hi"},
        {"post_type": "message", "message_type": "private", "user_id": 2, "message": "hi"},
        {"post_type": "message", "message_type": "private", "user_id": 1, "message": "[CQ:image,file=x]"},
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 1,
            "message": [{"type": "image", "data": {"file": "x"}}],
        },
    ],
)
def test_non_private_unapproved_or_non_text_event_is_ignored(event):
    assert normalize_onebot_private_event(
        event,
        allowed_user_ids=frozenset({"1"}),
    ) is None


def test_transport_fails_closed_without_allowlist():
    with pytest.raises(ValueError, match="ALLOWED_USER_IDS"):
        OneBotTransport(
            api_base_url="http://127.0.0.1:3000",
            allowed_user_ids=frozenset(),
        )


def test_transport_enqueue_only_accepts_approved_private_text():
    transport = OneBotTransport(
        api_base_url="http://127.0.0.1:3000",
        allowed_user_ids=frozenset({"7"}),
    )

    assert transport.enqueue_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 8,
            "message": "no",
        }
    ) is False
    assert transport.enqueue_event(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": 7,
            "message": "yes",
        }
    ) is True

    turn = transport.receive()
    assert turn is not None
    assert turn.text == "yes"
    assert turn.conversation_id == "private:7"


def test_onebot_signature_verification():
    body = b'{"hello":"world"}'
    secret = "local-secret"
    digest = hmac.new(secret.encode(), body, hashlib.sha1).hexdigest()

    assert verify_onebot_signature(body, f"sha1={digest}", secret) is True
    assert verify_onebot_signature(body, "sha1=wrong", secret) is False
    assert verify_onebot_signature(body, None, secret) is False
    assert verify_onebot_signature(body, None, None) is True


def test_transport_send_posts_private_message(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"status":"ok","retcode":0}'

    def fake_urlopen(request: Request, timeout: int):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["authorization"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr("conversation.onebot.urlopen", fake_urlopen)
    transport = OneBotTransport(
        api_base_url="http://127.0.0.1:3000/",
        access_token="secret-token",
        allowed_user_ids=frozenset({"123"}),
    )

    transport.send(
        AssistantReply(
            channel="qq",
            conversation_id="private:123",
            text="在呢。",
        )
    )

    assert captured["url"] == "http://127.0.0.1:3000/send_private_msg"
    assert captured["authorization"] == "Bearer secret-token"
    assert captured["timeout"] == 15
    assert b'"user_id": 123' in captured["data"]
    assert "在呢。".encode("utf-8") in captured["data"]
