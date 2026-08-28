from __future__ import annotations

import json
from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.engine import ConversationEngine, THIN_HIKARI_SYSTEM_INSTRUCTIONS
from conversation.models import UserTurn
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages) -> str:
        self.calls.append(list(messages))
        return "嗯，我在。"


def test_cli_accepts_thin_prompt_profile():
    args = build_parser().parse_args(["--prompt-profile", "thin"])

    assert args.prompt_profile == "thin"


def test_thin_prompt_keeps_grounding_without_full_voice_profile(tmp_path: Path):
    provider = FakeProvider()
    engine = ConversationEngine(
        provider,
        MemoryStore(tmp_path / "memory.db"),
        relationship_context={
            "kind": "primary_local_user",
            "basis": "trusted_runtime_binding",
        },
        personality_profile=None,
        voice_profile=None,
        system_instructions=THIN_HIKARI_SYSTEM_INSTRUCTIONS,
    )

    engine.respond(UserTurn("cli", "gemma-test", "hikari"))

    call = provider.calls[0]
    assert call[0].role == "system"
    assert call[0].content == THIN_HIKARI_SYSTEM_INSTRUCTIONS
    assert "Memory provenance is strict" in call[0].content
    assert "Direct conversation alone does not grant" in call[0].content

    grounding = json.loads(call[1].content)
    assert grounding["identity"]["name"] == "Hikari"
    assert grounding["relationship"]["kind"] == "primary_local_user"
    assert grounding["personality"] == {}
    assert grounding["voice"] == {}
    assert grounding["memory_provenance"]["current_user_turn"]["recalled"] is False
    assert grounding["capabilities"]["memory"]["available"] is True
