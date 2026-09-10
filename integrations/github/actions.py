"""Repository-scoped conversational GitHub effects with durable provenance.

No policy or evidence-authoring operations are part of the model-visible catalog.
The caller supplies trusted conversation/source identity, never model arguments.
"""
from __future__ import annotations

from copy import deepcopy
import os
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from .client import GitHubClient, GitHubError, GitHubOutcomeUnknown, repository_from_origin, validate_repository
from .governance import GitHubEvidenceStore, GitHubMergeGate, GitHubPolicyStore, protected_path


def _schema(required: tuple[str, ...] = (), **properties) -> dict:
    return {"type": "object", "properties": {"repository": {"type": "string", "description": "已配置允许的 owner/name，省略使用默认仓库"}, **properties},
            "required": list(required), "additionalProperties": False}


TEXT = {"type": "string"}
NONEMPTY = {"type": "string", "minLength": 1}
POSITIVE = {"type": "integer", "minimum": 1}
SHA = {"type": "string", "pattern": "^[0-9a-fA-F]{40}$"}


_ACTIONS = {
    "list_prs": ("read", "列出仓库 PR（最多 30 条）", _schema(state={"type": "string", "enum": ["open", "closed", "all"]})),
    "read_pr": ("read", "读取一个 PR 的 GitHub 当前记录", _schema(("number",), number=POSITIVE)),
    "list_runs": ("read", "读取最近的工作流运行（最多 20 条）", _schema()),
    "read_file": ("read", "读取明确分支或提交上的 UTF-8 文件", _schema(("path", "ref"), path=NONEMPTY, ref=NONEMPTY)),
    "jobs": ("read", "读取工作流运行的任务及结果", _schema(("run_id",), run_id=POSITIVE)),
    "logs": ("read", "读取一个工作流任务日志", _schema(("job_id",), job_id=POSITIVE)),
    "create_branch": ("write", "从完整提交 SHA 创建 hikari/engineering/ 分支", _schema(("branch", "sha"), branch=NONEMPTY, sha=SHA)),
    "write_file": ("write", "在 Hikari engineering 分支提交普通代码或新增测试", _schema(("path", "content", "branch", "message"), path=NONEMPTY, content=TEXT, branch=NONEMPTY, message=NONEMPTY, expected_blob_sha=SHA)),
    "create_pr": ("write", "为 Hikari engineering 分支创建草稿 PR 并记录创建凭据", _schema(("head", "base", "title", "body"), head=NONEMPTY, base=NONEMPTY, title=NONEMPTY, body=TEXT)),
    "update_pr": ("write", "更新具有本机 Hikari 创建凭据的 PR 标题或正文", _schema(("number",), number=POSITIVE, title=NONEMPTY, body=TEXT)),
    "rerun_failed": ("write", "请求重跑 Hikari engineering 分支中已失败运行的失败任务", _schema(("run_id",), run_id=POSITIVE)),
    "merge_pr": ("write", "按独立操作人策略验证当前 head，满足条件后将草稿转为待审查并合并", _schema(("number", "expected_head"), number=POSITIVE, expected_head=SHA)),
}


class GitHubActionBlocked(ValueError):
    def __init__(self, message: str, *, code: str, **details):
        super().__init__(message)
        self.blocker = {"code": code, **details}


