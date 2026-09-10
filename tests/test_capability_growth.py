from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import time

import pytest

from capabilities import CapabilityError, CapabilityGrowth, RecipeRuntime
from conversation.models import UserTurn
from engineering.session import EngineeringResult, EngineeringSessionStore
from engineering.workspace import EngineeringWorkspace


INPUT = {"type": "object", "properties": {"text": {"type": "string"}},
         "required": ["text"], "additionalProperties": False}
OUTPUT = {"type": "object", "properties": {"todos": {"type": "array", "items": {"type": "string"}},
          "count": {"type": "integer"}}, "required": ["todos", "count"], "additionalProperties": False}
CASES = [{"input": {"text": "TODO buy milk\nnotes\nTODO call home\nTODO buy milk"},
          "expected": {"todos": ["buy milk", "call home"], "count": 2}},
         {"input": {"text": "No outstanding items"}, "expected": {"todos": [], "count": 0}}]
TURN = UserTurn("test", "private-a", "Extract unique TODO items and count them from these notes.",
                actor_id="owner")


def recipe():
    return {"format": "hikari.recipe.v1", "capability_id": "private.todo_summary", "version": 1,
            "owner": "hikari.private", "permissions": [], "input_schema": INPUT, "output_schema": OUTPUT,
            "steps": [
                {"id": "lines", "service": "text.lines", "args": {"text": {"ref": "input.text"}}},
                {"id": "selected", "service": "lines.starting", "args": {"lines": {"ref": "lines"}, "text": "TODO "}},
                {"id": "trimmed", "service": "lines.strip_prefix", "args": {"lines": {"ref": "selected"}, "prefix": "TODO "}},
                {"id": "unique", "service": "lines.unique", "args": {"lines": {"ref": "trimmed"}}},
                {"id": "count", "service": "lines.count", "args": {"lines": {"ref": "unique"}}},
            ], "return": {"todos": {"ref": "unique"}, "count": {"ref": "count"}}}


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout.strip()


@pytest.fixture
def growth(tmp_path_factory):
    # Git worktrees and versioned candidate paths add several nested directories;
    # keep the fixture prefix short on native Windows (without changing OS policy).
    tmp_path = tmp_path_factory.mktemp("growth")
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    git(repository, "config", "core.autocrlf", "false")
    (repository / "README.md").write_text("Synthetic isolated capability fixture\n", encoding="utf-8")
    git(repository, "add", "README.md")
    git(repository, "commit", "-m", "fixture baseline")
    return CapabilityGrowth(tmp_path / "state" / "growth.db",
                            engineering_store=EngineeringSessionStore(tmp_path / "state" / "engineering"),
                            repository=repository, implementation_enabled=True)


def request(growth, **changes):
    args = dict(source_ref="wire:1", turn=TURN, capability_id="private.todo_summary", input_schema=INPUT,
                output_schema=OUTPUT, acceptance_cases=CASES, constraints=["preserve first-seen order"],
                resume_input=CASES[0]["input"])
    args.update(changes)
    return growth.request(**args)


