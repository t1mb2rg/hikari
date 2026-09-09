import json
from pathlib import Path

import pytest

from integrations.github.actions import GitHubActionService
from integrations.github.client import GitHubClient, GitHubError, GitHubOutcomeUnknown
from integrations.github.governance import GitHubEvidenceStore, GitHubMergeGate, GitHubPolicyStore


HEAD, BASE, BLOB = "a" * 40, "b" * 40, "c" * 40
REPO = "owner/hikari"
BRANCH = "hikari/engineering/fix"


class Remote:
    repository = REPO

    def __init__(self):
        self.calls = []
        self.head = HEAD
        self.base = BASE
        self.draft = False
        self.files = [{"filename": "feature.py", "status": "added"}]
        self.review_result = []
        self.check_result = [{"id": 1, "name": "tests", "head_sha": HEAD, "status": "completed", "conclusion": "success"}]
        self.run = {"head_branch": BRANCH, "status": "completed", "conclusion": "failure", "event": "pull_request",
                    "path": ".github/workflows/test.yml", "head_sha": HEAD}
        self.fail_create = False
        self.malformed_create = False

    def pull_requests(self, **kwargs):
        return [{"number": 2, "title": "actual title"}]

    def pull_request(self, number):
        return {"number": number, "title": "actual title", "body": "body", "state": "open", "merged": False,
                "draft": self.draft, "mergeable": True, "mergeable_state": "blocked" if self.draft else "clean",
                "head": {"sha": self.head, "ref": BRANCH, "repo": {"full_name": self.repository}},
                "base": {"sha": self.base, "ref": "main"}}

    def workflow_runs(self):
        return [{"id": 4, "status": "completed", "conclusion": "failure"}]

    def workflow_run(self, run_id):
        return self.run

    def workflow_jobs(self, run_id):
        return {"total_count": 1, "jobs": [{"id": 5, "conclusion": "failure"}]}

    def job_log(self, job_id):
        return "actual failure log"

    def read_file(self, path, *, ref):
        return {"path": path, "ref": ref, "sha": BLOB, "content": "content", "type": "file"}

    def create_branch(self, branch, sha):
        self.calls.append(("create_branch", branch))
        return {"ref": "refs/heads/" + branch, "object": {"sha": sha}}

    def write_file(self, **kwargs):
        self.calls.append(("write_file", kwargs))
        return {"commit": {"sha": HEAD}, "content": {"sha": BLOB}}

    def create_pull_request(self, **kwargs):
        self.calls.append(("create_pr", kwargs))
        if self.fail_create:
            raise GitHubOutcomeUnknown("timeout after create")
        if self.malformed_create:
            return {"number": 2}
        self.draft = True
        return self.pull_request(2)

    def update_pull_request(self, number, **kwargs):
        self.calls.append(("update_pr", number))
        return {**self.pull_request(number), **kwargs}

    def rerun_workflow(self, run_id):
        self.calls.append(("rerun", run_id))
        return {}

    def checks(self, head):
        return self.check_result

    def reviews(self, number):
        return self.review_result

    def changed_files(self, number):
        return self.files

    def mark_ready(self, number, *, expected_head):
        self.calls.append(("ready", expected_head))
        self.draft = False
        return {"draft": False}

    def _merge_after_gate(self, number, **kwargs):
        self.calls.append(("merge", kwargs))
        return {"merged": True, "sha": "d" * 40}


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("HIKARI_GITHUB_REPOSITORY", raising=False)
    monkeypatch.delenv("HIKARI_GITHUB_ALLOWED_REPOSITORIES", raising=False)
    remote = Remote()
    service = GitHubActionService(tmp_path, repositories=[REPO], client_factory=lambda repo: remote)
    return service, remote


def policy(service, *, enabled=True, pins=None):
    document = {"version": 1, "repositories": {REPO: {"auto_merge": enabled, "allowed_bases": ["main"], "required_checks": ["tests"],
                "require_physical_gate": True, "rerun_workflows": pins or {}}}}
    store = GitHubPolicyStore(service.policy_path)
    return store.save(document, expected_revision=store.load()["revision"], operator=True)


def owned(service, remote):
    evidence = GitHubEvidenceStore(service.state_dir / "github_evidence.db")
    evidence.record_owned(REPO, 2, session_id="conversation", head=BRANCH, base="main")
    evidence.record_physical_gate(REPO, 2, HEAD, "Operator physically verified actual feature behavior on this head")
    return evidence


