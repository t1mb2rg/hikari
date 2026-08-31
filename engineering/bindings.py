from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import time
from typing import Mapping


@dataclass(frozen=True, slots=True)
class EngineeringConversationBinding:
    """Durable ownership metadata linking one engineering session to a Hikari conversation."""

    session_id: str
    channel: str
    conversation_id: str
    created_at: float = 0.0

    def __post_init__(self) -> None:
        session_id = self.session_id.strip()
        channel = self.channel.strip()
        conversation_id = self.conversation_id.strip()
        if not session_id:
            raise ValueError("engineering binding session_id must not be empty")
        if not channel:
            raise ValueError("engineering binding channel must not be empty")
        if not conversation_id:
            raise ValueError("engineering binding conversation_id must not be empty")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "conversation_id", conversation_id)
        object.__setattr__(self, "created_at", float(self.created_at or time.time()))

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "session_id": self.session_id,
            "channel": self.channel,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringConversationBinding":
        if payload.get("version") != 1:
            raise ValueError("unsupported engineering binding version")
        return cls(
            session_id=str(payload.get("session_id", "")),
            channel=str(payload.get("channel", "")),
            conversation_id=str(payload.get("conversation_id", "")),
            created_at=float(payload.get("created_at", 0.0)),
        )


class EngineeringConversationBindingStore:
    """Small durable index used by Conversation and the Engineering Worker.

    Engineering state remains owned by EngineeringSession. This index only says
    which Hikari conversation owns a session so terminal results can return to
    the correct transport without an external callback protocol.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    @staticmethod
    def _conversation_key(channel: str, conversation_id: str) -> str:
        return json.dumps(
            [channel.strip(), conversation_id.strip()],
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def bind(self, binding: EngineeringConversationBinding) -> None:
        payload = self._load()
        bindings = payload.setdefault("bindings", {})
        conversations = payload.setdefault("conversations", {})
        if not isinstance(bindings, dict) or not isinstance(conversations, dict):
            raise RuntimeError("engineering binding store is malformed")
        existing = bindings.get(binding.session_id)
        mapping = binding.to_mapping()
        if existing is not None and existing != mapping:
            raise ValueError("engineering session is already bound to another conversation")
        bindings[binding.session_id] = mapping
        conversations[self._conversation_key(binding.channel, binding.conversation_id)] = binding.session_id
        self._save(payload)

    def get(self, session_id: str) -> EngineeringConversationBinding | None:
        payload = self._load()
        bindings = payload.get("bindings", {})
        if not isinstance(bindings, Mapping):
            return None
        raw = bindings.get(session_id.strip())
        if not isinstance(raw, Mapping):
            return None
        return EngineeringConversationBinding.from_mapping(raw)

    def for_conversation(
        self,
        channel: str,
        conversation_id: str,
    ) -> EngineeringConversationBinding | None:
        payload = self._load()
        conversations = payload.get("conversations", {})
        if not isinstance(conversations, Mapping):
            return None
        session_id = conversations.get(self._conversation_key(channel, conversation_id))
        if not isinstance(session_id, str) or not session_id:
            return None
        return self.get(session_id)

    def all(self) -> list[EngineeringConversationBinding]:
        payload = self._load()
        bindings = payload.get("bindings", {})
        if not isinstance(bindings, Mapping):
            return []
        result: list[EngineeringConversationBinding] = []
        for raw in bindings.values():
            if not isinstance(raw, Mapping):
                continue
            try:
                result.append(EngineeringConversationBinding.from_mapping(raw))
            except (TypeError, ValueError):
                continue
        return result

    def _load(self) -> dict[str, object]:
        if not self.path.is_file():
            return {"version": 1, "bindings": {}, "conversations": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise RuntimeError("engineering binding store is unreadable") from None
        if not isinstance(payload, dict) or payload.get("version") != 1:
            raise RuntimeError("engineering binding store is malformed")
        return payload

    def _save(self, payload: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
