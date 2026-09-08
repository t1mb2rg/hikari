from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from engineering.backend import EngineeringAgentEvent, EngineeringAgentResult
from engineering.maintainer import (
    project_maintainer_authority,
    project_push_authority,
    project_session_authority_ceiling,
    project_test_environment,
)
from engineering.session import (
    EngineeringAuthority,
    EngineeringProtocolError,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from engineering.worker import EngineeringWorker
from engineering.workspace import EngineeringWorkspace, EngineeringWorkspaceError


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    return proc.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Hikari Test")
    _git(repo, "config", "user.email", "hikari@example.invalid")
    (repo / "README.md").write_text("# Hikari\n\nResident intelligence.\n", encoding="utf-8")
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(repo, "add", "README.md", "module.py")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _store(tmp_path: Path) -> EngineeringSessionStore:
    return EngineeringSessionStore(tmp_path / "resident" / "engineering")


def _pending_session(
    tmp_path: Path,
) -> tuple[EngineeringSessionStore, EngineeringSessionState, EngineeringTurn]:
    store = _store(tmp_path)
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=_repo(tmp_path),
        authority_ceiling=EngineeringAuthority.read_only(),
        session_id="session-one",
    )
    store.create(state)
    turn = EngineeringTurn.create(
        intent="Read README and tell me what this project is.",
        authority=EngineeringAuthority.read_only(),
    )
    store.enqueue_turn(state.session_id, turn)
    return store, state, turn


def _pending_maintainer_session(
    tmp_path: Path,
) -> tuple[EngineeringSessionStore, EngineeringSessionState, EngineeringTurn]:
    store = _store(tmp_path)
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=_repo(tmp_path),
        authority_ceiling=project_session_authority_ceiling(),
        session_id="maintainer-session",
    )
    store.create(state)
    turn = EngineeringTurn.create(
        intent="Update README so the project declares it is Maintained by Hikari.",
        authority=project_maintainer_authority(),
    )
    store.enqueue_turn(state.session_id, turn)
    return store, state, turn


def test_authority_ceiling_rejects_write_turn(tmp_path: Path) -> None:
    store, state, _ = _pending_session(tmp_path)
    write = EngineeringTurn.create(
        intent="Change README",
        authority=EngineeringAuthority(repository_read=True, repository_write=True),
    )

    with pytest.raises(EngineeringProtocolError, match="authority ceiling"):
        store.enqueue_turn(state.session_id, write)


def test_read_only_worker_completes_and_persists_real_result(tmp_path: Path) -> None:
    store, state, turn = _pending_session(tmp_path)

    class FakeBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            assert "READ-ONLY" in prompt
            assert (Path(worktree) / "README.md").is_file()
            return EngineeringAgentResult(
                0,
                "{}",
                "",
                "README describes Hikari as a resident intelligence.",
                "claude-session-1",
            )

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: FakeBackend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    saved = store.load(state.session_id)
    assert saved.backend_session_id == "claude-session-1"
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.status == "completed"
    assert "resident intelligence" in result.message
    assert result.changed_files == ()


def test_read_only_worker_blocks_backend_mutation(tmp_path: Path) -> None:
    store, state, turn = _pending_session(tmp_path)

    class MutatingBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text("mutated\n", encoding="utf-8")
            return EngineeringAgentResult(0, "{}", "", "I changed it", "claude-session-2")

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: MutatingBackend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "blocked"
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.changed_files == ("README.md",)


def test_follow_up_turn_preserves_backend_session_context(tmp_path: Path) -> None:
    store, state, _ = _pending_session(tmp_path)
    seen_backend_sessions: list[str | None] = []

    class FakeBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            return EngineeringAgentResult(0, "{}", "", "done", "claude-session-shared")

    def factory(current: EngineeringSessionState, _turn: EngineeringTurn):
        seen_backend_sessions.append(current.backend_session_id)
        return FakeBackend()

    worker = EngineeringWorker(store, backend_factory=factory)
    assert worker.run_once().status == "completed"

    follow = EngineeringTurn.create(
        intent="Now inspect the architecture document too.",
        authority=EngineeringAuthority.read_only(),
    )
    store.enqueue_turn(state.session_id, follow)
    assert worker.run_once().status == "completed"
    assert seen_backend_sessions == [None, "claude-session-shared"]


def test_maintainer_worker_delegates_validation_to_backend_and_commits(tmp_path: Path) -> None:
    store, state, turn = _pending_maintainer_session(tmp_path)

    class FakeMaintainerBackend:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            self.calls += 1
            assert "task-appropriate validation" in prompt
            assert "Documentation-only changes do not need" in prompt
            path = Path(worktree) / "README.md"
            path.write_text("# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8")
            return EngineeringAgentResult(
                0,
                "{}",
                "",
                "Updated the README. Documentation-only change; no project tests were needed.",
                "claude-maintainer-1",
                events=(
                    EngineeringAgentEvent("tool", "Read: README.md"),
                    EngineeringAgentEvent("tool", "Edit: README.md"),
                ),
            )

    backend = FakeMaintainerBackend()
    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: backend,
    ).run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    assert backend.calls == 1
    saved = store.load(state.session_id)
    workspace = Path(saved.workspace_path or "")
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(workspace, "log", "-1", "--pretty=%s").startswith("hikari:")
    assert "未记录到项目测试命令" in outcome.message
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.changed_files == ("README.md",)