@pytest.mark.parametrize("action,arguments,key", [
    ("list_prs", {}, "pull_requests"), ("read_pr", {"number": 2}, "number"),
    ("list_runs", {}, "workflow_runs"), ("read_file", {"path": "feature.py", "ref": HEAD}, "content"),
    ("jobs", {"run_id": 4}, "jobs"), ("logs", {"job_id": 5}, "content"),
])
def test_reads_are_grounded_and_create_no_evidence(service, action, arguments, key):
    facade, remote = service
    result = facade.handle(action, arguments, "message", "conversation")
    assert result["status"] == "ok"
    assert key in result["data"]
    assert not facade.state_dir.exists()
    assert remote.calls == []


@pytest.mark.parametrize("action,arguments", [
    ("grant_permission", {}), ("deploy", {}), ("create_branch", {"branch": "main", "sha": HEAD}),
    ("list_prs", {"repository": "other/repo"}), ("read_pr", {"number": True}),
    ("list_prs", {"shell": "anything"}), ("update_pr", {"number": 2}),
    ("read_file", {"path": "../secret", "ref": HEAD}),
])
def test_unknown_effect_or_scope_is_blocked_before_remote_mutation(service, action, arguments):
    facade, remote = service
    assert facade.handle(action, arguments, "message", "conversation")["status"] == "blocked"
    assert remote.calls == []


def test_catalog_excludes_authority_and_evidence_writes():
    catalog = GitHubActionService.catalog()
    assert {item["name"] for item in catalog} == {"list_prs", "read_pr", "list_runs", "read_file", "jobs", "logs", "create_branch", "write_file", "create_pr", "update_pr", "rerun_failed", "merge_pr"}
    assert all(item["parameters"]["additionalProperties"] is False for item in catalog)
    catalog[0]["parameters"]["required"].append("mutated")
    assert "mutated" not in GitHubActionService.catalog()[0]["parameters"]["required"]


def test_real_job_log_transport_opts_in_then_strips_terminal_controls(monkeypatch):
    import subprocess
    calls = []
    monkeypatch.setattr("integrations.github.client.shutil.which", lambda *args, **kwargs: "gh")
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="step: \x1b[32msuccess\x1b[0m\n\x1b]0;fake-title\x07next\x00\tline", stderr="")
    monkeypatch.setattr("integrations.github.client.subprocess.run", run)
    content = GitHubClient(REPO).job_log(5)
    assert "--allow-escape-sequences" in calls[0]
    assert content == "step: success\nnext\tline"


def test_write_receipt_replay_does_not_repeat_external_effect_and_ref_is_immutable(service):
    facade, remote = service
    args = {"branch": BRANCH, "sha": HEAD}
    first = facade.handle("create_branch", args, "action-1", "conversation")
    assert first["status"] == "completed"
    replay = facade.handle("create_branch", args, "action-1", "conversation")
    assert replay["replayed"] and replay["data"] == first["data"]
    changed = facade.handle("create_branch", {**args, "sha": BASE}, "action-1", "conversation")
    assert changed["status"] == "blocked"
    assert len(remote.calls) == 1


def test_write_requires_runtime_provenance(service):
    facade, remote = service
    assert facade.handle("create_branch", {"branch": BRANCH, "sha": HEAD}, "", "conversation")["status"] == "blocked"
    assert remote.calls == []


@pytest.mark.parametrize("mode", ["timeout", "malformed"])
def test_uncertain_create_does_not_fabricate_ownership_or_retry(service, mode):
    facade, remote = service
    remote.fail_create = mode == "timeout"
    remote.malformed_create = mode == "malformed"
    args = {"head": BRANCH, "base": "main", "title": "Fix", "body": "Details"}
    first = facade.handle("create_pr", args, "action-1", "conversation")
    assert first["status"] == "unknown"
    assert facade.handle("create_pr", args, "action-1", "conversation")["status"] == "unknown"
    assert len(remote.calls) == 1
    assert GitHubEvidenceStore(facade.state_dir / "github_evidence.db").owned(REPO, 2) is None


def test_only_confirmed_created_pr_receives_ownership(service):
    facade, remote = service
    args = {"head": BRANCH, "base": "main", "title": "Fix", "body": "Details"}
    result = facade.handle("create_pr", args, "action-1", "conversation")
    assert result["status"] == "completed"
    assert GitHubEvidenceStore(facade.state_dir / "github_evidence.db").owned(REPO, 2)["session_id"] == "conversation"


