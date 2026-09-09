"""Operator-owned policy and machine-owned PR receipts outside candidate worktrees."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import sqlite3
import time

from .client import GitHubClient, GitHubError, validate_repository


AUTHORITY_PATHS = (
    ".github/workflows/*", "core/delegation.py", "core/capabilities.py",
    "engineering/worker.py", "engineering/session.py", "engineering/validation_policy.py",
    "engineering/github_publish.py", "integrations/github/*", "dashboard/settings.py",
    "dashboard/app.py", "resident/environment*.py", ".codex/*", "AGENTS.md",
    "**/conftest.py", "conftest.py", "pyproject.toml", "uv.lock",
)


@dataclass(frozen=True)
class MergePolicy:
    repository: str
    allowed_bases: tuple[str, ...]
    required_checks: tuple[str, ...]
    enabled: bool = False
    require_physical_gate: bool = True
    method: str = "squash"

    @classmethod
    def load(cls, path: Path, repository: str):
        validate_repository(repository)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(repository, (), ())
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("GitHub 授权配置不可读取")
        policy = value.get("repositories", {}).get(repository)
        if policy is None:
            return cls(repository, (), ())
        if (not isinstance(policy, dict) or not isinstance(policy.get("allowed_bases"), list)
                or not isinstance(policy.get("required_checks"), list)
                or any(not isinstance(v, str) or not v.strip() for v in policy["allowed_bases"] + policy["required_checks"])
                or not isinstance(policy.get("auto_merge", False), bool)
                or not isinstance(policy.get("require_physical_gate", True), bool)):
            raise ValueError("GitHub 自动合并配置格式不正确")
        method = policy.get("method", "squash")
        if method not in {"squash", "merge", "rebase"}:
            raise ValueError("GitHub 合并方式不正确")
        return cls(repository, tuple(policy["allowed_bases"]), tuple(policy["required_checks"]),
                   policy.get("auto_merge", False), policy.get("require_physical_gate", True), method)


class GitHubEvidenceStore:
    def __init__(self, path: Path, *, read_only: bool = False):
        self.path = Path(path)
        self.read_only = read_only
        if read_only:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS owned_prs(
                repository TEXT, number INTEGER, session_id TEXT NOT NULL,
                head TEXT NOT NULL, base TEXT NOT NULL, created_at REAL NOT NULL,
                PRIMARY KEY(repository, number))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS physical_gates(
                repository TEXT, number INTEGER, head_sha TEXT, evidence TEXT NOT NULL,
                verified_at REAL NOT NULL, PRIMARY KEY(repository, number, head_sha))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS merge_receipts(
                repository TEXT, number INTEGER, head_sha TEXT, merge_sha TEXT NOT NULL,
                completed_at REAL NOT NULL, PRIMARY KEY(repository, number, head_sha))""")

    def _connect(self):
        connection = (sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
                      if self.read_only else sqlite3.connect(self.path, timeout=10))
        connection.row_factory = sqlite3.Row
        return connection

    def record_owned(self, repository, number, *, session_id, head, base):
        GitHubClient._branch(head)
        with self._connect() as connection:
            current = connection.execute("SELECT * FROM owned_prs WHERE repository=? AND number=?", (repository, number)).fetchone()
            if current and (current["session_id"], current["head"], current["base"]) != (session_id, head, base):
                raise ValueError("PR 已存在不同的所有权凭据")
            connection.execute("INSERT OR IGNORE INTO owned_prs VALUES (?,?,?,?,?,?)", (repository, number, session_id, head, base, time.time()))

    def owned(self, repository, number):
        if not self.path.is_file():
            return None
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM owned_prs WHERE repository=? AND number=?", (repository, number)).fetchone()
        return dict(row) if row else None

    def record_physical_gate(self, repository, number, sha, evidence):
        GitHubClient._sha(sha)
        if not isinstance(evidence, str) or len(evidence.strip()) < 10:
            raise ValueError("需要具体的真实验收证据")
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO physical_gates VALUES (?,?,?,?,?)", (repository, number, sha, evidence.strip(), time.time()))

    def has_physical_gate(self, repository, number, sha):
        if not self.path.is_file():
            return False
        with self._connect() as connection:
            return connection.execute("SELECT 1 FROM physical_gates WHERE repository=? AND number=? AND head_sha=?", (repository, number, sha)).fetchone() is not None

    def record_merge(self, repository, number, head_sha, merge_sha):
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO merge_receipts VALUES (?,?,?,?,?)", (repository, number, head_sha, merge_sha, time.time()))


