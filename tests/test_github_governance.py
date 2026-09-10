import json
from pathlib import Path

import pytest

from integrations.github.client import GitHubClient, GitHubError
from integrations.github.governance import GitHubEvidenceStore, GitHubMergeGate


HEAD = "a" * 40
BASE = "b" * 40


class Remote:
    repository = "owner/hikari"

    def __init__(self):
        self.head = HEAD
        self.draft = False
        self.checks_result = [{"id": 1, "name": "pytest", "head_sha": HEAD, "status": "completed", "conclusion": "success"}]
        self.files = [{"filename": "feature.py", "status": "added"}]
        self.review_result = []
        self.merged = []

    def pull_request(self, number):
        return {"state": "open", "merged": False, "draft": self.draft, "mergeable": True,
                "mergeable_state": "clean", "head": {"sha": self.head, "ref": "hikari/engineering/test", "repo": {"full_name": self.repository}},
                "base": {"sha": BASE, "ref": "main"}}

    def checks(self, sha):
        return self.checks_result

    def reviews(self, number):
        return self.review_result

    def changed_files(self, number):
        return self.files

    def _merge_after_gate(self, number, **kwargs):
        self.merged.append((number, kwargs))
        return {"merged": True, "sha": "c" * 40}


def setup_gate(tmp_path):
    remote = Remote()
    evidence = GitHubEvidenceStore(tmp_path / "github_evidence.db")
    evidence.record_owned(remote.repository, 1, session_id="session", head="hikari/engineering/test", base="main")
    evidence.record_physical_gate(remote.repository, 1, HEAD, "Operator verified the actual end-to-end behavior on this head")
    policy_path = tmp_path / "github_policy.json"
    policy_path.write_text(json.dumps({"version": 1, "repositories": {remote.repository: {
        "auto_merge": True, "allowed_bases": ["main"], "required_checks": ["pytest"], "require_physical_gate": True,
    }}}), encoding="utf-8")
    return remote, evidence, policy_path, GitHubMergeGate(remote, evidence, policy_path)


def test_merge_uses_exact_verified_head_and_records_receipt(tmp_path: Path):
    remote, evidence, _, gate = setup_gate(tmp_path)
    assert gate.assess(1)["ready"]
    result = gate.merge(1, expected_head=HEAD)
    assert result["status"] == "merged"
    assert remote.merged == [(1, {"expected_head": HEAD, "method": "squash"})]
    with evidence._connect() as connection:
        assert connection.execute("SELECT merge_sha FROM merge_receipts").fetchone()[0] == "c" * 40


@pytest.mark.parametrize("failure", ["new_head", "pending_check", "stale_check", "review", "authority", "existing_test", "draft", "no_policy", "empty_checks", "not_owned"])
def test_merge_fails_closed_at_each_required_boundary(tmp_path: Path, failure: str):
    remote, evidence, policy_path, gate = setup_gate(tmp_path)
    if failure == "new_head":
        remote.head = "d" * 40
    elif failure == "pending_check":
        remote.checks_result.append({"id": 2, "name": "pytest", "head_sha": HEAD, "status": "in_progress", "conclusion": None})
    elif failure == "stale_check":
        remote.checks_result[0]["head_sha"] = "d" * 40
    elif failure == "review":
        remote.review_result = [{"user": {"login": "reviewer"}, "state": "CHANGES_REQUESTED"}]
    elif failure == "authority":
        remote.files = [{"filename": ".github/workflows/test.yml", "status": "modified"}]
    elif failure == "existing_test":
        remote.files = [{"filename": "tests/test_existing.py", "status": "modified"}]
    elif failure == "draft":
        remote.draft = True
    elif failure == "no_policy":
        policy_path.unlink()
    elif failure == "empty_checks":
        policy = json.loads(policy_path.read_text())
        policy["repositories"][remote.repository]["required_checks"] = []
        policy_path.write_text(json.dumps(policy))
    elif failure == "not_owned":
        with evidence._connect() as connection:
            connection.execute("DELETE FROM owned_prs")
    assert not gate.assess(1)["ready"]
    with pytest.raises(GitHubError):
        gate.merge(1, expected_head=HEAD)
    assert remote.merged == []


def test_readonly_evidence_does_not_create_state(tmp_path: Path):
    path = tmp_path / "missing" / "evidence.db"
    store = GitHubEvidenceStore(path, read_only=True)
    assert store.owned("owner/hikari", 1) is None
    assert not store.has_physical_gate("owner/hikari", 1, HEAD)
    assert not path.parent.exists()


def test_remote_mutations_reject_unscoped_branch_and_path():
    client = GitHubClient("owner/hikari")
    with pytest.raises(ValueError):
        client.create_branch("main", HEAD)
    with pytest.raises(ValueError):
        client.write_file("../escape", "content", branch="hikari/engineering/test", message="change")