class GitHubActionService:
    def __init__(self, project_root: Path, *, state_dir: Path | None = None,
                 repositories: Iterable[str] | None = None,
                 client_factory: Callable | None = None,
                 environment: dict[str, str] | None = None):
        self.project_root = Path(project_root).resolve()
        self.state_dir = Path(state_dir).resolve() if state_dir is not None else self.project_root / "state"
        self.policy_path = self.state_dir / "github_policy.json"
        self.environment = dict(os.environ if environment is None else environment)
        self._client_factory = client_factory or (lambda repository: GitHubClient(repository, environment=self.environment))
        self._configuration_error = ""
        try:
            configured_default = self.environment.get("HIKARI_GITHUB_REPOSITORY", "").strip()
            if repositories is None:
                configured = self.environment.get("HIKARI_GITHUB_ALLOWED_REPOSITORIES", "")
                repositories = [value.strip() for value in configured.split(",") if value.strip()] if configured.strip() else None
            allowed = tuple(validate_repository(value) for value in repositories) if repositories is not None else ()
            default = validate_repository(configured_default) if configured_default else (allowed[0] if allowed else repository_from_origin(self.project_root))
            allowed = allowed or (default,)
            if default.casefold() not in {repo.casefold() for repo in allowed}:
                raise ValueError("默认 GitHub 仓库不在允许列表中")
            self.repository = next(repo for repo in allowed if repo.casefold() == default.casefold())
            self.repositories = allowed
        except (ValueError, GitHubError, OSError) as exc:
            self.repository, self.repositories = "", ()
            self._configuration_error = str(exc)

    @staticmethod
    def catalog() -> list[dict]:
        return [{"name": name, "action": name, "effect": effect, "description": description, "parameters": deepcopy(parameters)}
                for name, (effect, description, parameters) in _ACTIONS.items()]

    @staticmethod
    def _validate(action: str, arguments: dict) -> dict:
        if action not in _ACTIONS:
            raise ValueError("未注册的 GitHub 操作；部署、权限和授权策略只能由用户操作")
        if not isinstance(arguments, dict):
            raise ValueError("GitHub 操作参数必须是对象")
        schema = _ACTIONS[action][2]
        if set(arguments) - set(schema["properties"]) or set(schema["required"]) - set(arguments):
            raise ValueError("GitHub 操作参数包含未知字段或缺少必需字段")
        for key, value in arguments.items():
            rule = schema["properties"][key]
            if rule["type"] == "string" and (not isinstance(value, str) or (rule.get("minLength") and not value.strip())):
                raise ValueError(f"{key} 必须为有效文本")
            if rule["type"] == "integer" and (type(value) is not int or value < 1):
                raise ValueError(f"{key} 必须为正整数")
            if "enum" in rule and value not in rule["enum"]:
                raise ValueError(f"{key} 不在支持的取值范围")
            if rule is SHA or rule.get("pattern"):
                GitHubClient._sha(value)
        if action == "update_pr" and not ({"title", "body"} & set(arguments)):
            raise ValueError("没有要更新的 PR 内容")
        for key in ("branch", "head"):
            if key in arguments:
                GitHubClient._branch(arguments[key])
        if "path" in arguments:
            path = arguments["path"]
            relative = PurePosixPath(path)
            if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in path or any(ch in path for ch in "\x00\r\n"):
                raise ValueError("文件路径必须在仓库内部")
        if action == "write_file":
            status = "modified" if arguments.get("expected_blob_sha") else "added"
            if protected_path(arguments["path"], status=status):
                raise ValueError("权限、部署、验收或策略路径需要用户直接处理")
            if len(arguments["content"].encode("utf-8")) > 1_000_000:
                raise ValueError("文件写入超过 1 MB 限制")
        return dict(arguments)

    def handle(self, action: str, arguments: dict, source_ref: str, conversation_id: str) -> dict:
        result = {"status": "blocked", "action": action, "repository": "", "source_ref": source_ref,
                  "conversation_id": conversation_id, "data": None, "error": None}
        evidence = None
        claimed = False
        effect_started = False
        try:
            values = self._validate(action, arguments)
            if self._configuration_error:
                raise ValueError(self._configuration_error)
            requested = values.pop("repository", self.repository)
            validate_repository(requested)
            repository = next((repo for repo in self.repositories if repo.casefold() == requested.casefold()), None)
            if repository is None:
                raise ValueError("该 GitHub 仓库不在本机允许列表中")
            result["repository"] = repository
            client = self._client_factory(repository)
            if _ACTIONS[action][0] == "read":
                result.update(status="ok", data=self._read(client, action, values))
                return result
            if not isinstance(source_ref, str) or not source_ref.strip() or not isinstance(conversation_id, str) or not conversation_id.strip():
                raise ValueError("GitHub 写操作需要运行时提供不可变来源和对话标识")
            evidence = GitHubEvidenceStore(self.state_dir / "github_evidence.db")
            receipt = evidence.claim_action(source_ref=source_ref, conversation_id=conversation_id,
                                            action=action, repository=repository, arguments=values)
            if not receipt["claimed"]:
                if receipt["result"] is not None:
                    return {**receipt["result"], "replayed": True}
                result.update(status="unknown", error="同一来源已有未确认的外部操作；不会自动重试，请先核实 GitHub 当前状态", receipt={"status": receipt["status"], "source_ref": source_ref})
                return result
            claimed = True
            self._preflight(client, evidence, action, values)
            effect_started = True
            data = self._write(client, evidence, action, values, conversation_id)
            result.update(status="completed", data=data)
        except Exception as exc:
            result.update(status="unknown" if effect_started else "blocked", error=str(exc) or type(exc).__name__)
            if isinstance(exc, GitHubActionBlocked):
                result["blocker"] = exc.blocker
        if claimed:
            result["receipt"] = {"status": result["status"], "source_ref": source_ref, "conversation_id": conversation_id}
            try:
                evidence.finish_action(source_ref, conversation_id, result)
            except Exception:
                result.update(status="unknown", error="外部操作可能已发生，但本地结果凭据未能完成；请核实 GitHub，不能自动重试")
                result["receipt"]["status"] = "pending"
        return result

    @staticmethod
    def _read(client, action, values):
        if action == "list_prs":
            return {"pull_requests": client.pull_requests(**values), "limit": 30}
        if action == "read_pr":
            return client.pull_request(values["number"])
        if action == "list_runs":
            return {"workflow_runs": client.workflow_runs(), "limit": 20}
        if action == "read_file":
            return client.read_file(values["path"], ref=values["ref"])
        if action == "jobs":
            return client.workflow_jobs(values["run_id"])
        if action == "logs":
            return {"job_id": values["job_id"], "content": client.job_log(values["job_id"]), "untrusted_content": True}
        raise ValueError("未注册的读取操作")

    def _preflight(self, client, evidence, action, values):
        if action == "update_pr":
            current = client.pull_request(values["number"])
            owned = evidence.owned(client.repository, values["number"])
            if not owned or owned["head"] != current["head"]["ref"] or owned["base"] != current["base"]["ref"] or (current["head"].get("repo") or {}).get("full_name", "").casefold() != client.repository.casefold():
                raise ValueError("只能更新拥有 Hikari 创建凭据且分支一致的 PR")
        elif action == "rerun_failed":
            run = client.workflow_run(values["run_id"])
            GitHubClient._branch(run.get("head_branch"))
            if run.get("status") != "completed" or run.get("conclusion") not in {"failure", "timed_out"}:
                raise ValueError("只能重跑 Hikari engineering 分支中已经失败的运行")
            policy = GitHubPolicyStore(self.policy_path).load()["document"]["repositories"].get(client.repository, {})
            approved = policy.get("rerun_workflows", {})
            path = run.get("path", "")
            if run.get("event") not in {"pull_request", "push"}:
                raise GitHubActionBlocked("只支持操作人授权的普通 PR 或 push 检查工作流", code="workflow_event_not_allowed", event=run.get("event"))
            if not isinstance(path, str) or not path.startswith(".github/workflows/") or ".." in path or not path.endswith((".yml", ".yaml")):
                raise GitHubActionBlocked("无法确认工作流的仓库文件路径", code="workflow_path_unknown")
            sha = GitHubClient._sha(run.get("head_sha"))
            workflow = client.read_file(path, ref=sha)
            observed_blob = GitHubClient._sha(workflow.get("sha"))
            if observed_blob != approved.get(path):
                raise GitHubActionBlocked("工作流尚未获得操作人授权或内容与授权版本不一致", code="workflow_pin_required",
                                         repository=client.repository, workflow_path=path, observed_blob_sha=observed_blob,
                                         observed_head_sha=sha, configured_blob_sha=approved.get(path))
        elif action == "merge_pr":
            assessment = GitHubMergeGate(client, evidence, self.policy_path).assess(values["number"])
            if assessment["head_sha"] != values["expected_head"] or not (assessment["ready"] or assessment["eligible_for_ready"]):
                raise ValueError("合并条件未满足：" + "；".join(c["reason"] for c in assessment["conditions"] if not c["passed"]))

    def _write(self, client, evidence, action, values, conversation_id):
        if action == "create_branch":
            data = client.create_branch(**values)
            if data.get("ref") != "refs/heads/" + values["branch"] or data.get("object", {}).get("sha") != values["sha"]:
                raise GitHubOutcomeUnknown("GitHub 未确认目标分支及提交")
            return data
        if action == "write_file":
            data = client.write_file(**values)
            GitHubClient._sha(data.get("commit", {}).get("sha"))
            GitHubClient._sha((data.get("content") or {}).get("sha"))
            return data
        if action == "create_pr":
            data = client.create_pull_request(**values)
            if (type(data.get("number")) is not int or data["number"] < 1 or data.get("head", {}).get("ref") != values["head"]
                    or data.get("base", {}).get("ref") != values["base"] or data.get("draft") is not True
                    or (data.get("head", {}).get("repo") or {}).get("full_name", "").casefold() != client.repository.casefold()):
                raise GitHubOutcomeUnknown("GitHub 未确认 Hikari 创建的草稿 PR，未写入所有权凭据")
            evidence.record_owned(client.repository, data["number"], session_id=conversation_id, head=values["head"], base=values["base"])
            return data
        if action == "update_pr":
            data = client.update_pull_request(**values)
            if data.get("number") != values["number"] or any(data.get(key) != values[key] for key in ("title", "body") if key in values):
                raise GitHubOutcomeUnknown("GitHub 未确认 PR 更新内容")
            return data
        if action == "rerun_failed":
            client.rerun_workflow(values["run_id"])
            return {"run_id": values["run_id"], "status": "rerun_requested", "conclusion": None}
        if action == "merge_pr":
            return GitHubMergeGate(client, evidence, self.policy_path).merge(values["number"], expected_head=values["expected_head"])
        raise ValueError("未注册的写入操作")
