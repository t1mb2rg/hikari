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


def extract_group_message(message: object, *, bot_self_id: str | int) -> str | None:
    """Accept group messages that address Hikari with an at-self mention.

    The at-self segment(s) are stripped; every remaining segment must be text.
    Mentions of other members, CQ-code string payloads, images and any other
    segment type are rejected so shared group noise never triggers a model call.
    """

    if isinstance(message, str):
        return None
    try:
        segments = list(message)  # type: ignore[arg-type]
    except TypeError:
        return None

    self_id_text = str(bot_self_id).strip()
    if not self_id_text:
        return None
    addressed = False
    parts: list[str] = []
    for segment in segments:
        if _segment_type(segment) == "at":
            data = _segment_data(segment)
            target = data.get("qq") if isinstance(data, Mapping) else None
            if str(target).strip() == self_id_text:
                addressed = True
                continue
            return None
        if _segment_type(segment) != "text":
            return None
        data = _segment_data(segment)
        if not isinstance(data, Mapping):
            return None
        text = data.get("text")
        if not isinstance(text, str):
            return None
        parts.append(text)
    if not addressed:
        return None
    joined = "".join(parts).strip()
    return joined or None


def normalize_group_message(
    *,
    bot_self_id: str | int,
    group_id: str | int,
    user_id: str | int,
    message_id: str | int,
    message: object,
    allowed_user_ids: frozenset[str],
    allowed_group_ids: frozenset[str],
) -> tuple[str, UserTurn] | None:
    """Map an at-self group message from an allowlisted user into a group turn.

    Fail-closed: the sender must be allowlisted, the group must be allowlisted,
    and the message must carry an at-self mention with pure text otherwise.
    """

    user_id_text = str(user_id).strip()
    group_id_text = str(group_id).strip()
    if not user_id_text or not group_id_text:
        return None
    if user_id_text not in allowed_user_ids:
        return None
    if group_id_text not in allowed_group_ids:
        return None
    text = extract_group_message(message, bot_self_id=bot_self_id)
    if text is None:
        return None

    request_id = (
        f"qq:{str(bot_self_id).strip()}:g:{group_id_text}:{str(message_id).strip()}"
    )
    if request_id.endswith(":") or "::" in request_id:
        return None
    return (
        request_id,
        UserTurn(
            channel="qq",
            conversation_id=f"group:{group_id_text}",
            text=text,
        ),
    )
