from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
from urllib.parse import quote


class GitHubError(RuntimeError):
    pass


class GitHubOutcomeUnknown(GitHubError):
    """A mutating request may have reached GitHub; it must not be replayed."""


def validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库必须为 owner/name")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise ValueError("GitHub 仓库名不正确")
    return repository


def repository_from_origin(path: Path) -> str:
    try:
        result = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                                capture_output=True, text=True, encoding="utf-8", timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        raise GitHubError("无法读取 GitHub origin，请检查本机仓库或指定配置") from None
    remote = result.stdout.strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", remote)
    if result.returncode or not match:
        raise GitHubError("仓库没有可识别的 GitHub origin，请在配置中指定仓库")
    return validate_repository(match.group(1))


class GitHubClient:
    """A repository-scoped API adapter, never a model-supplied shell command.

    Methods build fixed GitHub API endpoints. Mutations are exposed only through
    the action service and exact-head merge gate.
    """

    def __init__(self, repository: str, *, environment: dict[str, str] | None = None, timeout: float = 15):
        self.repository = validate_repository(repository)
        self.environment = dict(os.environ if environment is None else environment)
        self.timeout = timeout

    def _request(self, resource: str = "", *, method: str = "GET", payload: dict | None = None, raw: bool = False):
        executable = shutil.which("gh", path=self.environment.get("PATH"))
        if not executable:
            raise GitHubError("找不到 GitHub CLI，请先安装 gh 并完成本机登录")
        endpoint = f"repos/{self.repository}" + (f"/{resource}" if resource else "")
        try:
            argv = [executable, "api", "--hostname", "github.com", "--method", method, endpoint]
            if raw:
                # gh refuses job logs containing terminal controls unless opted in.
                # Accept the transport bytes, then remove controls before display.
                argv.append("--allow-escape-sequences")
            if payload is not None:
                argv.extend(["--input", "-"])
            result = subprocess.run(argv, input=json.dumps(payload) if payload is not None else None,
                                    env=self.environment, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            error = GitHubOutcomeUnknown if method != "GET" else GitHubError
            raise error(f"GitHub 请求未完成：{type(exc).__name__}") from None
        if result.returncode:
            error = GitHubOutcomeUnknown if method != "GET" else GitHubError
            raise error(f"GitHub 访问失败（退出码 {result.returncode}），请检查 gh 登录及该仓库的访问权限")
        if len(result.stdout) > 4 * 1024 * 1024:
            raise GitHubError("GitHub 响应超过当前读取上限")
        if raw:
            clean = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])", "", result.stdout)
            return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", clean)
        if not result.stdout.strip():
            return {}
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise GitHubError("GitHub 返回了无法读取的响应") from None

    def _get(self, resource: str = ""):
        return self._request(resource)

    @staticmethod
    def _branch(branch: str) -> str:
        if (not isinstance(branch, str) or not branch.startswith("hikari/engineering/")
                or not re.fullmatch(r"[A-Za-z0-9_./-]+", branch) or ".." in branch
                or branch.endswith(("/", ".", ".lock"))):
            raise ValueError("写入仅限 Hikari 的普通 engineering 分支")
        return branch

    @staticmethod
    def _sha(sha: str) -> str:
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40}", sha):
            raise ValueError("需要完整的 Git SHA")
        return sha

    def create_branch(self, branch: str, sha: str) -> dict:
        return self._request("git/refs", method="POST", payload={"ref": "refs/heads/" + self._branch(branch), "sha": self._sha(sha)})

    def write_file(self, path: str, content: str, *, branch: str, message: str, expected_blob_sha: str | None = None) -> dict:
        self._branch(branch)
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in path:
            raise ValueError("文件路径必须在仓库内部")
        if not isinstance(content, str) or len(content.encode("utf-8")) > 1_000_000:
            raise ValueError("写入内容必须是 1 MB 以内的文本")
        if not message.strip():
            raise ValueError("提交说明不能为空")
        body = {"message": message, "content": base64.b64encode(content.encode("utf-8")).decode(), "branch": branch}
        if expected_blob_sha is not None:
            body["sha"] = self._sha(expected_blob_sha)
        return self._request(f"contents/{quote(path, safe='/')}", method="PUT", payload=body)

    def create_pull_request(self, *, head: str, base: str, title: str, body: str) -> dict:
        self._branch(head)
        if not base.strip() or not title.strip():
            raise ValueError("PR 需要目标分支与标题")
        return self._request("pulls", method="POST", payload={"head": head, "base": base, "title": title, "body": body, "draft": True})

    def update_pull_request(self, number: int, *, title: str | None = None, body: str | None = None) -> dict:
        current = self.pull_request(number)
        self._branch(current["head"]["ref"])
        data = {}
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        if not data:
            raise ValueError("没有要更新的 PR 内容")
        return self._request(f"pulls/{number}", method="PATCH", payload=data)

    def checks(self, sha: str) -> list[dict]:
        data = self._get(f"commits/{self._sha(sha)}/check-runs?per_page=100")
        if data.get("total_count", 0) > 100:
            raise GitHubError("检查数量超过当前完整读取上限，不能据此放行合并")
        return data.get("check_runs", [])

    def reviews(self, number: int) -> list[dict]:
        self.pull_request(number)
        data = self._get(f"pulls/{number}/reviews?per_page=100")
        if len(data) >= 100:
            raise GitHubError("审查记录需要分页，当前无法确认完整审查状态")
        return data

    def changed_files(self, number: int) -> list[dict]:
        current = self.pull_request(number)
        if current.get("changed_files", 0) > 100:
            raise GitHubError("PR 超过 100 个文件，需人工审查")
        files = self._get(f"pulls/{number}/files?per_page=100")
        if not isinstance(files, list) or type(current.get("changed_files")) is not int or len(files) != current["changed_files"]:
            raise GitHubError("无法确认 PR 修改文件列表完整，取消自动合并")
        return files

    def rerun_workflow(self, run_id: int) -> dict:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("运行 ID 必须为正整数")
        return self._request(f"actions/runs/{run_id}/rerun-failed-jobs", method="POST")

    def workflow_jobs(self, run_id: int) -> dict:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("运行 ID 必须为正整数")
        return self._get(f"actions/runs/{run_id}/jobs?per_page=100")

    def workflow_run(self, run_id: int) -> dict:
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise ValueError("运行 ID 必须为正整数")
        return self._get(f"actions/runs/{run_id}")

    def mark_ready(self, number: int, *, expected_head: str) -> dict:
        current = self.pull_request(number)
        if current["head"]["sha"] != self._sha(expected_head):
            raise GitHubError("草稿转为待审查前 head 已变化")
        self._branch(current["head"]["ref"])
        node_id = current.get("node_id")
        if not isinstance(node_id, str) or not node_id:
            raise GitHubError("缺少 PR 的 GitHub 节点标识")
        executable = shutil.which("gh", path=self.environment.get("PATH"))
        if not executable:
            raise GitHubError("找不到 GitHub CLI")
        query = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{isDraft}}}"
        try:
            response = subprocess.run(
                [executable, "api", "--hostname", "github.com", "graphql", "--input", "-"],
                input=json.dumps({"query": query, "variables": {"id": node_id}}), env=self.environment,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=self.timeout)
            value = json.loads(response.stdout)
            if response.returncode or value.get("errors") or value["data"]["markPullRequestReadyForReview"]["pullRequest"]["isDraft"] is not False:
                raise GitHubOutcomeUnknown("GitHub 未确认草稿已转为待审查")
        except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
            raise GitHubOutcomeUnknown("无法确认草稿转为待审查的结果") from None
        return {"draft": False}

    def job_log(self, job_id: int) -> str:
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id <= 0:
            raise ValueError("Job ID 必须为正整数")
        return self._request(f"actions/jobs/{job_id}/logs", raw=True)

    def _merge_after_gate(self, number: int, *, expected_head: str, method: str = "squash") -> dict:
        if method not in {"squash", "merge", "rebase"}:
            raise ValueError("不支持的合并方式")
        return self._request(f"pulls/{number}/merge", method="PUT",
                             payload={"sha": self._sha(expected_head), "merge_method": method})

    def repository_info(self) -> dict:
        data = self._get()
        return {key: data.get(key) for key in ("full_name", "description", "default_branch", "private", "html_url", "permissions")}

    def pull_requests(self, *, state: str = "open") -> list[dict]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("invalid PR state")
        data = self._get(f"pulls?state={state}&per_page=30")
        return [{"number": p["number"], "title": p["title"], "state": p["state"],
                 "draft": p["draft"], "head": p["head"]["ref"], "base": p["base"]["ref"],
                 "head_sha": p["head"]["sha"], "url": p["html_url"], "updated_at": p["updated_at"]} for p in data]

    def workflow_runs(self) -> list[dict]:
        data = self._get("actions/runs?per_page=20")
        return [{"id": r["id"], "name": r["name"], "branch": r["head_branch"], "sha": r["head_sha"],
                 "status": r["status"], "conclusion": r["conclusion"], "url": r["html_url"],
                 "updated_at": r["updated_at"]} for r in data.get("workflow_runs", [])]

    def pull_request(self, number: int) -> dict:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError("PR number must be a positive integer")
        return self._get(f"pulls/{number}")

    def read_file(self, path: str, *, ref: str) -> dict:
        relative = PurePosixPath(path)
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or "\\" in path:
            raise ValueError("文件路径必须在仓库内部")
        if not ref or any(ch in ref for ch in "\r\n\x00"):
            raise ValueError("需要明确的分支或提交")
        data = self._get(f"contents/{quote(path, safe='/')}?ref={quote(ref, safe='')}")
        if isinstance(data, list):
            return {"type": "directory", "entries": [{"name": r["name"], "path": r["path"], "type": r["type"]} for r in data]}
        if data.get("encoding") != "base64" or data.get("size", 0) > 1_000_000:
            raise GitHubError("目前只读取 1 MB 以内的文本文件")
        try:
            content = base64.b64decode(data["content"]).decode("utf-8")
        except (ValueError, UnicodeError):
            raise GitHubError("目标不是可读取的 UTF-8 文本") from None
        return {"type": "file", "path": data["path"], "sha": data["sha"], "content": content, "ref": ref}
