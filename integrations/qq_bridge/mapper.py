from __future__ import annotations

from collections.abc import Mapping

from conversation.models import UserTurn


def _segment_type(segment: object) -> object:
    if isinstance(segment, Mapping):
        return segment.get("type")
    return getattr(segment, "type", None)


def _segment_data(segment: object) -> object:
    if isinstance(segment, Mapping):
        return segment.get("data")
    return getattr(segment, "data", None)


def extract_text_message(message: object) -> str | None:
    """Accept only pure-text OneBot payloads for the first QQ physical gate."""

    if isinstance(message, str):
        text = message.strip()
        if not text or "[CQ:" in text:
            return None
        return text

    try:
        segments = list(message)  # type: ignore[arg-type]
    except TypeError:
        return None

    parts: list[str] = []
    for segment in segments:
        if _segment_type(segment) != "text":
            return None
        data = _segment_data(segment)
        if not isinstance(data, Mapping):
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    joined = "".join(parts).strip()
    return joined or None


def normalize_private_message(
    *,
    bot_self_id: str | int,
    user_id: str | int,
    message_id: str | int,
    message: object,
    allowed_user_ids: frozenset[str],
) -> tuple[str, UserTurn] | None:
    user_id_text = str(user_id).strip()
    if not user_id_text or user_id_text not in allowed_user_ids:
        return None
    text = extract_text_message(message)
    if text is None:
        return None

    request_id = f"qq:{str(bot_self_id).strip()}:{str(message_id).strip()}"
    if request_id.endswith(":"):
        return None
    return (
        request_id,
        UserTurn(
            channel="qq",
            conversation_id=f"private:{user_id_text}",
            text=text,
        ),
    )
