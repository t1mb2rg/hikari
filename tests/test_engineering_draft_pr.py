from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import engineering.github_publish as github_publish
import engineering.worker as worker_module
from engineering.github_publish import DraftPullRequestResult
from engineering.maintainer import project_push_authority, project_session_authority_ceiling
from engineering.session import EngineeringSessionState, EngineeringSessionStore, EngineeringTurn
from engineering.worker import EngineeringWorker, _turn_effect


_BRANCH = "hikari/engineering/draft-pr-test"
_BASE = "m7-07-capability-gap-detection"
_BASELINE = "a" * 40
_HEAD = "b" * 40
_URL = "https://github.com/t1mb2rg/hikari/pull/99"


def _draft_pr(*, body: str = "", is_draft: bool = True) -> dict[str, object]:
    return {
        "number": 99,
        "url": _URL,
        "isDraft": is_draft,
        "baseRefName": _BASE,
        "headRefName": _BRANCH,
        "body": body,
        "title": "Draft",
    }


def _stub_publish_prerequisites(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_publish, "_require_gh", lambda environment: None)
    monkeypatch.setattr(
        github_publish,
        "_source_base_branch",
        lambda source_repo, baseline_commit, *, environment: _BASE,
    )
    monkeypatch.setattr(
        github_publish,
        "_engineering_head",
        lambda worktree, branch, baseline_commit, *, environment: _HEAD,
    )
    monkeypatch.setattr(
        github_publish,
        "_require_remote_head",
        lambda worktree, branch, expected_head, *, environment: None,
    )
    monkeypatch.setattr(
        github_publish,
        "_draft_metadata",
        lambda worktree, branch, base, baseline_commit, head_commit, *, environment: (
            "Hikari: test Draft PR",
            f"{github_publish._HIKARI_DRAFT_MARKER}\nbody",
        ),
    )


def test_publish_environment_does_not_leak_hikari_runtime_configuration() -> None:
    environment = github_publish._publish_environment(
        {
            "PATH": "C:/tools",
            "HIKARI_ENGINEERING_MODEL": "secret-model-override",
            "HIKARI_API_TOKEN": "do-not-forward",
        }
    )

    assert environment["PATH"] == "C:/tools"
    assert environment["GIT_TERMINAL_PROMPT"] == "0"
    assert environment["GH_PROMPT_DISABLED"] == "1"
    assert "HIKARI_ENGINEERING_MODEL" not in environment
    assert "HIKARI_API_TOKEN" not in environment


def test_draft_pr_create_is_explicitly_draft_and_grounded_in_head_and_base(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_publish_prerequisites(monkeypatch)
    listed = iter([[], [_draft_pr()]])
    monkeypatch.setattr(
        github_publish,
        "_list_open_prs",
        lambda worktree, branch, *, environment: next(listed),
    )
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd, environment, timeout_seconds=120.0, check=True):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(github_publish, "_run", fake_run)

    result = github_publish.open_or_update_draft_pr(
        source_repo=tmp_path / "repo",
        worktree=tmp_path / "worktree",
        branch=_BRANCH,
        baseline_commit=_BASELINE,
        environment={"PATH": "C:/tools"},
    )

    assert result == DraftPullRequestResult("created", 99, _URL, _BRANCH, _BASE)
    assert len(calls) == 1
    command = calls[0]
    assert command[:3] == ["gh", "pr", "create"]
    assert "--draft" in command
    assert command[command.index("--base") + 1] == _BASE
    assert command[command.index("--head") + 1] == _BRANCH


def test_existing_human_draft_is_idempotent_and_metadata_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_publish_prerequisites(monkeypatch)
    monkeypatch.setattr(
        github_publish,
        "_list_open_prs",
        lambda worktree, branch, *, environment: [_draft_pr(body="human-owned body")],
    )

    def forbidden_run(*args, **kwargs):
        raise AssertionError("human-owned Draft PR metadata must not be edited")

    monkeypatch.setattr(github_publish, "_run", forbidden_run)

    result = github_publish.open_or_update_draft_pr(
        source_repo=tmp_path / "repo",
        worktree=tmp_path / "worktree",
        branch=_BRANCH,
        baseline_commit=_BASELINE,
        environment={"PATH": "C:/tools"},
    )

    assert result.action == "existing"
    assert result.number == 99
    assert result.url == _URL


