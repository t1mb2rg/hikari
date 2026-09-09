"""Real task/Growth/Worker/operator/outbox integration with only model seams faked."""
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from capabilities import CapabilityError, CapabilityGrowth, RecipeRuntime
from capabilities.operator import CapabilityOperatorControls
from conversation.models import AssistantReply, UserTurn
from conversation.task_pump import ConversationTaskPump
from conversation.task_router import ConversationTaskRouter, TaskIntent
from conversation.task_store import ConversationTaskStore
from core.delivery import DeliveryOutbox
from dashboard.app import create_app
from dashboard.probes import DashboardProbeConfig
from engineering.backend import EngineeringAgentEvent, EngineeringAgentResult
from engineering.session import EngineeringSessionStore
from engineering.worker import EngineeringWorker


SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}},
          "required": ["text"], "additionalProperties": False}
CASES = [{"input": {"text": "hello"}, "expected": {"text": "HELLO"}},
         {"input": {"text": ""}, "expected": {"text": ""}}]
OWNER = UserTurn("qq", "private:123", "Learn a pure uppercase capability and apply it to hello.", actor_id="123")


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          encoding="utf-8", timeout=20).stdout.strip()


class Resolver:
    def __init__(self):
        self.seen = []
        self.intent = TaskIntent(kind="capability", goal="Uppercase original text", constraints=("pure only",),
            arguments={"capability_id": "private.uppercase_integration", "input_schema": SCHEMA,
                       "output_schema": SCHEMA, "acceptance_cases": CASES, "resume_input": CASES[0]["input"]})

    def resolve(self, engine, turn, **context):
        self.seen.append((turn, context))
        return self.intent


class Engine:
    def __init__(self):
        self.events = []
        self.memory = SimpleNamespace(remember_event=lambda *args, **kwargs: self.events.append((args, kwargs)))

    def respond(self, turn, **kwargs):
        return AssistantReply(turn.channel, turn.conversation_id, "ordinary scoped chat")


class Backend:
    def __init__(self, growth, *, extra_file=False):
        self.growth = growth
        self.calls = 0
        self.extra_file = extra_file

    def run(self, path, prompt):
        self.calls += 1
        request = self.growth.list_requests()[0]
        recipe = {"format": "hikari.recipe.v1", "capability_id": request["capability_id"], "version": 1,
            "owner": "hikari.private", "permissions": [], "input_schema": SCHEMA, "output_schema": SCHEMA,
            "steps": [{"id": "upper", "service": "text.upper", "args": {"text": {"ref": "input.text"}}}],
            "return": {"text": {"ref": "upper"}}}
        for case in CASES:
            assert RecipeRuntime().invoke(recipe, case["input"]) == case["expected"]
        folder = Path(path) / self.growth.candidate_directory(request)
        folder.mkdir(parents=True)
        (folder / "recipe.json").write_text(json.dumps(recipe), encoding="utf-8")
        (folder / "tests.json").write_text(json.dumps(CASES), encoding="utf-8")
        if self.extra_file:
            (Path(path) / "README.md").write_text("unauthorized extra candidate mutation", encoding="utf-8")
        return EngineeringAgentResult(0, "", "", "Wrote recipe and tests and executed acceptance examples",
            "synthetic-model", (EngineeringAgentEvent("validation", "Executed two recipe cases"),))


