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
from conversation.action_bridge import (
    ConversationForgeBridge,
    PendingForgeAction,
    PendingForgeActionStore,
    _looks_like_engineering_intent,
)
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


class FakePlanner:
    def __init__(self, proposal: ActionProposal | None = None, error: Exception | None = None) -> None:
        self.proposal = proposal
        self.error = error
        self.calls = 0

    def plan(self, event, decision):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.proposal


class RecordingForgeAdapter:
    action_name = "run_forge_task"

    def __init__(self, *, success: bool = True, error: Exception | None = None) -> None:
        self.success = success
        self.error = error
        self.calls = 0

    def execute(self, action) -> ExecutionResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return ExecutionResult(
            action_name=self.action_name,
            success=self.success,
            summary="fake Forge run finished",
        )


def _proposal() -> ActionProposal:
    return ActionProposal(
        action_name="run_forge_task",
        arguments={
            "project_id": "hikari",
            "goal": "Add one bounded test marker",
            "constraints": ["keep scope narrow"],
            "acceptance": ["tests pass"],
        },
        effect="modify Hikari in a Forge engineering worktree",
        reason="the user explicitly requested a code change",
        confidence=0.95,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )


def _engine(tmp_path: Path, replies: list[str] | None = None) -> tuple[ConversationEngine, FakeProvider]:
    provider = FakeProvider(list(replies or ["普通回复"]))
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    return engine, provider


def _bridge(
    tmp_path: Path,
    planner: FakePlanner,
    adapter: RecordingForgeAdapter,
    *,
    now: list[float] | None = None,
    ttl: float = 600.0,
) -> ConversationForgeBridge:
    clock_state = now or [1000.0]
    return ConversationForgeBridge(
        planner,  # type: ignore[arg-type]
        ActionAuthorizationPolicy(),
        ActionExecutor([adapter]),
        PendingForgeActionStore(tmp_path / "pending.json"),
        confirmation_ttl_seconds=ttl,
        clock=lambda: clock_state[0],
    )


def test_non_engineering_chat_falls_through_without_planning(tmp_path: Path):
    engine, provider = _engine(tmp_path, ["在呢。"])
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(engine, UserTurn("qq", "private:7", "hikari"))

    assert reply.text == "在呢。"
    assert planner.calls == 0
    assert adapter.calls == 0
    assert len(provider.calls) == 1


def test_engineering_request_creates_confirmation_without_execution(tmp_path: Path):
    engine, provider = _engine(tmp_path)
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)
    turn = UserTurn("qq", "private:7", "让 Forge 给 Hikari 加一个小功能")

    reply = bridge.respond(engine, turn, source_ref="qq:1")

    assert "回复“确认”开始" in reply.text
    assert "Add one bounded test marker" in reply.text
    assert planner.calls == 1
    assert adapter.calls == 0
    assert provider.calls == []
    pending = bridge.pending_store.get("qq", "private:7")
    assert pending is not None
    assert pending.proposal.action_name == "run_forge_task"


def test_same_conversation_confirmation_executes_exactly_once(tmp_path: Path):
    engine, provider = _engine(tmp_path, ["没有待执行操作。"])
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    first = bridge.respond(engine, UserTurn("qq", "private:7", "确认"))
    second = bridge.respond(engine, UserTurn("qq", "private:7", "确认"))

    assert "Forge 已执行完成" in first.text
    assert second.text == "没有待执行操作。"
    assert adapter.calls == 1
    assert bridge.pending_store.get("qq", "private:7") is None
    assert len(provider.calls) == 1


def test_cancel_clears_pending_without_execution(tmp_path: Path):
    engine, _provider = _engine(tmp_path)
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    reply = bridge.respond(engine, UserTurn("qq", "private:7", "取消"))

    assert reply.text == "取消了。这个 Forge 任务没有执行。"
    assert adapter.calls == 0
    assert bridge.pending_store.get("qq", "private:7") is None


def test_confirmation_is_scoped_to_same_conversation(tmp_path: Path):
    engine, provider = _engine(tmp_path, ["这里没有待确认操作。"])
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    reply = bridge.respond(engine, UserTurn("qq", "private:8", "确认"))

    assert reply.text == "这里没有待确认操作。"
    assert adapter.calls == 0
    assert bridge.pending_store.get("qq", "private:7") is not None
    assert len(provider.calls) == 1


def test_stale_confirmation_expires_without_execution(tmp_path: Path):
    now = [1000.0]
    engine, _provider = _engine(tmp_path)
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter, now=now, ttl=60.0)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    now[0] = 1061.0
    reply = bridge.respond(engine, UserTurn("qq", "private:7", "确认"))

    assert "已经过期" in reply.text
    assert adapter.calls == 0
    assert bridge.pending_store.get("qq", "private:7") is None


def test_unrelated_turn_while_pending_does_not_execute(tmp_path: Path):
    engine, provider = _engine(tmp_path)
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    reply = bridge.respond(engine, UserTurn("qq", "private:7", "等一下我先想想"))

    assert "还在等" in reply.text
    assert adapter.calls == 0
    assert provider.calls == []


def test_planner_failure_falls_back_to_normal_conversation(tmp_path: Path):
    engine, provider = _engine(tmp_path, ["先不动代码。"])
    planner = FakePlanner(error=RuntimeError("planner boom"))
    adapter = RecordingForgeAdapter()
    bridge = _bridge(tmp_path, planner, adapter)

    reply = bridge.respond(engine, UserTurn("qq", "private:7", "帮我修改 Hikari 代码"))

    assert reply.text == "先不动代码。"
    assert planner.calls == 1
    assert adapter.calls == 0
    assert len(provider.calls) == 1


def test_executor_failure_consumes_confirmation_and_does_not_retry(tmp_path: Path):
    engine, provider = _engine(tmp_path, ["没有第二次执行。"])
    planner = FakePlanner(_proposal())
    adapter = RecordingForgeAdapter(error=RuntimeError("executor boom"))
    bridge = _bridge(tmp_path, planner, adapter)

    bridge.respond(engine, UserTurn("qq", "private:7", "让 Forge 修改 Hikari 代码"))
    first = bridge.respond(engine, UserTurn("qq", "private:7", "确认"))
    second = bridge.respond(engine, UserTurn("qq", "private:7", "确认"))

    assert "不会自动重试" in first.text
    assert second.text == "没有第二次执行。"
    assert adapter.calls == 1
    assert len(provider.calls) == 1


def test_pending_state_survives_bridge_reconstruction(tmp_path: Path):
    store = PendingForgeActionStore(tmp_path / "pending.json")
    store.put(
        PendingForgeAction(
            channel="qq",
            conversation_id="private:7",
            proposal=_proposal(),
            created_at=123.0,
        )
    )

    restored = PendingForgeActionStore(tmp_path / "pending.json").get("qq", "private:7")

    assert restored is not None
    assert restored.created_at == 123.0
    assert restored.proposal.arguments["project_id"] == "hikari"


def test_engineering_intent_filter_is_narrow_but_natural():
    assert _looks_like_engineering_intent("让 Forge 跑一下") is True
    assert _looks_like_engineering_intent("帮我修改 Hikari 的代码") is True
    assert _looks_like_engineering_intent("给项目新增一个模块") is True
    assert _looks_like_engineering_intent("今天天气不错") is False
