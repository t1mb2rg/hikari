from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.jarvis_openjarvis import JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS
from conversation.models import UserTurn
from conversation.natural_context import build_resident_natural_context
from conversation.whiteboard import WhiteboardConversationEngine
from engineering.session import (
    EngineeringAuthority,
    EngineeringSessionState,
    EngineeringSessionStore,
)
from memory.store import MemoryStore


class RecordingProvider:
    def __init__(self) -> None:
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages) -> str:
        self.calls.append(list(messages))
        return "知道了，先生。"


def test_resident_natural_context_reports_active_engineering_task(tmp_path: Path):
    store = EngineeringSessionStore(tmp_path / "engineering")
    state = store.create(
        EngineeringSessionState.create(
            project_id="hikari",
            repository=tmp_path,
            authority_ceiling=EngineeringAuthority.read_only(),
        )
    )
    store.update_runtime(
        state.session_id,
        status="running",
        latest_summary="正在执行目标测试",
    )

    context = build_resident_natural_context(
        state_dir=tmp_path,
        qq_enabled=True,
        engineering_enabled=True,
    )

    assert "Hikari Resident 当前正在运行" in context
    assert "QQ Bridge 当前由 Resident 托管" in context
    assert "Engineering 任务处于 running 状态：正在执行目标测试" in context


def test_dynamic_natural_context_sits_next_to_current_turn(tmp_path: Path):
    provider = RecordingProvider()
    engine = WhiteboardConversationEngine(
        provider,
        MemoryStore(tmp_path / "memory.db"),
        system_instructions=JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS,
        relevant_context_provider=lambda: "当前可用的系统事实：\n- 当前没有后台工程任务。",
        relevant_context_placement="current_turn",
    )

    engine.respond(UserTurn("qq", "private:7", "你在干嘛"))

    current = provider.calls[0][-1]
    assert current.role == "user"
    assert "当前没有后台工程任务" in current.content
    assert "【现在对你说】\n你在干嘛" in current.content