@pytest.fixture
def setup(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Integration Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("isolated fixture", encoding="utf-8")
    git(repository, "add", ".")
    git(repository, "commit", "-m", "synthetic baseline")
    state = tmp_path / "state"
    growth = CapabilityGrowth(state / "capability_growth.db", engineering_store=EngineeringSessionStore(state / "engineering"),
                              repository=repository, implementation_enabled=True)
    tasks = ConversationTaskStore(state / "conversation_tasks.db")
    resolver = Resolver()
    router = ConversationTaskRouter(engineering_bridge=None, tasks=tasks, growth=growth, resolver=resolver)
    operator = CapabilityOperatorControls(repository, state)
    router.growth_operator = operator
    outbox = DeliveryOutbox(state / "proactive_delivery.db")
    return SimpleNamespace(repository=repository, state=state, growth=growth, tasks=tasks, resolver=resolver,
                           router=router, operator=operator, outbox=outbox, engine=Engine())


def implement(setup, *, automatic=True, extra_file=False):
    setup.router.respond(setup.engine, OWNER, source_ref="original-wire-request")
    if automatic:
        setup.operator.save_growth_policy({"version": 1, "auto_activate_pure_recipes": True,
                                            "allowed_services": ["text.upper"]}, "absent")
    backend = Backend(setup.growth, extra_file=extra_file)
    outcome = EngineeringWorker(setup.growth.engineering_store, backend_factory=lambda *a: backend).run_once()
    assert outcome.status == "completed"
    return backend


def test_real_worker_policy_pump_resumes_private_request_once_after_restart(setup):
    source_head = git(setup.repository, "rev-parse", "HEAD")
    backend = implement(setup)
    pump = ConversationTaskPump(setup.router, setup.outbox)
    pump()
    request = setup.growth.list_requests()[0]
    assert request["status"] == "resumed" and request["result"] == {"text": "HELLO"}
    assert request["evidence"]["operator_ref"].startswith("dashboard:pure-policy:")
    task = setup.tasks.get("original-wire-request")
    assert task["status"] == "resumed" and task["evidence"]["source_ref"] == "original-wire-request"
    messages = setup.outbox.pending()
    assert len(messages) == 1 and messages[0].request.recipient == "123"
    assert '"text": "HELLO"' in messages[0].request.text
    assert messages[0].request.delivery_id.endswith(":resumed")
    rebuilt_growth = CapabilityGrowth(setup.growth.path, engineering_store=EngineeringSessionStore(setup.state / "engineering"),
                                      repository=setup.repository, implementation_enabled=True)
    rebuilt = ConversationTaskRouter(engineering_bridge=None, tasks=ConversationTaskStore(setup.tasks.path),
                                    growth=rebuilt_growth, resolver=Resolver())
    rebuilt.growth_operator = CapabilityOperatorControls(setup.repository, setup.state)
    ConversationTaskPump(rebuilt, DeliveryOutbox(setup.outbox.path))()
    assert len(setup.outbox.pending()) == 1 and backend.calls == 1
    assert git(setup.repository, "rev-parse", "HEAD") == source_head
    assert not git(setup.repository, "status", "--porcelain")


def test_disabled_policy_reports_candidate_once_then_manual_activation_resumes(setup):
    implement(setup, automatic=False)
    pump = ConversationTaskPump(setup.router, setup.outbox)
    pump()
    request = setup.growth.list_requests()[0]
    assert request["status"] == "candidate_tested" and not request["evidence"]["live"]
    pump()
    assert len(setup.outbox.pending()) == 1
    setup.operator.operator_activate_capability(request["request_id"], request["candidate_digest"])
    pump()
    assert setup.growth.get(request["request_id"])["status"] == "resumed"
    assert {item.request.delivery_id.rsplit(":", 1)[-1] for item in setup.outbox.pending()} == {"candidate_tested", "resumed"}


def test_learning_without_original_input_reports_activation_and_stops_pending(setup):
    setup.resolver.intent = replace(setup.resolver.intent,
        arguments={key: value for key, value in setup.resolver.intent.arguments.items() if key != "resume_input"})
    implement(setup)
    pump = ConversationTaskPump(setup.router, setup.outbox)
    pump()
    assert setup.growth.list_requests()[0]["status"] == "active"
    assert len(setup.outbox.pending()) == 1, "learn-only requests still need a capability-ready completion"
    assert setup.tasks.unfinished() == [], "an activated learn-only task must not remain pending forever"
    pump()
    assert len(setup.outbox.pending()) == 1


def test_router_context_and_invocation_never_share_another_owner_candidate(setup):
    implement(setup)
    ConversationTaskPump(setup.router, setup.outbox)()
    setup.resolver.intent = TaskIntent("chat", "chat")
    stranger = replace(OWNER, actor_id="999", conversation_id="private:999", text="What can you do?")
    setup.router.respond(setup.engine, stranger, source_ref="stranger-wire")
    context = setup.resolver.seen[-1][1]["growth_description"]
    assert context["requests"] == [] and context["active_interfaces"] == []
    seen = len(setup.resolver.seen)
    setup.router.respond(setup.engine, replace(stranger, scope="shared"), source_ref="shared-wire")
    assert len(setup.resolver.seen) == seen
    with pytest.raises(CapabilityError):
        setup.growth.invoke("private.uppercase_integration", {"text": "secret"}, turn=stranger)


def test_completed_worker_outside_candidate_boundary_is_not_activated(setup):
    implement(setup, extra_file=True)
    ConversationTaskPump(setup.router, setup.outbox)()
    request = setup.growth.list_requests()[0]
    assert request["status"] == "failed" and "outside" in request["evidence"]["reason"]
    assert setup.operator.growth_snapshot()["active_interfaces"] == []
    assert len(setup.outbox.pending()) == 1 and setup.outbox.pending()[0].request.delivery_id.endswith(":failed")


def test_dashboard_operator_gets_are_read_only_and_writes_require_origin_boundary(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    state = tmp_path / "state"
    client = TestClient(create_app(DashboardProbeConfig(repository, state)), base_url="http://127.0.0.1")
    for endpoint in ("github-policy", "growth-policy", "capabilities"):
        assert client.get("/api/operator/" + endpoint).status_code == 200
    assert not state.exists()
    payload = {"revision": "absent", "document": {"version": 1,
        "auto_activate_pure_recipes": True, "allowed_services": ["text.upper"]}}
    assert client.put("/api/operator/growth-policy", json=payload).status_code == 403
    assert client.put("/api/operator/growth-policy", json=payload,
        headers={"X-Hikari-Action": "dashboard", "Origin": "https://external.invalid"}).status_code == 403
    assert not state.exists()
    assert client.put("/api/operator/growth-policy", json=payload,
                      headers={"X-Hikari-Action": "dashboard"}).status_code == 200
    assert not (state / "capability_growth.db").exists()


def test_resumed_result_delivery_recovers_if_outbox_enqueue_fails(setup, monkeypatch):
    implement(setup)
    real_enqueue = setup.outbox.enqueue
    monkeypatch.setattr(setup.outbox, "enqueue", lambda *a: (_ for _ in ()).throw(OSError("simulated unavailable outbox")))
    with pytest.raises(OSError):
        ConversationTaskPump(setup.router, setup.outbox)()
    assert setup.growth.list_requests()[0]["status"] == "resumed"
    monkeypatch.setattr(setup.outbox, "enqueue", real_enqueue)
    ConversationTaskPump(setup.router, setup.outbox)()
    assert len(setup.outbox.pending()) == 1, "terminal task update must not orphan a durable resumed result"


def test_reply_claim_evidence_never_exposes_a_different_principal_source_ref(setup):
    implement(setup)
    stranger = replace(OWNER, actor_id="999", conversation_id="private:999")
    evidence = setup.router.reply_evidence(stranger, "original-wire-request")
    assert evidence["current"] is None and evidence["historical"] == []


def test_another_auto_policy_candidate_error_does_not_starve_active_result_delivery(setup, monkeypatch):
    implement(setup, automatic=False)
    setup.growth.advance_all()
    request = setup.growth.list_requests()[0]
    setup.operator.operator_activate_capability(request["request_id"], request["candidate_digest"])
    monkeypatch.setattr(setup.operator, "auto_activate_tested_capabilities", lambda: {
        "enabled": True, "activated": [], "skipped": [],
        "errors": [{"request_id": "different-candidate", "error": "malformed candidate evidence"}],
    })
    ConversationTaskPump(setup.router, setup.outbox)()
    assert setup.growth.get(request["request_id"])["status"] == "resumed"
    assert len(setup.outbox.pending()) == 1
