from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from engineering.backend import EngineeringAgentResult
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


def test_source_head_requires_clean_committed_repository(tmp_path: Path):
    repo = _repo(tmp_path)
    expected = _git(repo, "rev-parse", "HEAD")

    assert EngineeringWorkspace.source_head(repo) == expected

    (repo / "README.md").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(EngineeringWorkspaceError, match="uncommitted changes"):
        EngineeringWorkspace.source_head(repo)