def test_restart_pending_receipt_never_retries(service):
    facade, remote = service
    args = {"branch": BRANCH, "sha": HEAD}
    store = GitHubEvidenceStore(facade.state_dir / "github_evidence.db")
    store.claim_action(source_ref="action-1", conversation_id="conversation", action="create_branch", repository=REPO, arguments=args)
    assert facade.handle("create_branch", args, "action-1", "conversation")["status"] == "unknown"
    assert remote.calls == []


@pytest.mark.parametrize("path", [".github/workflows/test.yml", "integrations/github/governance.py", "state/github_policy.json", "deploy/prod.yml", "tests/conftest.py", "tests/test_existing.py", "actions/authorization.py", "./actions/authorization.py", "./state/github_policy.json"])
def test_conversation_cannot_change_authority_or_existing_validation(service, path):
    facade, remote = service
    args = {"path": path, "content": "changed", "branch": BRANCH, "message": "Change", "expected_blob_sha": BLOB}
    assert facade.handle("write_file", args, "action-1", "conversation")["status"] == "blocked"
    assert remote.calls == []


def test_new_ordinary_test_can_be_created(service):
    facade, remote = service
    result = facade.handle("write_file", {"path": "tests/test_new_feature.py", "content": "def test_new(): pass", "branch": BRANCH, "message": "Test new feature"}, "action-1", "conversation")
    assert result["status"] == "completed"


def test_update_requires_actual_owned_pr(service):
    facade, remote = service
    assert facade.handle("update_pr", {"number": 2, "title": "Changed"}, "action-1", "conversation")["status"] == "blocked"
    owned(facade, remote)
    assert facade.handle("update_pr", {"number": 2, "title": "Changed"}, "action-2", "conversation")["status"] == "completed"


def test_rerun_pin_is_operator_owned_and_does_not_claim_ci_success(service):
    facade, remote = service
    first = facade.handle("rerun_failed", {"run_id": 4}, "action-1", "conversation")
    assert first["status"] == "blocked"
    assert first["blocker"]["code"] == "workflow_pin_required"
    assert first["blocker"]["observed_blob_sha"] == BLOB
    assert remote.calls == []
    policy(facade, pins={remote.run["path"]: BLOB})
    result = facade.handle("rerun_failed", {"run_id": 4}, "action-2", "conversation")
    assert result["status"] == "completed"
    assert result["data"]["status"] == "rerun_requested"
    assert result["data"]["conclusion"] is None


def test_changed_workflow_pin_stays_blocked(service):
    facade, remote = service
    policy(facade, pins={remote.run["path"]: HEAD})
    result = facade.handle("rerun_failed", {"run_id": 4}, "action-1", "conversation")
    assert result["status"] == "blocked"
    assert result["blocker"]["configured_blob_sha"] == HEAD
    assert remote.calls == []


@pytest.mark.parametrize("change", [{"head_branch": "main"}, {"conclusion": "success"}, {"event": "deployment"}])
def test_rerun_rejects_deployment_and_nonfailed_runs(service, change):
    facade, remote = service
    remote.run.update(change)
    policy(facade, pins={remote.run["path"]: BLOB})
    assert facade.handle("rerun_failed", {"run_id": 4}, "action-1", "conversation")["status"] == "blocked"
    assert remote.calls == []


def test_operator_policy_defaults_disabled_and_requires_revision_and_operator(tmp_path):
    store = GitHubPolicyStore(tmp_path / "state/github_policy.json")
    initial = store.load()
    assert initial == {"revision": "absent", "document": {"version": 1, "repositories": {}}, "configured": False}
    assert not store.path.parent.exists()
    with pytest.raises(PermissionError):
        store.save(initial["document"], expected_revision="absent")
    saved = store.save(initial["document"], expected_revision="absent", operator=True)
    assert saved["revision"] != "absent"
    with pytest.raises(ValueError, match="修改"):
        store.save(initial["document"], expected_revision="absent", operator=True)


@pytest.mark.parametrize("configuration", [{"auto_merge": True}, {"allowed_bases": None}, {"required_checks": None}, {"method": "force"}, {"permissions": "write"}, {"rerun_workflows": {"workflow.yml": HEAD}}])
def test_policy_rejects_undefined_or_unsafe_configuration(tmp_path, configuration):
    with pytest.raises(ValueError):
        GitHubPolicyStore(tmp_path / "policy.json").save({"version": 1, "repositories": {REPO: configuration}}, expected_revision="absent", operator=True)


