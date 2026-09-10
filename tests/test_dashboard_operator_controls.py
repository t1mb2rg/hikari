import hashlib
import json
import sqlite3
import subprocess
import sys

import pytest

from capabilities import CapabilityError, CapabilityGrowth, RecipeRuntime
from capabilities.runtime import canonical
from capabilities.operator import CapabilityOperatorControls
from conversation.models import UserTurn
from dashboard.operator_controls import DashboardOperatorControls, OperatorPolicyConflict
from dashboard.probes import DashboardProbeConfig
from engineering.session import EngineeringSessionStore
from integrations.github.governance import GitHubPolicyStore
from resident.file_locks import serialized_file_update


SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}},
          "required": ["text"], "additionalProperties": False}
TURN = UserTurn("test", "private-operator", "Make the supplied text uppercase", actor_id="owner")
CASES = [{"input": {"text": "hello"}, "expected": {"text": "HELLO"}}]


@pytest.fixture
def controls(tmp_path):
    repository = tmp_path / "source"
    repository.mkdir()
    return DashboardOperatorControls(DashboardProbeConfig(repository, tmp_path / "state"))


def seed_candidate(controls, *, native=False, identity="private.uppercase", bad_tests=False):
    """Synthetic candidate receipt for operator-boundary tests; execute the real pure runtime."""
    growth = CapabilityGrowth(controls.growth_path,
        engineering_store=EngineeringSessionStore(controls.config.state_dir / "engineering"),
        repository=controls.config.repository, implementation_enabled=False)
    request = growth.request(source_ref=identity, turn=TURN, capability_id=identity,
        input_schema=SCHEMA, output_schema=SCHEMA, acceptance_cases=CASES,
        implementation_kind="native" if native else "recipe", resume_input=CASES[0]["input"])
    recipe = {"format": "hikari.recipe.v1", "capability_id": identity, "version": 1,
        "owner": "hikari.private", "permissions": [], "input_schema": SCHEMA, "output_schema": SCHEMA,
        "steps": [{"id": "upper", "service": "text.upper", "args": {"text": {"ref": "input.text"}}}],
        "return": {"text": {"ref": "upper"}}}
    assert RecipeRuntime().invoke(recipe, CASES[0]["input"]) == CASES[0]["expected"]
    tests = [{"input": {"text": "hello"}, "expected": {"text": "wrong"}}] if bad_tests else CASES
    candidate = {"kind": "native" if native else "recipe", "recipe": recipe,
                 "source_files": {"recipe.json": json.dumps(recipe), "tests.json": json.dumps(tests)},
                 "provenance": {"synthetic_operator_test_fixture": True}}
    digest = hashlib.sha256(canonical(candidate).encode("utf-8")).hexdigest()
    evidence = {"validation": {"passed": None if native else True,
        "runner": "AST syntax inspection only" if native else "hikari-owned RecipeRuntime v1", "case_count": 2},
        "live": False, "candidate_digest": digest}
    with sqlite3.connect(growth.path) as db:
        db.execute("INSERT INTO growth_candidates VALUES(?,?,?,?,?)", (identity, 1, request["request_id"], digest, canonical(candidate)))
        db.execute("UPDATE growth_requests SET status=?,candidate_digest=?,evidence_json=? WHERE request_id=?",
                   ("candidate_implemented" if native else "candidate_tested", digest, canonical(evidence), request["request_id"]))
    return growth, growth.get(request["request_id"])


def policy(*services, enabled=True):
    return {"version": 1, "auto_activate_pure_recipes": enabled, "allowed_services": list(services)}


def github_policy():
    return {"version": 1, "repositories": {"owner/repo": {
        "auto_merge": True, "allowed_bases": ["main"], "required_checks": ["tests"],
        "require_physical_gate": True, "method": "squash", "rerun_workflows": {}}}}


