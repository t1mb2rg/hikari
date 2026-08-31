from pathlib import Path

from conversation.engineering_bridge import looks_like_read_only_engineering_intent
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


def test_read_only_engineering_intent_gate_is_narrow():
    assert looks_like_read_only_engineering_intent("先去看看 README，告诉我项目现在是什么状态")
    assert looks_like_read_only_engineering_intent("再分析一下 memory 模块")
    assert not looks_like_read_only_engineering_intent("帮我修改 memory 模块")
    assert not looks_like_read_only_engineering_intent("今天晚上吃什么")


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
    assert "我看完了" in record.request.text
    assert "README 表明项目正在 M7" in record.request.text
    assert record.request.source == "engineering"
