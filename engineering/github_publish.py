from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Mapping, Sequence


_HIKARI_DRAFT_MARKER = "<!-- hikari-engineering-draft-pr -->"


@dataclass(frozen=True, slots=True)
class DraftPullRequestResult:
    action: str
    number: int
    url: str
    head: str
    base: str

    def __post_init__(self) -> None:
        action = self.action.strip().lower()
        url = self.url.strip()
        head = self.head.strip()
        base = self.base.strip()
        if action not in {"created", "updated", "existing"}:
            raise ValueError(f"unsupported Draft PR action: {action!r}")
        if self.number <= 0:
            raise ValueError("Draft PR number must be positive")
        if not url or not head or not base:
            raise ValueError("Draft PR result requires url, head, and base")
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "url", url)
        object.__setattr__(self, "head", head)
        object.__setattr__(self, "base", base)


def _publish_environment(environment: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    result = {
        str(key): str(value)
        for key, value in source.items()
        if not str(key).upper().startswith("HIKARI_")
    }
    result["GIT_TERMINAL_PROMPT"] = "0"
    result["GH_PROMPT_DISABLED"] = "1"
    return result


def _run(
    argv: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout_seconds: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        list(argv),
        cwd=cwd,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if len(detail) > 1800:
            detail = detail[-1800:]
        raise RuntimeError(detail or f"command failed with exit code {proc.returncode}")
    return proc


def _git(
    root: Path,
    *args: str,
    environment: Mapping[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return _run(
        ["git", "-C", str(root), *args],
        cwd=root,
        environment=environment,
        check=check,
    )


def _require_gh(environment: Mapping[str, str]) -> None:
    if shutil.which("gh", path=environment.get("PATH")) is None:
        raise RuntimeError(
            "GitHub CLI `gh` is not available; Draft PR publication cannot run non-interactively"
        )


def _source_base_branch(
    source_repo: Path,
    baseline_commit: str,
    *,
    environment: Mapping[str, str],
) -> str:
    baseline = baseline_commit.strip()
    if not baseline:
        raise RuntimeError("engineering session has no trusted baseline commit")
    branch = _git(
        source_repo,
        "branch",
        "--show-current",
        environment=environment,
    ).stdout.strip()
    if not branch:
        raise RuntimeError("source repository is detached; Draft PR base branch is ambiguous")
    if branch.startswith("hikari/engineering/"):
        raise RuntimeError("refusing to use an engineering branch as the Draft PR base")

    ancestor = _git(
        source_repo,
        "merge-base",
        "--is-ancestor",
        baseline,
        "HEAD",
        environment=environment,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError(
            "current source branch no longer descends from the engineering session baseline; refusing to guess a Draft PR base"
        )
    return branch


def _engineering_head(
    worktree: Path,
    branch: str,
    baseline_commit: str,
    *,
    environment: Mapping[str, str],
) -> str:
    normalized_branch = branch.strip()
    if not normalized_branch.startswith("hikari/engineering/"):
        raise RuntimeError("refusing to publish a Draft PR for a non-engineering branch")
    current_branch = _git(
        worktree,
        "branch",
        "--show-current",
        environment=environment,
    ).stdout.strip()
    if current_branch != normalized_branch:
        raise RuntimeError("engineering worktree branch does not match durable session state")
    dirty = _git(
        worktree,
        "status",
        "--porcelain",
        environment=environment,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("refusing to publish a Draft PR from a dirty engineering worktree")
    head = _git(
        worktree,
        "rev-parse",
        "HEAD",
        environment=environment,
    ).stdout.strip()
    if not baseline_commit.strip() or head == baseline_commit.strip():
        raise RuntimeError("engineering branch has no committed project change for a Draft PR")
    return head


def _require_remote_head(
    worktree: Path,
    branch: str,
    expected_head: str,
    *,
    environment: Mapping[str, str],
) -> None:
    proc = _git(
        worktree,
        "ls-remote",
        "--exit-code",
        "--heads",
        "origin",
        f"refs/heads/{branch}",
        environment=environment,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            "engineering branch is not available on origin; push the non-protected engineering branch before opening its Draft PR"
        )
    remote_head = (proc.stdout.strip().split() or [""])[0]
    if remote_head != expected_head.strip():
        raise RuntimeError(
            "origin engineering branch does not match the local committed head; push the latest non-protected engineering branch before opening its Draft PR"
        )


def _draft_metadata(
    worktree: Path,
    branch: str,
    base: str,
    baseline_commit: str,
    head_commit: str,
    *,
    environment: Mapping[str, str],
) -> tuple[str, str]:
    subject = _git(
        worktree,
        "log",
        "-1",
        "--format=%s",
        f"{baseline_commit}..HEAD",
        environment=environment,
    ).stdout.strip()
    if subject.casefold().startswith("hikari: "):
        subject = subject[8:].strip()
    title = f"Hikari: {subject or 'engineering maintenance'}"
    if len(title) > 120:
        title = title[:117].rstrip() + "..."

    changed = _git(
        worktree,
        "diff",
        "--name-only",
        baseline_commit,
        "HEAD",
        environment=environment,
    ).stdout.splitlines()
    files = [item.strip() for item in changed if item.strip()]
    visible_files = files[:20]
    file_lines = [f"- `{item}`" for item in visible_files]
    if len(files) > len(visible_files):
        file_lines.append(f"- 另有 {len(files) - len(visible_files)} 个文件")
    if not file_lines:
        file_lines.append("- 无可见文件变化")

    body = "\n".join(
        [
            _HIKARI_DRAFT_MARKER,
            "## Hikari Engineering Draft",
            "",
            f"- Base: `{base}`",
            f"- Head: `{branch}`",
            f"- Baseline: `{baseline_commit[:12]}`",
            f"- Current commit: `{head_commit[:12]}`",
            "",
            "### Changed files",
            *file_lines,
            "",
            "This Draft PR is maintained by Hikari inside the standing project maintainer mandate.",
        ]
    )
    return title, body


def _list_open_prs(
    worktree: Path,
    branch: str,
    *,
    environment: Mapping[str, str],
) -> list[dict[str, object]]:
    proc = _run(
        [
            "gh",
            "pr",
            "list",
            "--head",
            branch,
            "--state",
            "open",
            "--limit",
            "10",
            "--json",
            "number,url,isDraft,baseRefName,headRefName,body,title",
        ],
        cwd=worktree,
        environment=environment,
    )
    try:
        payload = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub CLI returned unreadable Draft PR metadata") from exc
    if not isinstance(payload, list):
        raise RuntimeError("GitHub CLI returned invalid Draft PR metadata")
    return [item for item in payload if isinstance(item, dict)]


def _one_matching_pr(
    prs: list[dict[str, object]],
    branch: str,
) -> dict[str, object] | None:
    matching = [item for item in prs if str(item.get("headRefName", "")).strip() == branch]
    if len(matching) > 1:
        raise RuntimeError("multiple open pull requests exist for the same engineering branch")
    return matching[0] if matching else None


def _result_from_pr(
    pr: Mapping[str, object],
    *,
    action: str,
    branch: str,
    base: str,
) -> DraftPullRequestResult:
    try:
        number = int(pr.get("number", 0))
    except (TypeError, ValueError):
        number = 0
    url = str(pr.get("url", "")).strip()
    if not bool(pr.get("isDraft", False)):
        raise RuntimeError("matching open pull request is not a Draft PR; refusing to change its review state")
    if not number or not url:
        raise RuntimeError("GitHub CLI did not return a trustworthy Draft PR identity")
    return DraftPullRequestResult(action, number, url, branch, base)


def open_or_update_draft_pr(
    *,
    source_repo: str | Path,
    worktree: str | Path,
    branch: str,
    baseline_commit: str,
    environment: Mapping[str, str] | None = None,
) -> DraftPullRequestResult:
    """Open or refresh Hikari's Draft PR for one already-pushed engineering branch.

    The head and base are derived from durable/local engineering state, never from model
    text. The current source branch may advance after the engineering session began, but
    it must still descend from the durable baseline. Existing human-authored Draft PR
    metadata is preserved unless the PR contains Hikari's ownership marker. A
    ready-for-review PR is never silently converted back to draft state.
    """

    source = Path(source_repo).expanduser().resolve()
    root = Path(worktree).expanduser().resolve()
    env = _publish_environment(environment)
    _require_gh(env)
    normalized_branch = branch.strip()
    base = _source_base_branch(source, baseline_commit, environment=env)
    head_commit = _engineering_head(
        root,
        normalized_branch,
        baseline_commit,
        environment=env,
    )
    _require_remote_head(
        root,
        normalized_branch,
        head_commit,
        environment=env,
    )
    title, body = _draft_metadata(
        root,
        normalized_branch,
        base,
        baseline_commit,
        head_commit,
        environment=env,
    )

    existing = _one_matching_pr(
        _list_open_prs(root, normalized_branch, environment=env),
        normalized_branch,
    )
    if existing is not None:
        current = _result_from_pr(
            existing,
            action="existing",
            branch=normalized_branch,
            base=str(existing.get("baseRefName", "")).strip() or base,
        )
        existing_body = str(existing.get("body", ""))
        if _HIKARI_DRAFT_MARKER not in existing_body:
            return current
        _run(
            [
                "gh",
                "pr",
                "edit",
                str(current.number),
                "--base",
                base,
                "--title",
                title,
                "--body",
                body,
            ],
            cwd=root,
            environment=env,
        )
        refreshed = _one_matching_pr(
            _list_open_prs(root, normalized_branch, environment=env),
            normalized_branch,
        )
        if refreshed is None:
            raise RuntimeError("Draft PR disappeared after GitHub metadata update")
        return _result_from_pr(
            refreshed,
            action="updated",
            branch=normalized_branch,
            base=base,
        )

    _run(
        [
            "gh",
            "pr",
            "create",
            "--draft",
            "--base",
            base,
            "--head",
            normalized_branch,
            "--title",
            title,
            "--body",
            body,
        ],
        cwd=root,
        environment=env,
    )
    created = _one_matching_pr(
        _list_open_prs(root, normalized_branch, environment=env),
        normalized_branch,
    )
    if created is None:
        raise RuntimeError("GitHub did not expose the newly created Draft PR")
    result = _result_from_pr(
        created,
        action="created",
        branch=normalized_branch,
        base=base,
    )
    if str(created.get("baseRefName", "")).strip() != base:
        raise RuntimeError("new Draft PR base does not match the trusted engineering baseline branch")
    return result
