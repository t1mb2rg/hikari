from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.jarvis_openjarvis import (
    JARVIS_EPISTEMIC_BOUNDARY_INSTRUCTIONS,
    JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS,
    OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS,
)
from conversation.models import UserTurn
from conversation.whiteboard import WhiteboardConversationEngine
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.reply


def test_production_composes_boundary_without_mutating_openjarvis_control():
    assert "EPISTEMIC BOUNDARY:" not in OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
    assert JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS == (
        OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
        + "\n\n"
        + JARVIS_EPISTEMIC_BOUNDARY_INSTRUCTIONS
    )
    assert JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS.startswith(
        OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
    )


def test_boundary_targets_runtime_and_action_claims_without_rewriting_persona():
    boundary = JARVIS_EPISTEMIC_BOUNDARY_INSTRUCTIONS

    assert "persona archetype is not evidence of capability" in boundary
    assert "hidden background work" in boundary
    assert "authorized action path" in boundary
    assert "Dry wit, metaphor, and vivid phrasing are welcome" in boundary


def test_cli_accepts_jarvis_production_profile():
    args = build_parser().parse_args(["--prompt-profile", "jarvis-production"])

    assert args.prompt_profile == "jarvis-production"


def test_production_whiteboard_exposes_only_bounded_prompt_and_real_turn(
    tmp_path: Path,
):
    provider = FakeProvider("在等您开口，先生。")
    engine = WhiteboardConversationEngine(
        provider,
        MemoryStore(tmp_path / "memory.db"),
        relationship_context={
            "kind": "should_not_enter_production_prompt",
            "basis": "trusted_runtime_binding",
        },
        history_limit=12,
        system_instructions=JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS,
    )

    reply = engine.respond(UserTurn("cli", "jarvis-production-0", "你在干嘛"))

    assert reply.text == "在等您开口，先生。"
    assert [(message.role, message.content) for message in provider.calls[0]] == [
        ("system", JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS),
        ("user", "你在干嘛"),
    ]
    serialized = "\n".join(message.content for message in provider.calls[0])
    assert "should_not_enter_production_prompt" not in serialized
