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


def validate_repository(repository: str) -> str:
    if not isinstance(repository, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("GitHub 仓库必须为 owner/name")
    if any(part in {".", ".."} for part in repository.split("/")):
        raise ValueError("GitHub 仓库名不正确")
    return repository


def repository_from_origin(path: Path) -> str:
    result = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"],
                            capture_output=True, text=True, encoding="utf-8", timeout=5)
    remote = result.stdout.strip()
    match = re.fullmatch(r"(?:https://github\.com/|git@github\.com:)([^/]+/[^/]+?)(?:\.git)?", remote)
    if result.returncode or not match:
        raise GitHubError("仓库没有可识别的 GitHub origin，请在配置中指定仓库")
    return validate_repository(match.group(1))


class GitHubClient:
    """A repository-scoped API adapter, never a model-supplied shell command.

    Methods build fixed GitHub API endpoints. This first surface is read-only;
    authorized mutations and exact-head merge gates are separate runtime effects.
    """

    def __init__(self, repository: str, *, environment: dict[str, str] | None = None, timeout: float = 15):
        self.repository = validate_repository(repository)
        self.environment = dict(os.environ if environment is None else environment)
        self.timeout = timeout

    def _get(self, resource: str = ""):
        executable = shutil.which("gh", path=self.environment.get("PATH"))
        if not executable:
            raise GitHubError("找不到 GitHub CLI，请先安装 gh 并完成本机登录")
        endpoint = f"repos/{self.repository}" + (f"/{resource}" if resource else "")
        try:
            result = subprocess.run([executable, "api", "--hostname", "github.com", "--method", "GET", endpoint],
                                    env=self.environment, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"GitHub 请求未完成：{type(exc).__name__}") from None
        if result.returncode:
            raise GitHubError(f"GitHub 访问失败（退出码 {result.returncode}），请检查 gh 登录及该仓库的访问权限")
        if len(result.stdout) > 4 * 1024 * 1024:
            raise GitHubError("GitHub 响应超过当前读取上限")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise GitHubError("GitHub 返回了无法读取的响应") from None

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
