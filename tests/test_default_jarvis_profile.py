from __future__ import annotations

import pytest

from conversation.cli import build_parser
from conversation.engine import ConversationEngine, INTERACTIVE_SYSTEM_INSTRUCTIONS
from conversation.jarvis_openjarvis import OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
from conversation.remote import PRIMARY_REMOTE_RELATIONSHIP_CONTEXT
from conversation.whiteboard import (
    WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    WhiteboardConversationEngine,
)
from resident.app import (
    _conversation_context_profile,
    _conversation_engine_configuration,
)


def test_cli_defaults_to_openjarvis_chinese_profile():
    args = build_parser().parse_args([])

    assert args.prompt_profile == "jarvis-openjarvis-zh"


def test_resident_conversation_defaults_to_jarvis_profile():
    assert _conversation_context_profile({}) == "jarvis"
    assert _conversation_context_profile({"HIKARI_CONVERSATION_CONTEXT_PROFILE": ""}) == "jarvis"


@pytest.mark.parametrize("profile", ["jarvis", "whiteboard", "grounded"])
def test_resident_conversation_accepts_explicit_profiles(profile: str):
    assert (
        _conversation_context_profile(
            {"HIKARI_CONVERSATION_CONTEXT_PROFILE": profile}
        )
        == profile
    )


def test_resident_conversation_rejects_unknown_profile():
    with pytest.raises(ValueError, match="grounded, whiteboard, or jarvis"):
        _conversation_context_profile(
            {"HIKARI_CONVERSATION_CONTEXT_PROFILE": "unknown"}
        )


def test_resident_jarvis_profile_is_minimal_openjarvis_chinese_path():
    engine_type, minimal_context, system_instructions, relationship_context = (
        _conversation_engine_configuration("jarvis")
    )

    assert engine_type is WhiteboardConversationEngine
    assert minimal_context is True
    assert system_instructions == OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
    assert relationship_context is None


def test_resident_whiteboard_profile_remains_available():
    engine_type, minimal_context, system_instructions, relationship_context = (
        _conversation_engine_configuration("whiteboard")
    )

    assert engine_type is WhiteboardConversationEngine
    assert minimal_context is True
    assert system_instructions == WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS
    assert relationship_context == PRIMARY_REMOTE_RELATIONSHIP_CONTEXT


def test_resident_grounded_profile_remains_available():
    engine_type, minimal_context, system_instructions, relationship_context = (
        _conversation_engine_configuration("grounded")
    )

    assert engine_type is ConversationEngine
    assert minimal_context is False
    assert system_instructions == INTERACTIVE_SYSTEM_INSTRUCTIONS
    assert relationship_context == PRIMARY_REMOTE_RELATIONSHIP_CONTEXT
