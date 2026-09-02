from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from engineering.backend import EngineeringAgentResult
from engineering.maintainer import (
    ProjectTestResult,
    project_maintainer_authority,
    project_test_environment,
    run_project_tests,
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
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    return repo


def _repo_with_validation(tmp_path: Path) -> Path:
    repo = _repo(tmp_path)
    (repo / "test_project.py").write_text(
        "from pathlib import Path\n\n"
        "def test_readme_is_maintained():\n"
        "    assert 'Maintained by Hikari' in Path('README.md').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    _git(repo, "add", "test_project.py")
    _git(repo, "commit", "-m", "add validation")
    return repo


def _store(tmp_path: Path) -> EngineeringSessionStore:
    return EngineeringSessionStore(tmp_path / "resident" / "engineering")


def _pending_session(tmp_path: Path) -> tuple[EngineeringSessionStore, EngineeringSessionState, EngineeringTurn]:
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
        repository=_repo_with_validation(tmp_path),
        authority_ceiling=project_maintainer_authority(),
        session_id="maintainer-session",
    )
    store.create(state)
    turn = EngineeringTurn.create(
        intent="Update README so the project declares it is Maintained by Hikari.",
        authority=project_maintainer_authority(),
    )
    store.enqueue_turn(state.session_id, turn)
    return store, state, turn


def test_authority_ceiling_rejects_write_turn(tmp_path: Path):
    store, state, _ = _pending_session(tmp_path)

    write = EngineeringTurn.create(
        intent="Change README",
        authority=EngineeringAuthority(repository_read=True, repository_write=True),
    )

    with pytest.raises(EngineeringProtocolError, match="authority ceiling"):
        store.enqueue_turn(state.session_id, write)


def test_read_only_worker_completes_and_persists_real_result(tmp_path: Path):
    store, state, turn = _pending_session(tmp_path)

    class FakeBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            assert "READ-ONLY" in prompt
            assert (Path(worktree) / "README.md").is_file()
            return EngineeringAgentResult(
                returncode=0,
                stdout="{}",
                stderr="",
                final_message="README describes Hikari as a resident intelligence.",
                session_id="claude-session-1",
            )

    worker = EngineeringWorker(store, backend_factory=lambda _state, _turn: FakeBackend())
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    saved = store.load(state.session_id)
    assert saved.status == "completed"
    assert saved.backend_session_id == "claude-session-1"
    assert saved.workspace_path
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.status == "completed"
    assert "resident intelligence" in result.message
    assert result.changed_files == ()
    kinds = [event.kind for event in store.events(state.session_id)]
    assert kinds == ["accepted", "started", "progress", "completed"]


def test_read_only_worker_blocks_backend_mutation(tmp_path: Path):
    store, state, turn = _pending_session(tmp_path)

    class MutatingBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text("mutated\n", encoding="utf-8")
            return EngineeringAgentResult(0, "{}", "", "I changed it", "claude-session-2")

    worker = EngineeringWorker(store, backend_factory=lambda _state, _turn: MutatingBackend())
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "blocked"
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.status == "blocked"
    assert result.changed_files == ("README.md",)


def test_follow_up_turn_preserves_backend_session_context(tmp_path: Path):
    store, state, _ = _pending_session(tmp_path)
    seen_backend_sessions: list[str | None] = []

    class FakeBackend:
        def __init__(self, next_id: str):
            self.next_id = next_id

        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            return EngineeringAgentResult(0, "{}", "", "done", self.next_id)

    def first_factory(current: EngineeringSessionState, turn: EngineeringTurn):
        seen_backend_sessions.append(current.backend_session_id)
        return FakeBackend("claude-session-shared")

    worker = EngineeringWorker(store, backend_factory=first_factory)
    assert worker.run_once().status == "completed"

    follow = EngineeringTurn.create(
        intent="Now inspect the architecture document too.",
        authority=EngineeringAuthority.read_only(),
    )
    store.enqueue_turn(state.session_id, follow)

    def second_factory(current: EngineeringSessionState, turn: EngineeringTurn):
        seen_backend_sessions.append(current.backend_session_id)
        return FakeBackend("claude-session-shared")

    worker = EngineeringWorker(store, backend_factory=second_factory)
    assert worker.run_once().status == "completed"
    assert seen_backend_sessions == [None, "claude-session-shared"]


def test_maintainer_worker_edits_tests_and_commits_without_human_step(tmp_path: Path):
    store, state, turn = _pending_maintainer_session(tmp_path)

    class FakeMaintainerBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            assert "Maintainer Session" in prompt
            path = Path(worktree) / "README.md"
            path.write_text("# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8")
            return EngineeringAgentResult(
                0,
                "{}",
                "",
                "Updated the README maintenance statement.",
                "claude-maintainer-1",
            )

    worker = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: FakeMaintainerBackend(),
    )
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    saved = store.load(state.session_id)
    workspace = Path(saved.workspace_path or "")
    assert workspace.is_dir()
    assert _git(workspace, "status", "--porcelain") == ""
    assert _git(workspace, "log", "-1", "--pretty=%s").startswith("hikari:")
    assert "验证：项目测试通过" in outcome.message
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.changed_files == ("README.md",)


