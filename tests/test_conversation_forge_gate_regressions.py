from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    ExecutionResult,
)
from conversation.action_bridge import (
    ConversationForgeBridge,
    PendingForgeActionStore,
    build_conversation_forge_bridge,
)
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from memory.store import MemoryStore


class _Provider:
    def complete(self, messages):
        return "unused"


class _Planner:
    def __init__(self, proposal: ActionProposal) -> None:
        self.proposal = proposal

    def plan(self, event, decision):
        return self.proposal


class _ForgeAdapter:
    action_name = "run_forge_task"

    def __init__(self) -> None:
        self.calls = 0

    def execute(self, action) -> ExecutionResult:
        self.calls += 1
        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary="gate regression Forge run finished",
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
        confidence=0.99,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )


def test_confirm_execution_alias_dispatches_pending_forge_once(tmp_path: Path):
    engine = ConversationEngine(_Provider(), MemoryStore(tmp_path / "memory.db"))  # type: ignore[arg-type]
    adapter = _ForgeAdapter()
    bridge = ConversationForgeBridge(
        _Planner(_proposal()),  # type: ignore[arg-type]
        ActionAuthorizationPolicy(),
        ActionExecutor([adapter]),
        PendingForgeActionStore(tmp_path / "pending.json"),
    )

    bridge.respond(engine, UserTurn("qq", "private:gate", "让 Forge 修改 Hikari 测试"))
    reply = bridge.respond(engine, UserTurn("qq", "private:gate", "确认执行"))

    assert "Forge 已执行完成" in reply.text
    assert adapter.calls == 1
    assert bridge.pending_store.get("qq", "private:gate") is None


def test_runtime_forge_verification_uses_resident_python(tmp_path: Path):
    bridge = build_conversation_forge_bridge(
        {},
        _Provider(),  # type: ignore[arg-type]
        repository=tmp_path,
        state_dir=tmp_path / "state",
    )

    adapter = bridge.executor._adapters["run_forge_task"]
    profile = adapter.registry.resolve("hikari")
    expected = subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q"])

    assert profile.verification == (expected,)
    assert sys.executable in expected
