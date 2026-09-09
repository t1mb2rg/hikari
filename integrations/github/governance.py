"""Operator-owned policy and machine-owned PR receipts outside candidate worktrees."""
from __future__ import annotations

from dataclasses import dataclass
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import time
import uuid

from .client import GitHubClient, GitHubError, GitHubOutcomeUnknown, validate_repository


AUTHORITY_PATHS = (
    "capabilities/operator.py", "resident/file_locks.py", "conversation/github_workflow.py",
    "dashboard/operator_controls.py", "capabilities/runtime.py", "capabilities/growth.py", "capabilities/__init__.py",
    "conversation/claim_guard.py", "conversation/task_store.py", "conversation/task_pump.py", "conversation/bootstrap.py",
    ".github/*", ".gitmodules", ".gitattributes", "core/delegation.py", "core/capabilities.py", "core/action*.py",
    "actions/*", "core/runtime.py", "conversation/task_router.py", "conversation/gateway.py",
    "core/authorization*.py", "core/permission*.py", "core/policy*.py",
    "engineering/worker.py", "engineering/session.py", "engineering/validation_policy.py",
    "engineering/github_publish.py", "integrations/github/*", "dashboard/settings.py",
    "engineering/effects.py", "engineering/*backend*.py", "engineering/workspace.py", "engineering/config.py",
    "engineering/bindings.py", "resident/app.py", "resident/unified.py", "resident/paths.py",
    "dashboard/app.py", "resident/environment*.py", ".codex/*", "AGENTS.md",
    "**/conftest.py", "conftest.py", "pyproject.toml", "uv.lock", "pytest.ini", "tox.ini",
    "setup.cfg", "setup.py", "requirements*.txt", "scripts/*validat*", "scripts/*gate*",
    "state/*", ".env*", "Dockerfile*", "docker-compose*", "deploy/*", "deployment/*",
    "infra/*", "terraform/*", "**/*.tf", "**/CODEOWNERS", "CODEOWNERS",
)


def protected_path(path: str, *, status: str = "modified") -> bool:
    # Also examine previous_filename on renames at callers; fnmatch '*' spans '/'.
    normalized = str(PurePosixPath(path.replace("\\", "/"))).casefold()
    return (any(fnmatch.fnmatchcase(normalized, pattern.casefold()) for pattern in AUTHORITY_PATHS)
            or (normalized.startswith("tests/") and status != "added"))


