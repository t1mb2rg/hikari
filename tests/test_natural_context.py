from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from brain.model_reasoner import ChatMessage
from conversation.jarvis_openjarvis import JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS
from conversation.models import UserTurn
from conversation.natural_context import (
    add_user_model_context,
    build_resident_natural_context,
)
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


def test_resident_natural_context_reports_runtime_and_current_project(tmp_path: Path):
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
    (tmp_path / "CURRENT.md").write_text(
        "# Current\n\n- 当前正在推进 Natural Context。\n",
        encoding="utf-8",
    )

    context = build_resident_natural_context(
        state_dir=tmp_path,
        qq_enabled=True,
        engineering_enabled=True,
        repository=tmp_path,
    )

    assert "Hikari Resident 当前正在运行" in context
    assert "QQ Bridge 当前由 Resident 托管" in context
    assert "Engineering 任务处于 running 状态：正在执行目标测试" in context
    assert "当前正在推进 Natural Context" in context


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


def test_dynamic_context_recalls_related_prior_user_turn_only(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.remember_event(
        "conversation.user",
        "我感觉 M7 最近越来越大了，工程结构有点太重。",
        context={"channel": "qq", "conversation_id": "old", "role": "user"},
        importance=1.0,
    )
    memory.remember_event(
        "conversation.assistant",
        "M7 已经完全失控了。",
        context={"channel": "qq", "conversation_id": "old", "role": "assistant"},
        importance=1.0,
    )
    memory.remember_event(
        "conversation.user",
        "当前会话里也提到了 M7。",
        context={"channel": "qq", "conversation_id": "current", "role": "user"},
        importance=1.0,
    )

    provider = RecordingProvider()
    engine = WhiteboardConversationEngine(
        provider,
        memory,
        system_instructions=JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS,
        relevant_context_provider=lambda: "当前可用的系统事实：\n- Resident 正在运行。",
        relevant_context_placement="current_turn",
    )

    engine.respond(UserTurn("qq", "current", "我之前为什么觉得 M7 有问题来着"))

    current = provider.calls[0][-1].content
    assert "我感觉 M7 最近越来越大了，工程结构有点太重。" in current
    assert "M7 已经完全失控了。" not in current
    assert "当前会话里也提到了 M7。" not in current


def test_user_model_context_exposes_statements_without_internal_metadata():
    class FakeUserModelService:
        def retrieve(self, query: str, *, limit: int):
            assert "开发方式" in query
            assert limit == 2
            return [
                SimpleNamespace(
                    statement="用户偏好先做最小实现，再根据实际问题增加复杂度。",
                    confidence=0.97,
                    revision=4,
                )
            ]

    context = add_user_model_context(
        "当前事实：\n- Resident 正在运行。",
        user_model_service=FakeUserModelService(),
        query="我平时喜欢什么样的开发方式",
    )

    assert "用户偏好先做最小实现，再根据实际问题增加复杂度。" in context
    assert "0.97" not in context
    assert "revision" not in context
