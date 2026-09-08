from pathlib import Path

from core.delivery import DeliveryOutbox, DeliveryRequest
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


def test_existing_legacy_delivery_is_not_rewritten_by_new_message_format(tmp_path: Path) -> None:
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    authority = EngineeringAuthority.read_only()

    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=authority,
        session_id="legacy-session",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="旧版本的 README 检查",
        authority=authority,
    )
    sessions.enqueue_turn(state.session_id, turn)
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=turn.turn_id,
            status="failed",
            message="old terminal failure",
        ),
    )
    bindings.bind(
        EngineeringConversationBinding(
            session_id=state.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    delivery_id = f"engineering:{state.session_id}:{turn.turn_id}"
    legacy_text = "工程会话没有完成。\n\nold terminal failure"
    outbox.enqueue(
        DeliveryRequest(
            delivery_id=delivery_id,
            channel="qq",
            recipient="42",
            text=legacy_text,
            source="engineering",
        )
    )

    delivery = EngineeringCompletionDelivery(sessions, bindings, outbox)
    assert delivery.pump() == 1
    assert delivery.pump() == 1

    record = outbox.get(delivery_id)
    assert record is not None
    assert record.request.text == legacy_text
    assert "任务：" not in record.request.text