def complete(growth, row, *, candidate=None, tests=None, extra_file=None, native=False):
    """Fake only the model seam; real Git worktree, commit, SQLite and interpreter."""
    row = growth.advance(row["request_id"])
    state = growth.engineering_store.load(row["session_id"])
    workspace = EngineeringWorkspace.create(growth.repository, state.session_id)
    directory = workspace.path / growth.candidate_directory(row)
    directory.mkdir(parents=True)
    if native:
        manifest = {"format": "hikari.native.v1", "owner": "hikari.private",
                    "capability_id": row["capability_id"], "version": row["version"],
                    "input_schema": INPUT, "output_schema": OUTPUT,
                    "entrypoint": "implementation.py:invoke", "permissions": ["filesystem.read"]}
        (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        # Importing generated code would fail. The host must only parse it.
        (directory / "implementation.py").write_text(
            "raise RuntimeError('Resident must never import this')\ndef invoke(inputs):\n    return inputs\n",
            encoding="utf-8")
        (directory / "test_implementation.py").write_text("def test_candidate():\n    assert True\n", encoding="utf-8")
    else:
        (directory / "recipe.json").write_text(json.dumps(candidate if candidate is not None else recipe()), encoding="utf-8")
        (directory / "tests.json").write_text(json.dumps(tests if tests is not None else CASES), encoding="utf-8")
    if extra_file:
        (workspace.path / extra_file).write_text("out of bounds", encoding="utf-8")
    git(workspace.path, "add", ".")
    git(workspace.path, "commit", "-m", "synthetic model candidate")
    growth.engineering_store.save(replace(state, workspace_path=str(workspace.path),
        workspace_branch=workspace.branch, baseline_commit=workspace.baseline_commit))
    growth.engineering_store.save_result(state.session_id, EngineeringResult(
        turn_id=row["turn_id"], status="completed", message="Candidate files supplied by fake backend",
        changed_files=workspace.changed_files(), completed_at=time.time()))
    return growth.advance(row["request_id"]), workspace


def activate(growth, row):
    return growth.operator_activate(row["request_id"], approved_digest=row["candidate_digest"],
                                    operator_ref="operator:review:123")


def test_real_recipe_growth_activation_invocation_resume_and_restart(growth):
    source_head = git(growth.repository, "rev-parse", "HEAD")
    original = request(growth)
    tested, workspace = complete(growth, original)
    assert tested["status"] == "candidate_tested"
    assert tested["evidence"]["validation"] == {
        "runner": "hikari-owned RecipeRuntime v1", "passed": True, "case_count": 4}
    assert tested["evidence"]["live"] is False
    assert tested["intent"] == TURN.text
    assert tested["source_turn"]["actor_id"] == "owner"
    assert git(growth.repository, "rev-parse", "HEAD") == source_head
    with pytest.raises(CapabilityError, match="not active"):
        growth.invoke("private.todo_summary", CASES[0]["input"], turn=TURN)
    active = activate(growth, tested)
    assert active["status"] == "active"
    assert growth.invoke("private.todo_summary", {"text": "TODO pay bill\nTODO pay bill"}, turn=TURN) == {
        "todos": ["pay bill"], "count": 1}
    resumed = growth.resume(original["request_id"], turn=TURN)
    assert resumed["status"] == "resumed" and resumed["result"] == CASES[0]["expected"]
    reopened = CapabilityGrowth(growth.path, engineering_store=growth.engineering_store,
                                 repository=growth.repository, implementation_enabled=True)
    assert reopened.resume(original["request_id"], turn=TURN) == resumed
    assert reopened.request(source_ref="wire:1", turn=TURN, capability_id="private.todo_summary",
        input_schema=INPUT, output_schema=OUTPUT, acceptance_cases=CASES,
        constraints=["preserve first-seen order"], resume_input=CASES[0]["input"]) == resumed
    assert reopened.describe()["requests"][0]["status"] == "resumed"
    # Calls use the immutable validated snapshot, not subsequently altered candidate files.
    (workspace.path / growth.candidate_directory(tested) / "recipe.json").write_text("{}", encoding="utf-8")
    assert reopened.invoke("private.todo_summary", CASES[0]["input"], turn=TURN) == CASES[0]["expected"]


def test_source_identity_and_acceptance_are_immutable(growth):
    row = request(growth)
    assert request(growth)["request_id"] == row["request_id"]
    for change in ({"turn": replace(TURN, text="Different intent")}, {"constraints": ["different"]},
                   {"turn": replace(TURN, actor_id="other")}, {"acceptance_cases": CASES[:1]}):
        with pytest.raises(CapabilityError, match="immutable"):
            request(growth, **change)
    with sqlite3.connect(growth.path) as db, pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("UPDATE growth_requests SET request_json='{}'")


def test_shared_turns_cannot_request_invoke_or_resume(growth):
    with pytest.raises(CapabilityError, match="private"):
        request(growth, turn=replace(TURN, scope="shared"))
    tested, _ = complete(growth, request(growth))
    activate(growth, tested)
    for turn in (replace(TURN, scope="shared"), replace(TURN, actor_id="other"),
                 replace(TURN, actor_id=None), replace(TURN, conversation_id="different")):
        with pytest.raises(CapabilityError):
            growth.invoke("private.todo_summary", CASES[0]["input"], turn=turn)
        with pytest.raises(CapabilityError):
            growth.resume(tested["request_id"], turn=turn)


def test_model_description_contains_only_exact_owner_requests_and_active_interfaces(growth):
    tested, _ = complete(growth, request(growth))
    activate(growth, tested)
    other_turn = replace(TURN, conversation_id="private-b", actor_id="different-owner")
    other = request(growth, source_ref="wire:other", turn=other_turn, capability_id="private.other")
    collision = request(growth, source_ref="wire:collision", turn=other_turn)
    own = growth.describe(turn=TURN)
    assert [item["request_id"] for item in own["requests"]] == [tested["request_id"]]
    assert own["active_interfaces"] == [{"capability_id": tested["capability_id"], "version": 1,
        "input_schema": INPUT, "output_schema": OUTPUT, "permissions": [],
        "candidate_digest": tested["candidate_digest"]}]
    other_view = growth.describe(turn=other_turn)
    assert [item["request_id"] for item in other_view["requests"]] == [other["request_id"], collision["request_id"]]
    assert other_view["active_interfaces"] == []
    assert growth.describe(turn=replace(TURN, actor_id=None))["requests"] == []
    assert len(growth.describe()["requests"]) == 3  # Explicit operator-only inspection.
    with pytest.raises(CapabilityError, match="private"):
        growth.describe(turn=replace(TURN, scope="shared"))


def test_dispatch_is_durable_idempotent_and_narrow(growth):
    row = request(growth)
    queued = growth.advance(row["request_id"])
    assert growth.advance(row["request_id"]) == queued
    state = growth.engineering_store.load(queued["session_id"])
    turn = growth.engineering_store.load_turn(queued["session_id"], queued["turn_id"])
    assert len(growth.engineering_store.list_states()) == 1
    assert turn.authority.repository_write and turn.authority.run_tests
    assert not turn.authority.publish and not turn.authority.network and not turn.authority.outside_repo
    context = json.loads(turn.context)
    assert context["source_request"]["intent"] == TURN.text
    assert context["source_request"]["acceptance_cases"] == CASES
    assert turn.effect == "maintain_project"
    assert turn.source_request_id == row["request_id"]
    assert turn.constraints == ("preserve first-seen order",)
    assert [json.loads(item) for item in turn.acceptance_criteria] == CASES
    assert len(context["allowed_changed_files"]) == 2
    assert state.current_turn_id == queued["turn_id"]
    assert growth.advance_all()[0]["status"] == "implementing"


def test_crash_after_engineering_enqueue_recovers_same_turn(growth, monkeypatch):
    row = request(growth)
    original_status = growth._status

    def crash(*args, **kwargs):
        raise RuntimeError("simulated process loss after engineering enqueue")

    monkeypatch.setattr(growth, "_status", crash)
    with pytest.raises(RuntimeError, match="process loss"):
        growth.advance(row["request_id"])
    state = growth.engineering_store.list_states()[0]
    assert state.status == "pending" and growth.get(row["request_id"])["status"] == "requested"
    monkeypatch.setattr(growth, "_status", original_status)
    recovered = growth.advance(row["request_id"])
    assert recovered["session_id"] == state.session_id
    assert recovered["turn_id"] == state.current_turn_id
    assert len(growth.engineering_store.list_states()) == 1


def test_legacy_growth_turn_without_additive_handoff_fields_is_not_rewritten(growth, monkeypatch):
    row = request(growth)
    original_status = growth._status

    def crash(*args, **kwargs):
        raise RuntimeError("simulated old dispatcher crash")

    monkeypatch.setattr(growth, "_status", crash)
    with pytest.raises(RuntimeError):
        growth.advance(row["request_id"])
    state = growth.engineering_store.list_states()[0]
    path = growth.engineering_store.root / state.session_id / "turns" / (state.current_turn_id + ".json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    for field in ("effect", "source_request_id", "constraints", "acceptance_criteria"):
        payload.pop(field)
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(growth, "_status", original_status)
    recovered = growth.advance(row["request_id"])
    assert recovered["status"] == "implementing"
    assert path.read_bytes() == before


def test_disabled_growth_preserves_request_then_explicit_retry(growth):
    growth.implementation_enabled = False
    row = growth.advance(request(growth)["request_id"])
    assert row["status"] == "blocked"
    assert not growth.engineering_store.list_states()
    growth.implementation_enabled = True
    assert growth.advance(row["request_id"])["status"] == "blocked"
    retry = growth.retry(row["request_id"], turn=TURN)
    assert retry["attempt"] == 2 and retry["intent"] == TURN.text
    assert growth.advance(row["request_id"])["status"] == "implementing"
    assert "blocked" in [event["kind"] for event in growth.events(row["request_id"])]


def test_failed_acceptance_retains_actual_evidence(growth):
    candidate = recipe()
    candidate["return"]["count"] = 99
    failed, _ = complete(growth, request(growth), candidate=candidate)
    assert failed["status"] == "failed"
    assert "immutable_acceptance case 0 failed" in failed["evidence"]["reason"]
    events = growth.events(failed["request_id"])
    observed = next(event for event in events if event["kind"] == "candidate_observed")
    assert observed["evidence"]["provenance"]["commit"]
    case = next(event for event in events if event["kind"] == "validation_case")
    assert case["evidence"]["actual"]["count"] == 99 and case["evidence"]["passed"] is False
    with pytest.raises(CapabilityError, match="tested"):
        activate(growth, failed)


@pytest.mark.parametrize("alteration,reason", [
    ("schema", "immutable output_schema"), ("permission", "permission expansion"),
    ("service", "unsupported service"), ("extra_file", "outside"),
])
def test_candidate_cannot_change_contract_permissions_or_scope(growth, alteration, reason):
    candidate = recipe()
    extra = None
    if alteration == "schema":
        candidate["output_schema"] = {"type": "string"}
    elif alteration == "permission":
        candidate["permissions"] = ["shell"]
    elif alteration == "service":
        candidate["steps"][0]["service"] = "python.exec"
    else:
        extra = "README.md"
    row, _ = complete(growth, request(growth), candidate=candidate, extra_file=extra)
    assert row["status"] == "failed" and reason in row["evidence"]["reason"]


def test_completed_label_without_workspace_is_not_available(growth):
    queued = growth.advance(request(growth)["request_id"])
    growth.engineering_store.save_result(queued["session_id"], EngineeringResult(
        turn_id=queued["turn_id"], status="completed", message="available=true, all tests pass",
        completed_at=time.time()))
    row = growth.advance(queued["request_id"])
    assert row["status"] == "failed" and row["candidate_digest"] is None


def test_disagreeing_terminal_record_is_not_available(growth):
    queued = growth.advance(request(growth)["request_id"])
    growth.engineering_store.save_result(queued["session_id"], EngineeringResult(
        turn_id=queued["turn_id"], status="completed", message="completed", completed_at=time.time()))
    state = growth.engineering_store.load(queued["session_id"])
    growth.engineering_store.save(replace(state, status="failed"))
    row = growth.advance(queued["request_id"])
    assert row["status"] == "failed" and "disagree" in row["evidence"]["reason"]


def test_operator_activation_requires_exact_digest_and_registry_integrity(growth):
    row, _ = complete(growth, request(growth))
    with pytest.raises(CapabilityError, match="content"):
        growth.operator_activate(row["request_id"], approved_digest="wrong", operator_ref="operator:1")
    activate(growth, row)
    with sqlite3.connect(growth.path) as db:
        db.execute("UPDATE growth_candidates SET candidate_json='{}'")
    with pytest.raises(CapabilityError, match="matches evidence"):
        growth.invoke("private.todo_summary", CASES[0]["input"], turn=TURN)


def test_native_candidate_routes_code_but_never_executes_or_claims_tested(growth):
    original = request(growth, implementation_kind="native")
    row, _ = complete(growth, original, native=True)
    assert row["status"] == "candidate_implemented"
    assert row["evidence"]["validation"]["passed"] is None
    assert row["evidence"]["live"] is False
    with pytest.raises(CapabilityError, match="host adapter"):
        activate(growth, row)
    with pytest.raises(CapabilityError, match="not active"):
        growth.invoke("private.todo_summary", CASES[0]["input"], turn=TURN)


@pytest.mark.parametrize("inputs", [{"text": 1}, {"text": "okay", "command": "danger"},
                                   {"text": "x" * 128001}])
def test_runtime_enforces_input_schema_and_size(inputs):
    with pytest.raises(CapabilityError):
        RecipeRuntime().invoke(recipe(), inputs)


def test_runtime_bounded_output_and_invalid_references():
    with pytest.raises(CapabilityError, match="item limit"):
        RecipeRuntime().invoke(recipe(), {"text": "a\n" * 2001})
    candidate = recipe()
    candidate["steps"][0]["args"]["text"] = {"ref": "input.__class__"}
    with pytest.raises(CapabilityError, match="unresolved reference"):
        RecipeRuntime().invoke(candidate, {"text": "hello"})


def test_runtime_rejects_amplification_before_constructing_large_output():
    candidate = recipe()
    candidate["output_schema"] = {"type": "string"}
    candidate["steps"] = [
        {"id": "lines", "service": "text.lines", "args": {"text": {"ref": "input.text"}}},
        {"id": "joined", "service": "lines.join", "args": {
            "lines": {"ref": "lines"}, "separator": "a" * 10_000}},
    ]
    candidate["return"] = {"ref": "joined"}
    with pytest.raises(CapabilityError, match="joined output"):
        RecipeRuntime().invoke(candidate, {"text": "a\n" * 100})


def test_version_collision_never_replaces_other_owned_candidate(growth):
    row, _ = complete(growth, request(growth))
    other, _ = complete(growth, request(growth, source_ref="wire:2"))
    assert other["status"] == "failed" and "already owned" in other["evidence"]["reason"]
    assert growth.get(row["request_id"])["candidate_digest"] == row["candidate_digest"]