class GitHubPolicyStore:
    """Only the authenticated operator surface may call save; never a model action."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @staticmethod
    def _validate(value: dict) -> dict:
        if not isinstance(value, dict) or set(value) - {"version", "repositories"}:
            raise ValueError("GitHub 授权配置包含未知字段")
        if type(value.get("version")) is not int or value["version"] != 1 or not isinstance(value.get("repositories"), dict):
            raise ValueError("GitHub 授权配置格式不正确")
        for repository, item in value["repositories"].items():
            validate_repository(repository)
            allowed = {"auto_merge", "allowed_bases", "required_checks", "require_physical_gate", "method", "rerun_workflows"}
            if not isinstance(item, dict) or set(item) - allowed:
                raise ValueError("仓库授权配置包含未知字段")
            for name in ("allowed_bases", "required_checks"):
                entries = item.get(name, [])
                if not isinstance(entries, list) or any(not isinstance(v, str) or not v.strip() or v != v.strip() for v in entries) or len(set(entries)) != len(entries):
                    raise ValueError("目标分支与必需检查应为不重复的名称列表")
            if not isinstance(item.get("auto_merge", False), bool) or not isinstance(item.get("require_physical_gate", True), bool):
                raise ValueError("自动合并和真实验收开关必须为布尔值")
            if item.get("method", "squash") not in {"squash", "merge", "rebase"}:
                raise ValueError("GitHub 合并方式不正确")
            workflows = item.get("rerun_workflows", {})
            if not isinstance(workflows, dict):
                raise ValueError("允许重跑的工作流必须包含路径和完整 blob SHA")
            for path, sha in workflows.items():
                if not isinstance(path, str) or not path.startswith(".github/workflows/") or ".." in path or not path.endswith((".yml", ".yaml")):
                    raise ValueError("工作流路径不正确")
                GitHubClient._sha(sha)
        return value

    def load(self) -> dict:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {"revision": "absent", "document": {"version": 1, "repositories": {}}, "configured": False}
        value = self._validate(json.loads(raw.decode("utf-8")))
        return {"revision": hashlib.sha256(raw).hexdigest(), "document": value, "configured": True}

    def save(self, document: dict, *, expected_revision: str, operator: bool = False) -> dict:
        if operator is not True:
            raise PermissionError("只有操作人设置界面可以修改 GitHub 授权配置")
        self._validate(document)
        if any(item.get("auto_merge", False) and (not item.get("allowed_bases") or not item.get("required_checks")) for item in document["repositories"].values()):
            raise ValueError("启用自动合并前必须明确目标分支和必需检查")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise ValueError("保存配置需要读取时的版本")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_name(self.path.name + ".lock")
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            raise ValueError("授权配置正在保存，请重新读取后重试") from None
        temporary = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            os.close(descriptor)
            if self.load()["revision"] != expected_revision:
                raise ValueError("授权配置已被修改，请重新读取后再保存")
            raw = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            with temporary.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)
            lock.unlink(missing_ok=True)
        return self.load()


@dataclass(frozen=True)
class MergePolicy:
    repository: str
    allowed_bases: tuple[str, ...]
    required_checks: tuple[str, ...]
    enabled: bool = False
    require_physical_gate: bool = True
    method: str = "squash"
    revision: str = "absent"

    @classmethod
    def load(cls, path: Path, repository: str):
        validate_repository(repository)
        snapshot = GitHubPolicyStore(path).load()
        value = snapshot["document"]
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("GitHub 授权配置不可读取")
        policy = value.get("repositories", {}).get(repository)
        if policy is None:
            return cls(repository, (), (), revision=snapshot["revision"])
        if (not isinstance(policy, dict) or not isinstance(policy.get("allowed_bases", []), list)
                or not isinstance(policy.get("required_checks", []), list)
                or any(not isinstance(v, str) or not v.strip() for v in policy.get("allowed_bases", []) + policy.get("required_checks", []))
                or not isinstance(policy.get("auto_merge", False), bool)
                or not isinstance(policy.get("require_physical_gate", True), bool)):
            raise ValueError("GitHub 自动合并配置格式不正确")
        method = policy.get("method", "squash")
        if method not in {"squash", "merge", "rebase"}:
            raise ValueError("GitHub 合并方式不正确")
        return cls(repository, tuple(policy.get("allowed_bases", [])), tuple(policy.get("required_checks", [])),
                   policy.get("auto_merge", False), policy.get("require_physical_gate", True), method, snapshot["revision"])


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
            connection.execute("""CREATE TABLE IF NOT EXISTS action_receipts(
                source_ref TEXT, conversation_id TEXT, action TEXT NOT NULL,
                repository TEXT NOT NULL, arguments_json TEXT NOT NULL, status TEXT NOT NULL,
                result_json TEXT, created_at REAL NOT NULL, completed_at REAL,
                PRIMARY KEY(source_ref, conversation_id))""")

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

    def claim_action(self, *, source_ref, conversation_id, action, repository, arguments):
        """Reserve immutable provenance before an external effect; never replay uncertainty."""
        serialized = json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM action_receipts WHERE source_ref=? AND conversation_id=?", (source_ref, conversation_id)).fetchone()
            if row:
                if (row["action"], row["repository"], row["arguments_json"]) != (action, repository, serialized):
                    raise ValueError("来源凭据已经绑定其他 GitHub 操作，不能替换或扩大操作")
                return {"claimed": False, "status": row["status"], "result": json.loads(row["result_json"]) if row["result_json"] else None}
            connection.execute("INSERT INTO action_receipts VALUES (?,?,?,?,?,'pending',NULL,?,NULL)",
                               (source_ref, conversation_id, action, repository, serialized, time.time()))
            return {"claimed": True, "status": "pending", "result": None}

    def finish_action(self, source_ref, conversation_id, result):
        if result.get("status") not in {"completed", "blocked", "unknown"}:
            raise ValueError("操作凭据只能记录明确完成、拒绝或结果未知")
        serialized = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False)
        with self._connect() as connection:
            updated = connection.execute("UPDATE action_receipts SET status=?,result_json=?,completed_at=? WHERE source_ref=? AND conversation_id=? AND status='pending'",
                                         (result["status"], serialized, time.time(), source_ref, conversation_id)).rowcount
            if updated != 1:
                raise ValueError("操作凭据已经结束，不能更改")


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
        check("historic_gate", not (repository.casefold() == "t1mb2rg/hikari" and number in {77, 78, 79}), "历史 M7 验收 PR 保留由用户决定")
        check("allowed_base", pr["base"]["ref"] in policy.allowed_bases, "目标分支在独立授权配置中")
        ownership = self.evidence.owned(repository, number)
        check("owned_pr", ownership and ownership["head"] == pr["head"]["ref"] and ownership["base"] == pr["base"]["ref"], "具有 Hikari 创建的持久 PR 所有权凭据")
        check("same_repository", (pr["head"].get("repo") or {}).get("full_name", "").casefold() == repository.casefold(), "来源为同一授权仓库")
        check("open", pr.get("state") == "open" and not pr.get("merged"), "PR 仍开放且未合并")
        check("ready", not pr.get("draft"), "PR 已从草稿进入待合并状态")
        check("mergeable", pr.get("mergeable") is True and (pr.get("mergeable_state") == "clean" or (pr.get("draft") is True and pr.get("mergeable_state") == "blocked")), "GitHub 已确认无冲突，草稿转为待审查后再次确认分支保护状态")
        checks = self.client.checks(head)
        latest = {}
        for run in sorted(checks, key=lambda item: item.get("id", 0)):
            latest[run["name"]] = run
        check("required_checks_configured", bool(policy.required_checks), "已明确列出必需检查，不能凭空集合放行")
        for name in policy.required_checks:
            run = latest.get(name, {})
            check("check:" + name, run.get("head_sha") == head and run.get("status") == "completed" and run.get("conclusion") == "success", name + " 在当前 head 上通过")
        latest_reviews = {}
        reviews = sorted(self.client.reviews(number), key=lambda item: (item.get("submitted_at") or "", item.get("id", 0)))
        for review in reviews:
            reviewer = (review.get("user") or {}).get("login") or ("unknown-review:" + str(review.get("id", id(review))))
            state = review.get("state")
            if state == "CHANGES_REQUESTED":
                latest_reviews[reviewer] = state
            elif state == "DISMISSED" or (state == "APPROVED" and review.get("commit_id") == head):
                latest_reviews[reviewer] = state
        check("review", "CHANGES_REQUESTED" not in latest_reviews.values(), "没有尚未解决的阻塞审查")
        changes = self.client.changed_files(number)
        protected = sorted({path for item in changes for path in (item["filename"], item.get("previous_filename"))
                            if path and protected_path(path, status=item.get("status", "modified"))})
        check("authority_unchanged", not protected, "权限、验收或执行边界修改需要人工决定" + ("：" + ", ".join(protected) if protected else ""))
        if policy.require_physical_gate:
            check("physical_gate", self.evidence.has_physical_gate(repository, number, head), "当前 head 已有真实验收记录")
        current = self.client.pull_request(number)
        check("snapshot_stable", current["head"]["sha"] == head and current["base"]["sha"] == pr["base"]["sha"]
              and current["base"]["ref"] == pr["base"]["ref"] and current["head"]["ref"] == pr["head"]["ref"]
              and current.get("state") == "open" and not current.get("merged") and current.get("draft") == pr.get("draft"), "审查期间 PR 的 head、目标分支和状态未变化")
        return {"repository": repository, "number": number, "head_sha": head,
                "base_sha": pr["base"]["sha"], "head_ref": pr["head"]["ref"], "base_ref": pr["base"]["ref"], "ready": all(c["passed"] for c in conditions),
                "eligible_for_ready": pr.get("draft") is True and all(c["passed"] for c in conditions if c["key"] != "ready"),
                "conditions": conditions, "method": policy.method, "policy_revision": policy.revision, "checked_at": time.time()}

    def merge(self, number: int, *, expected_head: str) -> dict:
        assessment = self.assess(number)
        if assessment["head_sha"] != expected_head:
            raise GitHubError("PR head 已变化，需要重新验证后再合并")
        if assessment["eligible_for_ready"]:
            if not callable(getattr(self.client, "mark_ready", None)):
                raise GitHubError("当前 GitHub 适配器不能确认草稿转为待审查")
            original_base = assessment["base_sha"]
            original_refs = (assessment["head_ref"], assessment["base_ref"])
            original_policy = assessment["policy_revision"]
            if GitHubPolicyStore(self.policy_path).load()["revision"] != original_policy:
                raise GitHubError("授权配置在验收后已变化，请重新验证")
            self.client.mark_ready(number, expected_head=expected_head)
            assessment = self.assess(number)
            if (assessment["head_sha"] != expected_head or assessment["base_sha"] != original_base
                    or (assessment["head_ref"], assessment["base_ref"]) != original_refs or assessment["policy_revision"] != original_policy):
                raise GitHubError("草稿转为待审查期间分支状态变化，需要重新验证")
        if not assessment["ready"]:
            raise GitHubError("合并条件未满足：" + "；".join(c["reason"] for c in assessment["conditions"] if not c["passed"]))
        current = self.client.pull_request(number)
        if (current["head"]["sha"] != expected_head or current["base"]["sha"] != assessment["base_sha"]
                or current["head"]["ref"] != assessment["head_ref"] or current["base"]["ref"] != assessment["base_ref"]
                or (current["head"].get("repo") or {}).get("full_name", "").casefold() != self.client.repository.casefold()):
            raise GitHubError("合并前分支状态发生变化，取消本次操作")
        if current.get("draft") is not False or current.get("state") != "open" or current.get("merged") or current.get("mergeable") is not True or current.get("mergeable_state") != "clean":
            raise GitHubError("合并前 PR 状态或分支保护条件发生变化")
        if GitHubPolicyStore(self.policy_path).load()["revision"] != assessment["policy_revision"]:
            raise GitHubError("合并前操作人授权配置发生变化，取消本次操作")
        result = self.client._merge_after_gate(number, expected_head=expected_head, method=assessment["method"])
        if result.get("merged") is not True or not result.get("sha"):
            raise GitHubOutcomeUnknown("GitHub 没有确认合并成功")
        self.evidence.record_merge(self.client.repository, number, expected_head, result["sha"])
        return {"status": "merged", "merge_sha": result["sha"], "assessment": assessment}