def test_maintainer_completion_reports_observed_validation_without_rerunning_it(tmp_path: Path) -> None:
    store, _state, _turn = _pending_maintainer_session(tmp_path)

    class Backend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            return EngineeringAgentResult(
                0,
                "{}",
                "",
                "Updated and checked the relevant path.",
                "validated-session",
                events=(
                    EngineeringAgentEvent(
                        "tool",
                        "Bash: python -m pytest tests/test_engineering_runtime.py -q",
                    ),
                    EngineeringAgentEvent("tool_result", "Bash completed"),
                ),
            )

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: Backend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    assert "python -m pytest tests/test_engineering_runtime.py -q" in outcome.message
    kinds = [event.kind for event in store.events("maintainer-session")]
    assert kinds == ["accepted", "started", "progress", "progress", "completed"]


def test_backend_failure_is_not_committed_and_keeps_activity_evidence(tmp_path: Path) -> None:
    store, state, turn = _pending_maintainer_session(tmp_path)
    baseline = _git(Path(state.repository), "rev-parse", "HEAD")

    class FailingBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text("unfinished\n", encoding="utf-8")
            return EngineeringAgentResult(
                1,
                "",
                "validation failed inside Claude Code",
                "",
                "failed-session",
                events=(
                    EngineeringAgentEvent("tool", "Bash: python -m pytest tests/test_x.py -q"),
                    EngineeringAgentEvent("tool_result", "Bash failed: 1 failed"),
                ),
            )

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: FailingBackend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "failed"
    assert "最后活动" in outcome.message
    assert "Bash failed" in outcome.message
    saved = store.load(state.session_id)
    workspace = Path(saved.workspace_path or "")
    assert _git(workspace, "rev-parse", "HEAD") == baseline
    assert _git(workspace, "status", "--porcelain") != ""
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.changed_files == ("README.md",)


def test_readme_only_scope_drift_blocks_before_commit(tmp_path: Path) -> None:
    store, state, _turn = _pending_maintainer_session(tmp_path)
    baseline = _git(Path(state.repository), "rev-parse", "HEAD")

    class Backend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            (Path(worktree) / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
            return EngineeringAgentResult(0, "{}", "", "done", "backend-session")

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: Backend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "blocked"
    assert "README-only" in outcome.message
    workspace = Path(store.load(state.session_id).workspace_path or "")
    assert _git(workspace, "rev-parse", "HEAD") == baseline


def test_worker_pushes_committed_engineering_branch_to_origin(tmp_path: Path) -> None:
    store, state, _ = _pending_maintainer_session(tmp_path)
    source = Path(state.repository)
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    _git(source, "remote", "add", "origin", str(remote))

    class Backend:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            self.calls += 1
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            return EngineeringAgentResult(0, "{}", "", "maintained", "push-session")

    backend = Backend()
    worker = EngineeringWorker(store, backend_factory=lambda _state, _turn: backend)
    assert worker.run_once().status == "completed"
    saved = store.load(state.session_id)
    workspace = Path(saved.workspace_path or "")
    branch = saved.workspace_branch or ""
    local_head = _git(workspace, "rev-parse", "HEAD")

    push = EngineeringTurn.create(
        intent="Push this engineering branch to origin.",
        authority=project_push_authority(),
    )
    store.enqueue_turn(state.session_id, push)
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    assert backend.calls == 1
    assert "推送到 `origin`" in outcome.message
    assert branch.startswith("hikari/engineering/")
    assert _git(remote, "rev-parse", f"refs/heads/{branch}") == local_head


def test_project_test_environment_removes_live_hikari_configuration() -> None:
    cleaned = project_test_environment(
        {
            "PATH": "test-path",
            "HIKARI_MODEL_API_KEY": "secret",
            "hikari_qq_proactive_user_id": "real-user",
        }
    )
    assert cleaned == {"PATH": "test-path"}


def test_read_only_follow_up_allows_prior_authorized_commit_in_same_session(tmp_path: Path) -> None:
    store, state, _ = _pending_maintainer_session(tmp_path)

    class FirstBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            return EngineeringAgentResult(0, "{}", "", "maintained", "shared-session")

    worker = EngineeringWorker(store, backend_factory=lambda _state, _turn: FirstBackend())
    assert worker.run_once().status == "completed"

    follow = EngineeringTurn.create(
        intent="Read README again.",
        authority=EngineeringAuthority.read_only(),
    )
    store.enqueue_turn(state.session_id, follow)

    class ReadBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            assert "Maintained by Hikari" in (Path(worktree) / "README.md").read_text(encoding="utf-8")
            return EngineeringAgentResult(0, "{}", "", "still maintained", "shared-session")

    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: ReadBackend(),
    ).run_once()
    assert outcome is not None
    assert outcome.status == "completed"


def test_source_head_requires_clean_committed_repository(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    expected = _git(repo, "rev-parse", "HEAD")
    assert EngineeringWorkspace.source_head(repo) == expected

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(EngineeringWorkspaceError, match="uncommitted changes"):
        EngineeringWorkspace.source_head(repo)
