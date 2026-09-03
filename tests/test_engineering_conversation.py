from dataclasses import replace
from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import (
    ConversationEngineeringBridge,
    engineering_requirements_for_intent,
    engineering_session_matches_repository_head,
    looks_like_engineering_status_query,
    looks_like_read_only_engineering_intent,
)
from conversation.models import UserTurn
from core.delivery import DeliveryOutbox
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.delivery import EngineeringCompletionDelivery
from engineering.session import (
    EngineeringAuthority,
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from memory.store import MemoryStore


class _Provider:
    def complete(self, messages):
        return "unused"


class _ExplodingProvider:
    def complete(self, messages):
        raise AssertionError("deterministic engineering status query must not call the model")


def test_engineering_intent_gate_distinguishes_read_and_maintain_tasks():
    assert looks_like_read_only_engineering_intent("先去看看 README，告诉我项目现在是什么状态")
    assert looks_like_read_only_engineering_intent("再分析一下 memory 模块")
    assert not looks_like_read_only_engineering_intent("帮我修改 memory 模块")
    maintain = engineering_requirements_for_intent("帮我修改 memory 模块并修好测试")
    assert maintain is not None
    assert "engineering.repository.write" in maintain
    assert "engineering.tests.run" in maintain
    assert "engineering.git.commit" in maintain
    assert engineering_requirements_for_intent("今天晚上吃什么") is None
    assert looks_like_engineering_status_query("现在engineering任务是什么状态？")
    assert looks_like_engineering_status_query("Engineering Worker 进度怎么样了")
    assert not looks_like_engineering_status_query("今天状态怎么样")


def test_conversation_binding_round_trips(tmp_path: Path):
    store = EngineeringConversationBindingStore(tmp_path / "bindings.json")
    binding = EngineeringConversationBinding(
        session_id="session-1",
        channel="qq",
        conversation_id="private:42",
    )

    store.bind(binding)

    assert store.get("session-1") == binding
    assert store.for_conversation("qq", "private:42") == binding


def test_terminal_engineering_result_reuses_hikari_delivery_outbox(tmp_path: Path):
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    authority = EngineeringAuthority.read_only()
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=authority,
        session_id="session-1",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="看看 README",
        authority=authority,
    )
    sessions.enqueue_turn(state.session_id, turn)
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=turn.turn_id,
            status="completed",
            message="README 表明项目正在 M7。",
            backend_session_id="claude-session-1",
        ),
    )

    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    bindings.bind(
        EngineeringConversationBinding(
            session_id=state.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    delivery = EngineeringCompletionDelivery(sessions, bindings, outbox)

    delivery.pump()
    delivery.pump()

    record = outbox.get(f"engineering:{state.session_id}:{turn.turn_id}")
    assert record is not None
    assert record.state == "pending"
    assert record.request.channel == "qq"
    assert record.request.recipient == "42"
    assert "工程任务结果：已完成" in record.request.text
    assert "任务：看看 README" in record.request.text
    assert "README 表明项目正在 M7" in record.request.text
    assert record.request.source == "engineering"


def test_historical_terminal_delivery_is_labeled_as_old_task(tmp_path: Path):
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    authority = EngineeringAuthority.read_only()

    old = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=authority,
        session_id="old-session",
    )
    sessions.create(old)
    old_turn = EngineeringTurn.create(intent="旧任务：检查模型配置", authority=authority)
    sessions.enqueue_turn(old.session_id, old_turn)
    sessions.save_result(
        old.session_id,
        EngineeringResult(
            turn_id=old_turn.turn_id,
            status="failed",
            message="unrecognized model",
        ),
    )
    bindings.bind(
        EngineeringConversationBinding(
            session_id=old.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    current = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=authority,
        session_id="current-session",
    )
    sessions.create(current)
    bindings.bind(
        EngineeringConversationBinding(
            session_id=current.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    EngineeringCompletionDelivery(sessions, bindings, outbox).pump()
    record = outbox.get(f"engineering:{old.session_id}:{old_turn.turn_id}")
    assert record is not None
    assert "补发旧工程任务结果：没有完成" in record.request.text
    assert "任务：旧任务：检查模型配置" in record.request.text


def test_engineering_status_query_reads_failed_terminal_result_without_model(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    authority = EngineeringAuthority.read_only()
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=authority,
        session_id="failed-session",
    )
    sessions.create(state)
    task = EngineeringTurn.create(intent="修改 README", authority=authority)
    sessions.enqueue_turn(state.session_id, task)
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=task.turn_id,
            status="failed",
            message="Engineering backend 执行失败：unrecognized_model",
        ),
    )
    bindings.bind(
        EngineeringConversationBinding(
            session_id=state.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    bridge = ConversationEngineeringBridge(sessions, bindings, repository=repository)
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "现在engineering任务是什么状态？"),
    )

    assert "`failed`" in reply.text
    assert "unrecognized_model" in reply.text
    assert "已完成" not in reply.text


def test_engineering_status_query_reports_machine_written_running_phase(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    authority = EngineeringAuthority.read_only()
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=authority,
        session_id="running-session",
    )
    sessions.create(state)
    task = EngineeringTurn.create(intent="检查 README", authority=authority)
    sessions.enqueue_turn(state.session_id, task)
    sessions.update_runtime(
        state.session_id,
        status="running",
        latest_summary="正在运行项目测试",
    )
    bindings.bind(
        EngineeringConversationBinding(
            session_id=state.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    bridge = ConversationEngineeringBridge(sessions, bindings, repository=repository)
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "Engineering Worker 进度怎么样了"),
    )

    assert "`running`" in reply.text
    assert "`testing`" in reply.text
    assert "正在运行项目测试" in reply.text


def test_engineering_session_baseline_match_is_explicit(tmp_path: Path):
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=EngineeringAuthority.read_only(),
    )
    assert engineering_session_matches_repository_head(state, "new-head") is True

    state = replace(state, baseline_commit="old-head")
    assert engineering_session_matches_repository_head(state, "old-head") is True
    assert engineering_session_matches_repository_head(state, "new-head") is False


def test_conversation_rotates_terminal_session_when_repository_head_advanced(
    tmp_path: Path,
    monkeypatch,
):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    authority = EngineeringAuthority.read_only()

    old = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=authority,
        session_id="old-session",
    )
    old = replace(
        old,
        status="completed",
        baseline_commit="old-head",
        workspace_path=str(tmp_path / "old-worktree"),
        workspace_branch="hikari/engineering/old-session",
    )
    sessions.create(old)
    bindings.bind(
        EngineeringConversationBinding(
            session_id=old.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    monkeypatch.setattr(
        "conversation.engineering_bridge.EngineeringWorkspace.source_head",
        lambda repository: "new-head",
    )

    bridge = ConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
    )
    engine = ConversationEngine(_Provider(), MemoryStore(tmp_path / "memory.db"))
    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "去看看 README 最近更新了什么"),
    )

    assert "只读工程会话" in reply.text
    rebound = bindings.for_conversation("qq", "private:42")
    assert rebound is not None
    assert rebound.session_id != "old-session"
    new_state = sessions.load(rebound.session_id)
    assert new_state.status == "pending"
    assert new_state.baseline_commit is None
    assert new_state.authority_ceiling.repository_write is True
    assert sessions.load("old-session").baseline_commit == "old-head"


def test_conversation_routes_project_write_without_per_action_approval(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    bridge = ConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
    )
    engine = ConversationEngine(_Provider(), MemoryStore(tmp_path / "memory.db"))

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "帮我修改 memory 模块，把这个 bug 修掉"),
    )

    assert "项目维护职责" in reply.text
    assert "修改、测试和提交" in reply.text
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    state = sessions.load(binding.session_id)
    assert state.status == "pending"
    assert state.authority_ceiling.repository_write is True
    assert state.authority_ceiling.run_tests is True
    assert state.current_turn_id is not None
    engineering_turn = sessions.load_turn(state.session_id, state.current_turn_id)
    assert engineering_turn.authority.repository_write is True
    assert engineering_turn.authority.run_tests is True
    assert engineering_turn.authority.network is False
    assert engineering_turn.authority.publish is False