def test_maintainer_worker_repairs_failed_tests_before_commit(tmp_path: Path):
    store, state, _ = _pending_maintainer_session(tmp_path)

    class RepairingBackend:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            self.calls += 1
            path = Path(worktree) / "README.md"
            if self.calls == 1:
                path.write_text("# Hikari\n\nNot fixed yet.\n", encoding="utf-8")
            else:
                assert "Test failure" in prompt
                path.write_text("# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8")
            return EngineeringAgentResult(
                0,
                "{}",
                "",
                "Repaired README after validation feedback.",
                "claude-maintainer-repair",
            )

    backend = RepairingBackend()
    worker = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: backend,
        max_repair_attempts=2,
    )
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "completed"
    assert backend.calls == 2
    workspace = Path(store.load(state.session_id).workspace_path or "")
    assert _git(workspace, "status", "--porcelain") == ""
    assert "Maintained by Hikari" in (workspace / "README.md").read_text(encoding="utf-8")


def test_maintainer_worker_does_not_repair_missing_validation_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store, state, turn = _pending_maintainer_session(tmp_path)

    class Backend:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            self.calls += 1
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            return EngineeringAgentResult(0, "{}", "", "done", "backend-session")

    backend = Backend()
    monkeypatch.setattr(
        "engineering.worker.run_project_tests",
        lambda _path: ProjectTestResult(
            1,
            "ModuleNotFoundError: No module named 'httpx2'",
            "dependency_environment",
        ),
    )
    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: backend,
    ).run_once()

    assert outcome is not None
    assert outcome.status == "blocked"
    assert "验证环境不可用" in outcome.message
    assert backend.calls == 1
    result = store.load_result(state.session_id, turn.turn_id)
    assert result.changed_files == ("README.md",)


def test_project_test_failure_classification_uses_output_before_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        "engineering.maintainer.assert_nested_process_capability",
        lambda _path, **_kwargs: None,
    )
    full_output = (
        "ModuleNotFoundError: No module named 'httpx2'\n" + "x" * 7000
    )
    monkeypatch.setattr(
        "engineering.maintainer.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, full_output, ""
        ),
    )

    result = run_project_tests(tmp_path)

    assert result.failure_kind == "dependency_environment"
    assert len(result.output) == 5000


def test_project_test_environment_removes_live_hikari_configuration() -> None:
    cleaned = project_test_environment(
        {
            "PATH": "test-path",
            "HIKARI_MODEL_API_KEY": "secret",
            "hikari_qq_proactive_user_id": "real-user",
        }
    )

    assert cleaned == {"PATH": "test-path"}


def test_readme_only_task_blocks_scope_drift_before_tests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    store, _state, _turn = _pending_maintainer_session(tmp_path)
    tests_called = False

    class Backend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n", encoding="utf-8"
            )
            (Path(worktree) / "test_project.py").write_text(
                "def test_nothing():\n    assert True\n", encoding="utf-8"
            )
            return EngineeringAgentResult(0, "{}", "", "done", "backend-session")

    def fake_tests(_path):
        nonlocal tests_called
        tests_called = True
        return ProjectTestResult(0, "passed")

    monkeypatch.setattr("engineering.worker.run_project_tests", fake_tests)
    outcome = EngineeringWorker(
        store,
        backend_factory=lambda _state, _turn: Backend(),
    ).run_once()

    assert outcome is not None
    assert outcome.status == "blocked"
    assert "README-only" in outcome.message
    assert tests_called is False


def test_read_only_follow_up_allows_prior_authorized_commit_in_same_session(tmp_path: Path):
    store, state, _ = _pending_maintainer_session(tmp_path)

    class FirstBackend:
        def run(self, worktree: Path, prompt: str) -> EngineeringAgentResult:
            (Path(worktree) / "README.md").write_text(
                "# Hikari\n\nMaintained by Hikari.\n",
                encoding="utf-8",
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

    worker = EngineeringWorker(store, backend_factory=lambda _state, _turn: ReadBackend())
    outcome = worker.run_once()

    assert outcome is not None
    assert outcome.status == "completed"


def test_source_head_requires_clean_committed_repository(tmp_path: Path):
    repo = _repo(tmp_path)
    expected = _git(repo, "rev-parse", "HEAD")

    assert EngineeringWorkspace.source_head(repo) == expected

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(EngineeringWorkspaceError, match="uncommitted changes"):
        EngineeringWorkspace.source_head(repo)