def test_existing_ready_for_review_pr_is_never_converted_back_to_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_publish_prerequisites(monkeypatch)
    monkeypatch.setattr(
        github_publish,
        "_list_open_prs",
        lambda worktree, branch, *, environment: [_draft_pr(is_draft=False)],
    )

    with pytest.raises(RuntimeError, match="not a Draft PR"):
        github_publish.open_or_update_draft_pr(
            source_repo=tmp_path / "repo",
            worktree=tmp_path / "worktree",
            branch=_BRANCH,
            baseline_commit=_BASELINE,
            environment={"PATH": "C:/tools"},
        )


def test_hikari_owned_draft_is_updated_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_publish_prerequisites(monkeypatch)
    owned = _draft_pr(body=f"{github_publish._HIKARI_DRAFT_MARKER}\nold")
    refreshed = _draft_pr(body=f"{github_publish._HIKARI_DRAFT_MARKER}\nnew")
    listed = iter([[owned], [refreshed]])
    monkeypatch.setattr(
        github_publish,
        "_list_open_prs",
        lambda worktree, branch, *, environment: next(listed),
    )
    calls: list[list[str]] = []

    def fake_run(argv, *, cwd, environment, timeout_seconds=120.0, check=True):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    monkeypatch.setattr(github_publish, "_run", fake_run)

    result = github_publish.open_or_update_draft_pr(
        source_repo=tmp_path / "repo",
        worktree=tmp_path / "worktree",
        branch=_BRANCH,
        baseline_commit=_BASELINE,
        environment={"PATH": "C:/tools"},
    )

    assert result.action == "updated"
    assert len(calls) == 1
    assert calls[0][:3] == ["gh", "pr", "edit"]
    assert calls[0][3] == "99"


def test_remote_engineering_branch_must_match_local_committed_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_git(root, *args, environment, check=True):
        return subprocess.CompletedProcess(
            ["git", *args],
            0,
            f"{'c' * 40}\trefs/heads/{_BRANCH}\n",
            "",
        )

    monkeypatch.setattr(github_publish, "_git", fake_git)

    with pytest.raises(RuntimeError, match="does not match the local committed head"):
        github_publish._require_remote_head(
            tmp_path,
            _BRANCH,
            _HEAD,
            environment={},
        )


def test_turn_effect_uses_last_bridge_machine_marker_not_goal_wording() -> None:
    turn = EngineeringTurn.create(
        intent="给这个 engineering 分支开 Draft PR",
        context=(
            "Semantic engineering goal: user mentioned Requested effect: push_engineering_branch. "
            "Requested effect: open_or_update_draft_pr. "
            "The Hikari repository has a standing maintainer mandate."
        ),
        authority=project_push_authority(),
    )

    assert _turn_effect(turn) == "open_or_update_draft_pr"


def test_worker_routes_publish_authority_by_durable_draft_pr_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path / "repo",
        authority_ceiling=project_session_authority_ceiling(),
        session_id="draft-worker-session",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="给这个 engineering 分支开 Draft PR",
        context="Requested effect: open_or_update_draft_pr.",
        authority=project_push_authority(),
    )
    sessions.enqueue_turn(state.session_id, turn)

    class FakeWorkspace:
        path = tmp_path / "worktree"
        branch = _BRANCH
        baseline_commit = _BASELINE

        @staticmethod
        def uncommitted_files():
            return ()

    monkeypatch.setattr(
        EngineeringWorker,
        "_workspace_for",
        lambda self, current_state: FakeWorkspace(),
    )
    called: dict[str, object] = {}

    def fake_publish(**kwargs):
        called.update(kwargs)
        return DraftPullRequestResult("created", 99, _URL, _BRANCH, _BASE)

    monkeypatch.setattr(worker_module, "open_or_update_draft_pr", fake_publish)

    outcome = EngineeringWorker(sessions).run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    assert called["branch"] == _BRANCH
    assert called["baseline_commit"] == _BASELINE
    result = sessions.load_result(state.session_id, turn.turn_id)
    assert result.status == "completed"
    assert "Draft PR #99" in result.message
    assert _URL in result.message