def test_draft_transitions_only_after_substantive_conditions_and_merges_verified_head(service):
    facade, remote = service
    remote.draft = True
    evidence = owned(facade, remote)
    policy(facade)
    gate = GitHubMergeGate(remote, evidence, facade.policy_path)
    assessment = gate.assess(2)
    assert not assessment["ready"] and assessment["eligible_for_ready"]
    result = facade.handle("merge_pr", {"number": 2, "expected_head": HEAD}, "action-1", "conversation")
    assert result["status"] == "completed"
    assert [call[0] for call in remote.calls] == ["ready", "merge"]
    assert remote.calls[-1][1]["expected_head"] == HEAD


def test_draft_with_failed_check_is_never_marked_ready(service):
    facade, remote = service
    remote.draft = True
    remote.check_result[0]["conclusion"] = "failure"
    owned(facade, remote)
    policy(facade)
    assert facade.handle("merge_pr", {"number": 2, "expected_head": HEAD}, "action-1", "conversation")["status"] == "blocked"
    assert remote.calls == []


def test_renamed_authority_path_still_blocks_merge(service):
    facade, remote = service
    remote.files = [{"filename": "ordinary.py", "previous_filename": "engineering/validation_policy.py", "status": "renamed"}]
    evidence = owned(facade, remote)
    policy(facade)
    assessment = GitHubMergeGate(remote, evidence, facade.policy_path).assess(2)
    assert not assessment["ready"]
    assert "engineering/validation_policy.py" in next(c for c in assessment["conditions"] if c["key"] == "authority_unchanged")["reason"]


def test_stale_approval_does_not_clear_blocking_review(service):
    facade, remote = service
    remote.review_result = [
        {"id": 1, "user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED", "commit_id": BASE},
        {"id": 2, "user": {"login": "reviewer"}, "state": "APPROVED", "commit_id": BASE},
    ]
    evidence = owned(facade, remote)
    policy(facade)
    assert not GitHubMergeGate(remote, evidence, facade.policy_path).assess(2)["ready"]
    remote.review_result[1]["commit_id"] = HEAD
    assert GitHubMergeGate(remote, evidence, facade.policy_path).assess(2)["ready"]


def test_head_race_during_draft_transition_never_merges(service):
    facade, remote = service
    remote.draft = True
    evidence = owned(facade, remote)
    policy(facade)
    original = remote.mark_ready
    def change_head(number, *, expected_head):
        original(number, expected_head=expected_head)
        remote.head = "e" * 40
    remote.mark_ready = change_head
    with pytest.raises(GitHubError):
        GitHubMergeGate(remote, evidence, facade.policy_path).merge(2, expected_head=HEAD)
    assert [call[0] for call in remote.calls] == ["ready"]


def test_final_target_ref_race_with_same_commit_blocks_merge(service):
    facade, remote = service
    evidence = owned(facade, remote)
    policy(facade)
    original = remote.pull_request
    calls = 0
    def retarget(number):
        nonlocal calls
        calls += 1
        data = original(number)
        if calls == 3:
            data["base"]["ref"] = "release"
        return data
    remote.pull_request = retarget
    with pytest.raises(GitHubError, match="分支状态"):
        GitHubMergeGate(remote, evidence, facade.policy_path).merge(2, expected_head=HEAD)
    assert remote.calls == []


def test_final_operator_policy_revision_race_blocks_merge(service):
    facade, remote = service
    evidence = owned(facade, remote)
    policy(facade)
    original = remote.pull_request
    calls = 0
    def revoke(number):
        nonlocal calls
        calls += 1
        if calls == 3:
            policy(facade, enabled=False)
        return original(number)
    remote.pull_request = revoke
    with pytest.raises(GitHubError, match="授权配置"):
        GitHubMergeGate(remote, evidence, facade.policy_path).merge(2, expected_head=HEAD)
    assert remote.calls == []


@pytest.mark.parametrize("number", [77, 78, 79])
def test_historic_m7_gate_prs_never_auto_merge(service, number):
    facade, remote = service
    remote.repository = "t1mb2rg/hikari"
    evidence = GitHubEvidenceStore(facade.state_dir / "github_evidence.db")
    evidence.record_owned(remote.repository, number, session_id="legacy", head=BRANCH, base="main")
    evidence.record_physical_gate(remote.repository, number, HEAD, "Historical recorded gate evidence must not bypass operator decision")
    document = {"version": 1, "repositories": {remote.repository: {"auto_merge": True, "allowed_bases": ["main"], "required_checks": ["tests"]}}}
    GitHubPolicyStore(facade.policy_path).save(document, expected_revision="absent", operator=True)
    assessment = GitHubMergeGate(remote, evidence, facade.policy_path).assess(number)
    assert not assessment["ready"]
    assert not next(c for c in assessment["conditions"] if c["key"] == "historic_gate")["passed"]