def test_all_gets_are_read_only_and_default_policy_is_disabled(controls, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("GET must not construct a writer")
    monkeypatch.setattr(CapabilityGrowth, "__init__", forbidden)
    assert controls.get_github_policy() == {"revision": "absent", "configured": False,
        "document": {"version": 1, "repositories": {}}}
    assert controls.get_growth_policy()["document"] == policy(enabled=False)
    assert controls.growth_snapshot() == {"configured": False, "status": "absent", "requests": [],
                                         "active_interfaces": [], "errors": []}
    assert controls.auto_activate_tested_capabilities()["enabled"] is False
    assert not controls.config.state_dir.exists()


def test_reusable_capability_operator_import_does_not_load_dashboard():
    completed = subprocess.run([sys.executable, "-B", "-c",
        "import sys; from capabilities.operator import CapabilityOperatorControls; "
        "assert not any(name == 'dashboard' or name.startswith('dashboard.') for name in sys.modules)"],
        capture_output=True, text=True, timeout=20)
    assert completed.returncode == 0, completed.stderr


def test_dashboard_and_resident_share_the_same_operator_policy_truth(controls):
    reusable = CapabilityOperatorControls(controls.config.repository, controls.config.state_dir)
    assert reusable.get_growth_policy() == controls.get_growth_policy()
    saved = reusable.save_growth_policy(policy("text.upper"), "absent")
    assert saved == controls.get_growth_policy()
    disabled = controls.save_growth_policy(policy(enabled=False), saved["revision"])
    assert disabled == reusable.get_growth_policy()
    assert reusable.auto_activate_tested_capabilities()["enabled"] is False


def test_legacy_sqlite_get_reports_missing_schema_without_migration(controls):
    controls.config.state_dir.mkdir()
    with sqlite3.connect(controls.growth_path) as db:
        db.execute("CREATE TABLE legacy_marker(value)")
    before = controls.growth_path.read_bytes()
    snapshot = controls.growth_snapshot()
    assert snapshot["status"] == "error" and snapshot["errors"]
    assert controls.growth_path.read_bytes() == before
    with sqlite3.connect(controls.growth_path) as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("legacy_marker",)]


def test_existing_registry_get_does_not_write_or_construct_growth(controls, monkeypatch):
    _, request = seed_candidate(controls)
    before = controls.growth_path.read_bytes()
    monkeypatch.setattr(CapabilityGrowth, "__init__", lambda *a, **kw: pytest.fail("GET constructed writer"))
    snapshot = controls.growth_snapshot()
    assert snapshot["status"] == "ready"
    assert snapshot["requests"][0]["request_id"] == request["request_id"]
    assert snapshot["requests"][0]["activatable"] is True
    assert controls.growth_path.read_bytes() == before


def test_github_save_reuses_authoritative_governance_and_detects_conflict(controls, monkeypatch):
    original = GitHubPolicyStore.save
    calls = []
    def observe(self, document, **kwargs):
        calls.append(kwargs)
        return original(self, document, **kwargs)
    monkeypatch.setattr(GitHubPolicyStore, "save", observe)
    saved = controls.save_github_policy(github_policy(), "absent")
    assert calls == [{"expected_revision": "absent", "operator": True}]
    assert saved == GitHubPolicyStore(controls.github_policy_path).load()
    before = controls.github_policy_path.read_bytes()
    with pytest.raises(OperatorPolicyConflict):
        controls.save_github_policy(github_policy(), "absent")
    assert controls.github_policy_path.read_bytes() == before
    invalid = github_policy()
    invalid["repositories"]["owner/repo"]["required_checks"] = []
    with pytest.raises(ValueError):
        controls.save_github_policy(invalid, saved["revision"])


@pytest.mark.parametrize("document", [
    {"version": 1, "auto_activate_pure_recipes": "true", "allowed_services": ["text.upper"]},
    {"version": True, "auto_activate_pure_recipes": True, "allowed_services": ["text.upper"]},
    policy("shell.exec"), policy("*"), policy("text.upper", "text.upper"), policy(),
    {**policy("text.upper"), "allow_native": True},
])
def test_invalid_growth_policy_never_writes(controls, document):
    with pytest.raises(ValueError):
        controls.save_growth_policy(document, "absent")
    assert not controls.config.state_dir.exists()


def test_growth_policy_revision_and_exclusive_write_conflicts(controls):
    saved = controls.save_growth_policy(policy("text.upper"), "absent")
    assert saved["configured"] and len(saved["revision"]) == 64
    before = controls.growth_policy_path.read_bytes()
    with pytest.raises(OperatorPolicyConflict):
        controls.save_growth_policy(policy(enabled=False), "absent")
    assert controls.growth_policy_path.read_bytes() == before
    with serialized_file_update(controls.growth_policy_path):
        with pytest.raises(OperatorPolicyConflict):
            controls.save_growth_policy(policy(enabled=False), saved["revision"])
    assert controls.save_growth_policy(policy(enabled=False), saved["revision"])["document"] == policy(enabled=False)


