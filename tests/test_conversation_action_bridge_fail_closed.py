from __future__ import annotations

from pathlib import Path

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    ExecutionResult,
)
from brain.model_reasoner import ChatMessage
from conversation.action_bridge import ConversationForgeBridge, PendingForgeActionStore
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, reply: str = "ordinary fallback must not run") -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.reply


class SequencedPlanner:
    def __init__(self, outcomes: list[ActionProposal | None | BaseException]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def plan(self, event, decision):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class RecordingForgeAdapter:
    action_name = "run_forge_task"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, action) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary="fake Forge run finished",
        )


def _proposal() -> ActionProposal:
    return ActionProposal(
        action_name="run_forge_task",
        arguments={
            "project_id": "hikari",
            "goal": "Add one bounded test",
            "constraints": ["tests only"],
            "acceptance": ["tests pass"],
        },
        effect="modify tests in a Forge worktree",
        reason="explicit engineering request",
        confidence=0.95,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )


def _bridge(
    tmp_path: Path,
    planner: SequencedPlanner,
    adapter: RecordingForgeAdapter,
) -> tuple[ConversationForgeBridge, ConversationEngine, FakeProvider]:
    provider = FakeProvider()
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    bridge = ConversationForgeBridge(
        planner,  # type: ignore[arg-type]
        ActionAuthorizationPolicy(),
        ActionExecutor([adapter]),
        PendingForgeActionStore(tmp_path / "pending.json"),
    )
    return bridge, engine, provider


def test_engineering_planner_failure_fails_closed_without_normal_chat(tmp_path: Path):
    planner = SequencedPlanner([RuntimeError("planner boom")])
    adapter = RecordingForgeAdapter()
    bridge, engine, provider = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:7", "让 Forge 给 Hikari 补一个测试"),
    )

    assert "没有启动 Forge" in reply.text
    assert "没有执行任何改动" in reply.text
    assert planner.calls == 1
    assert adapter.calls == 0
    assert provider.calls == []


def test_planner_timeout_retries_once_then_creates_confirmation(tmp_path: Path):
    planner = SequencedPlanner([TimeoutError(), _proposal()])
    adapter = RecordingForgeAdapter()
    bridge, engine, provider = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:7", "让 Forge 给 Hikari 补一个测试"),
    )

    assert "要先经过你确认" in reply.text
    assert planner.calls == 2
    assert adapter.calls == 0
    assert provider.calls == []
    assert bridge.pending_store.get("qq", "private:7") is not None


def test_second_planner_timeout_fails_closed(tmp_path: Path):
    planner = SequencedPlanner([TimeoutError(), TimeoutError()])
    adapter = RecordingForgeAdapter()
    bridge, engine, provider = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:7", "让 Forge 给 Hikari 补一个测试"),
    )

    assert "Forge 规划没有完成" in reply.text
    assert planner.calls == 2
    assert adapter.calls == 0
    assert provider.calls == []


def test_planner_none_fails_closed_without_normal_chat(tmp_path: Path):
    planner = SequencedPlanner([None])
    adapter = RecordingForgeAdapter()
    bridge, engine, provider = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:7", "帮我修改 Hikari 的代码"),
    )

    assert "没有生成可执行的 Forge 提案" in reply.text
    assert planner.calls == 1
    assert adapter.calls == 0
    assert provider.calls == []