class GitHubMergeGate:
    def __init__(self, client: GitHubClient, evidence: GitHubEvidenceStore, policy_path: Path):
        self.client, self.evidence, self.policy_path = client, evidence, Path(policy_path)

    def assess(self, number: int) -> dict:
        repository = self.client.repository
        policy = MergePolicy.load(self.policy_path, repository)
        pr = self.client.pull_request(number)
        head = pr["head"]["sha"]
        conditions = []
        def check(key, passed, reason):
            conditions.append({"key": key, "passed": bool(passed), "reason": ("已满足：" if passed else "未满足：") + reason})
        check("operator_policy", policy.enabled, "操作人已启用该仓库的条件化自动合并")
        check("allowed_base", pr["base"]["ref"] in policy.allowed_bases, "目标分支在独立授权配置中")
        ownership = self.evidence.owned(repository, number)
        check("owned_pr", ownership and ownership["head"] == pr["head"]["ref"] and ownership["base"] == pr["base"]["ref"], "具有 Hikari 创建的持久 PR 所有权凭据")
        check("same_repository", pr["head"].get("repo", {}).get("full_name") == repository, "来源为同一授权仓库")
        check("open", pr.get("state") == "open" and not pr.get("merged"), "PR 仍开放且未合并")
        check("ready", not pr.get("draft"), "PR 已从草稿进入待合并状态")
        check("mergeable", pr.get("mergeable") is True and pr.get("mergeable_state") == "clean", "GitHub 已确认可无冲突合并")
        checks = self.client.checks(head)
        latest = {}
        for run in sorted(checks, key=lambda item: item.get("id", 0)):
            latest[run["name"]] = run
        check("required_checks_configured", bool(policy.required_checks), "已明确列出必需检查，不能凭空集合放行")
        for name in policy.required_checks:
            run = latest.get(name, {})
            check("check:" + name, run.get("head_sha") == head and run.get("status") == "completed" and run.get("conclusion") == "success", name + " 在当前 head 上通过")
        latest_reviews = {}
        for review in self.client.reviews(number):
            if review.get("state") in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest_reviews[review.get("user", {}).get("login", "unknown")] = review["state"]
        check("review", "CHANGES_REQUESTED" not in latest_reviews.values(), "没有尚未解决的阻塞审查")
        changes = self.client.changed_files(number)
        protected = [item["filename"] for item in changes
                     if any(fnmatch.fnmatchcase(item["filename"], pattern) for pattern in AUTHORITY_PATHS)
                     or (item["filename"].startswith("tests/") and item.get("status") != "added")]
        check("authority_unchanged", not protected, "权限、验收或执行边界修改需要人工决定" + ("：" + ", ".join(protected) if protected else ""))
        if policy.require_physical_gate:
            check("physical_gate", self.evidence.has_physical_gate(repository, number, head), "当前 head 已有真实验收记录")
        return {"repository": repository, "number": number, "head_sha": head,
                "base_sha": pr["base"]["sha"], "ready": all(c["passed"] for c in conditions),
                "conditions": conditions, "method": policy.method, "checked_at": time.time()}

    def merge(self, number: int, *, expected_head: str) -> dict:
        assessment = self.assess(number)
        if assessment["head_sha"] != expected_head:
            raise GitHubError("PR head 已变化，需要重新验证后再合并")
        if not assessment["ready"]:
            raise GitHubError("合并条件未满足：" + "；".join(c["reason"] for c in assessment["conditions"] if not c["passed"]))
        current = self.client.pull_request(number)
        if current["head"]["sha"] != expected_head or current["base"]["sha"] != assessment["base_sha"]:
            raise GitHubError("合并前分支状态发生变化，取消本次操作")
        result = self.client._merge_after_gate(number, expected_head=expected_head, method=assessment["method"])
        if result.get("merged") is not True or not result.get("sha"):
            raise GitHubError("GitHub 没有确认合并成功")
        self.evidence.record_merge(self.client.repository, number, expected_head, result["sha"])
        return {"status": "merged", "merge_sha": result["sha"], "assessment": assessment}
