import json
from pathlib import Path

import pytest

from brain.model_reasoner import ChatMessage
from conversation.claim_guard import ActionClaimGuard
from conversation.models import UserTurn
from conversation.natural import NaturalConversationEngine
from conversation.task_router import ConversationTaskRouter, TaskIntent, TaskIntentResolver
from conversation.task_store import ConversationTaskStore
from memory.store import MemoryStore


class Provider:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.messages = []

    def complete(self, messages):
        self.messages.append(messages)
        return self.outputs.pop(0)


def setup(tmp_path, outputs, **services):
    provider = Provider(outputs)
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    tasks = ConversationTaskStore(tmp_path / "tasks.db")
    router = ConversationTaskRouter(engineering_bridge=None, tasks=tasks, **services)
    engine.response_guard = ActionClaimGuard(provider, router.reply_evidence)
    return provider, engine, tasks, router


def test_chat_stays_natural_and_never_creates_task(tmp_path: Path):
    provider, engine, tasks, router = setup(tmp_path, [json.dumps({"kind": "chat", "goal": ""}), "在呢，今天怎么样？", '{"supported":true}'])
    reply = router.respond(engine, UserTurn("qq", "private:42", "晚上好", actor_id="42"), source_ref="chat")
    assert reply.text == "在呢，今天怎么样？"
    assert tasks.recent() == []
    assert len(engine.memory.recent_events(10)) == 2


def test_router_passes_complete_history_as_data_for_current_user_assent(tmp_path: Path):
    provider, engine, tasks, router = setup(tmp_path, [json.dumps({"kind": "engineering", "goal": "Implement the agreed behavior", "current_user_requests_execution": True,
        "effects": ["maintain_project"], "constraints": ["private scope only"], "acceptance_criteria": ["group requests stay rejected"]})])
    engine.memory.remember_event("conversation.user", "只允许私人范围，群聊仍拒绝执行", context={"channel":"qq", "conversation_id":"private:42", "actor_id":"42", "scope":"private"}, importance=1)
    reply = router.respond(engine, UserTurn("qq", "private:42", "就按这个做", actor_id="42"), source_ref="mandate")
    record = tasks.get("mandate")
    assert record["intent"]["constraints"] == ["private scope only"]
    assert record["status"] == "blocked"  # This fixture deliberately has no engineering backend.
    assert "未启用" in reply.text
    payload = json.loads(provider.messages[0][-1].content)
    assert payload["history"][0]["text"] == "只允许私人范围，群聊仍拒绝执行"


def test_no_execution_intent_without_current_user_request():
    with pytest.raises(ValueError, match="current user intent"):
        TaskIntent.parse({"kind": "github", "goal": "assistant proposed writing", "action": "write_file"})


def test_github_result_is_source_bound_and_repeated_request_reuses_receipt(tmp_path: Path):
    class GitHub:
        def __init__(self): self.calls = []
        def catalog(self): return [{"name": "list_prs"}]
        def handle(self, action, arguments, source_ref, conversation_id):
            self.calls.append((action, arguments, source_ref, conversation_id))
            return {"status": "ok", "data": {"number": 81, "state": "open"}}
    github = GitHub()
    _, engine, tasks, router = setup(tmp_path, [json.dumps({"kind": "github", "goal": "inspect PRs", "action": "list_prs", "arguments": {}, "current_user_requests_execution": True})], github_service=github)
    turn = UserTurn("qq", "private:42", "看看PR", actor_id="42")
    first = router.respond(engine, turn, source_ref="github")
    assert router.respond(engine, turn, source_ref="github") == first
    assert len(github.calls) == 1
    assert tasks.get("github")["evidence"]["data"]["number"] == 81
    with pytest.raises(ValueError, match="principal"):
        router.respond(engine, UserTurn("qq", "private:42", "看看PR", actor_id="other"), source_ref="github")


def test_unsupported_action_claim_is_corrected_before_memory_write(tmp_path: Path):
    _, engine, tasks, router = setup(tmp_path, [json.dumps({"kind":"chat", "goal":""}), "我已经开始执行，任务在后台处理了。", '{"supported":false}'])
    reply = router.respond(engine, UserTurn("qq", "private:42", "继续吧", actor_id="42"), source_ref="no-action")
    assert "不能说已经开始或完成" in reply.text
    assert all("任务在后台处理了" not in event.content for event in engine.memory.recent_events(10))
    assert tasks.recent() == []