def test_growth_policy_lock_is_released_after_real_process_crash(controls):
    code = (
        "import os,sys; from pathlib import Path; "
        "from capabilities.operator import CapabilityOperatorControls; "
        "control=CapabilityOperatorControls(Path(sys.argv[1]),Path(sys.argv[2])); "
        "lock=control._growth_policy_lock(); lock.__enter__(); os._exit(17)"
    )
    result = subprocess.run([sys.executable, "-B", "-c", code,
                             str(controls.config.repository), str(controls.config.state_dir)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 17, result.stderr
    assert not controls.growth_policy_path.exists()
    saved = controls.save_growth_policy(policy("text.upper"), "absent")
    assert saved["configured"] is True


def test_policies_cannot_be_saved_in_any_candidate_worktree(tmp_path):
    repository = tmp_path / "repository"
    repository.mkdir()
    inside = DashboardOperatorControls(DashboardProbeConfig(repository, repository / "state"))
    for method, document in ((inside.save_growth_policy, policy("text.upper")),
                             (inside.save_github_policy, github_policy())):
        with pytest.raises(ValueError, match="源码工作区"):
            method(document, "absent")
    worktree = tmp_path / "separate_worktree"
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: fixture", encoding="utf-8")
    elsewhere = DashboardOperatorControls(DashboardProbeConfig(repository, worktree / "state"))
    with pytest.raises(ValueError, match="Git 工作区"):
        elsewhere.save_growth_policy(policy("text.upper"), "absent")
    assert not (worktree / "state").exists()


def test_manual_activation_calls_real_digest_gate_and_generates_operator_identity(controls):
    growth, request = seed_candidate(controls)
    before = controls.growth_path.read_bytes()
    with pytest.raises(CapabilityError, match="摘要"):
        controls.operator_activate_capability(request["request_id"], "0" * 64)
    assert controls.growth_path.read_bytes() == before
    result = controls.operator_activate_capability(request["request_id"], request["candidate_digest"])
    assert result["status"] == "active"
    assert result["evidence"]["operator_ref"].startswith("dashboard:manual:")
    assert growth.invoke("private.uppercase", {"text": "world"}, turn=TURN) == {"text": "WORLD"}
    snapshot = controls.growth_snapshot()
    assert snapshot["active_interfaces"][0]["candidate_digest"] == request["candidate_digest"]
    with pytest.raises(TypeError):
        controls.operator_activate_capability(request["request_id"], request["candidate_digest"], operator_ref="forged")


def test_manual_native_and_missing_registry_activation_are_blocked(controls):
    with pytest.raises(CapabilityError):
        controls.operator_activate_capability("a" * 32, "b" * 64)
    assert not controls.config.state_dir.exists()
    _, native = seed_candidate(controls, native=True)
    before = controls.growth_path.read_bytes()
    with pytest.raises(CapabilityError, match="原生能力"):
        controls.operator_activate_capability(native["request_id"], native["candidate_digest"])
    assert controls.growth_path.read_bytes() == before


def test_explicit_policy_applies_only_tested_pure_allowed_services_and_never_native(controls):
    growth, request = seed_candidate(controls)
    _, native = seed_candidate(controls, native=True, identity="private.native")
    saved = controls.save_growth_policy(policy("text.lower"), "absent")
    assert growth.get(request["request_id"])["status"] == "candidate_tested"  # Saving is not application.
    first = controls.auto_activate_tested_capabilities()
    assert first["activated"] == [] and {item["reason"] for item in first["skipped"]} == {
        "services_outside_operator_policy", "native_requires_operator_deployment"}
    saved = controls.save_growth_policy(policy("text.upper"), saved["revision"])
    applied = controls.auto_activate_tested_capabilities()
    assert len(applied["activated"]) == 1 and not applied["errors"]
    active = applied["activated"][0]
    assert active["evidence"]["operator_ref"].startswith("dashboard:pure-policy:" + saved["revision"])
    assert growth.get(native["request_id"])["status"] == "candidate_implemented"
    assert growth.invoke("private.uppercase", {"text": "okay"}, turn=TURN) == {"text": "OKAY"}
    assert controls.auto_activate_tested_capabilities()["activated"] == []


def test_auto_activation_reruns_cases_instead_of_trusting_a_passed_label(controls):
    growth, request = seed_candidate(controls, bad_tests=True)
    controls.save_growth_policy(policy("text.upper"), "absent")
    result = controls.auto_activate_tested_capabilities()
    assert not result["activated"] and result["errors"]
    assert growth.get(request["request_id"])["status"] == "candidate_tested"
    with pytest.raises(CapabilityError, match="not active"):
        growth.invoke("private.uppercase", {"text": "hello"}, turn=TURN)