def test_shared_turn_never_calls_private_router_or_growth(tmp_path: Path):
    provider, engine, tasks, router = setup(tmp_path, ["共享群聊不执行工程任务。", '{"supported":true}'])
    reply = router.respond(engine, UserTurn("qq", "group:1", "修改项目", actor_id="42", scope="shared"), source_ref="group")
    assert "共享群聊" in reply.text
    assert len(provider.messages) == 2
    assert tasks.recent() == []


@pytest.mark.parametrize("unsupported", ["代码修好了，测试也通过了。", "我开始处理了。", "I fixed the bug and all tests passed."])
def test_all_action_claim_paraphrases_are_checked(tmp_path: Path, unsupported: str):
    provider = Provider(['{"supported":false}'])
    guarded = ActionClaimGuard(provider)(UserTurn("qq", "private:42", "怎么说"), unsupported, "none")
    assert guarded != unsupported
    assert len(provider.messages) == 1


def test_action_guard_json_mode_instructions_satisfy_provider_contract():
    from brain.providers.openai_compatible import OpenAICompatibleProvider
    from brain.providers.observed import ObservedChatProvider
    def transport(request, timeout):
        payload = json.loads(request.data)
        assert payload["response_format"] == {"type":"json_object"}
        assert any("json" in message["content"].lower() for message in payload["messages"])
        return json.dumps({"choices":[{"message":{"content":'{"supported":true}'}}]}).encode()
    provider = OpenAICompatibleProvider(base_url="https://example.invalid/v1", model="contract", transport=transport)
    reply = "晚上好，我在。"
    assert ActionClaimGuard(provider)(UserTurn("qq", "private:42", "晚上好"), reply) == reply


def test_old_source_keeps_its_result_when_session_moves_on(tmp_path: Path):
    from engineering.bindings import EngineeringConversationBindingStore
    from engineering.session import EngineeringSessionStore, EngineeringSessionState, EngineeringTurn, EngineeringAuthority, EngineeringResult
    from conversation.engineering_bridge import ConversationEngineeringBridge
    from conversation.task_pump import ConversationTaskPump
    from core.delivery import DeliveryOutbox
    _, engine, tasks, _ = setup(tmp_path, [])
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bridge = ConversationEngineeringBridge(sessions, EngineeringConversationBindingStore(tmp_path / "bindings.json"), repository=tmp_path)
    router = ConversationTaskRouter(engineering_bridge=bridge, tasks=tasks)
    session = sessions.create(EngineeringSessionState.create(project_id="hikari", repository=tmp_path, authority_ceiling=EngineeringAuthority.read_only()))
    first = UserTurn("qq", "private:42", "first", actor_id="42")
    first_turn = EngineeringTurn.create(intent="first", authority=EngineeringAuthority.read_only(), effect="inspect_project", source_request_id="a")
    sessions.enqueue_turn(session.session_id, first_turn)
    tasks.create("a", first, {"kind":"engineering", "goal":"first"})
    tasks.finish("a", status="accepted", evidence={"session_id":session.session_id, "turn_id":first_turn.turn_id}, reply="accepted")
    sessions.save_result(session.session_id, EngineeringResult(turn_id=first_turn.turn_id, status="completed", message="first actual result"))
    second_turn = EngineeringTurn.create(intent="second", authority=EngineeringAuthority.read_only(), effect="inspect_project", source_request_id="b")
    sessions.enqueue_turn(session.session_id, second_turn)
    pump = ConversationTaskPump(router, DeliveryOutbox(tmp_path / "outbox.db"))
    pump()
    assert tasks.get("a")["status"] == "completed"
    assert tasks.get("a")["evidence"]["summary"] == "first actual result"
    second = UserTurn("qq", "private:42", "second", actor_id="42")
    tasks.create("b", second, {"kind":"engineering"})
    tasks.finish("b", status="accepted", evidence={"session_id":session.session_id, "turn_id":second_turn.turn_id}, reply="accepted")
    sessions.update_runtime(session.session_id, status="completed", latest_summary="unproven")
    assert router._engineering_evidence("b", second)["status"] == "unknown"


def test_pending_tasks_and_private_evidence_do_not_fall_out_of_global_limits(tmp_path: Path):
    store = ConversationTaskStore(tmp_path / "tasks.db")
    first = UserTurn("qq", "private:1", "long task", actor_id="1")
    store.create("old-active", first, {"kind":"engineering"})
    for i in range(205):
        store.create(str(i), UserTurn("qq", "private:2", str(i), actor_id="2"), {"kind":"github"})
    assert store.unfinished()[0]["source_ref"] == "old-active"
    assert store.for_turn(first)[0]["source_ref"] == "old-active"
