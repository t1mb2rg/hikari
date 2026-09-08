from __future__ import annotations

import json
from pathlib import Path
import subprocess

from core.delivery import DeliveryOutbox
from engineering.bindings import EngineeringConversationBindingStore
from engineering.delivery import EngineeringCompletionDelivery
from engineering.maintainer import project_maintainer_authority
from engineering.session import (
    EngineeringAuthority,
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from engineering.workspace import EngineeringWorkspace


def _running_session(tmp_path: Path):
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path / "repo",
        authority_ceiling=EngineeringAuthority.read_only(),
        session_id="session-1",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="inspect README",
        authority=EngineeringAuthority.read_only(),
    )
    sessions.enqueue_turn(state.session_id, turn)
    sessions.update_runtime(
        state.session_id,
        status="running",
        latest_summary="worker was executing when Resident stopped",
    )
    return sessions, turn


def _worker_delivery(tmp_path: Path, sessions: EngineeringSessionStore):
    return EngineeringCompletionDelivery(
        sessions,
        EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json"),
        DeliveryOutbox(tmp_path / "proactive_delivery.db"),
    )


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()


def _source_repository(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "hikari@example.invalid")
    _git(path, "config", "user.name", "Hikari Tests")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "baseline")
    return path


def test_worker_owned_pump_requeues_orphaned_running_turn_with_same_id(tmp_path: Path) -> None:
    sessions, turn = _running_session(tmp_path)

    _worker_delivery(tmp_path, sessions).pump()

    recovered = sessions.load("session-1")
    assert recovered.status == "pending"
    assert recovered.current_turn_id == turn.turn_id
    assert recovered.latest_summary == "Engineering Worker restart recovered the same durable turn"
    assert sessions.load_turn(recovered.session_id, turn.turn_id).turn_id == turn.turn_id
    events = sessions.events(recovered.session_id)
    assert events[-1].kind == "accepted"
    assert events[-1].turn_id == turn.turn_id
    assert "restart recovered" in events[-1].summary


def test_resident_owned_pump_does_not_requeue_running_turn(tmp_path: Path) -> None:
    sessions, turn = _running_session(tmp_path)
    delivery = EngineeringCompletionDelivery(
        sessions,
        EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json"),
        DeliveryOutbox(tmp_path / "proactive_delivery.db"),
        renderer=lambda facts, channel, conversation_id: "unused",
    )

    delivery.pump()

    current = sessions.load("session-1")
    assert current.status == "running"
    assert current.current_turn_id == turn.turn_id


def test_worker_owned_pump_finalizes_result_written_before_state_crash(tmp_path: Path) -> None:
    sessions, turn = _running_session(tmp_path)
    result = EngineeringResult(
        turn_id=turn.turn_id,
        status="completed",
        message="durable result reached disk before state update",
        completed_at=1234.0,
    )
    result_path = sessions.root / "session-1" / "results" / f"{turn.turn_id}.json"
    result_path.write_text(
        json.dumps(result.to_mapping(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    _worker_delivery(tmp_path, sessions).pump()

    finalized = sessions.load("session-1")
    assert finalized.status == "completed"
    assert finalized.current_turn_id == turn.turn_id
    assert finalized.latest_summary == result.message
    assert sessions.load_result(finalized.session_id, turn.turn_id).message == result.message


def test_worker_owned_pump_blocks_unreadable_orphaned_turn_instead_of_guessing(tmp_path: Path) -> None:
    sessions, turn = _running_session(tmp_path)
    turn_path = sessions.root / "session-1" / "turns" / f"{turn.turn_id}.json"
    turn_path.unlink()

    _worker_delivery(tmp_path, sessions).pump()

    blocked = sessions.load("session-1")
    assert blocked.status == "blocked"
    assert blocked.current_turn_id == turn.turn_id
    assert "不可读取" in blocked.latest_summary
    result = sessions.load_result(blocked.session_id, turn.turn_id)
    assert result.status == "blocked"
    assert "不会猜测或重放" in result.message


def test_interrupted_explicit_command_is_blocked_not_replayed(tmp_path: Path) -> None:
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    authority = EngineeringAuthority(repository_read=True, run_commands=True)
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path / "repo",
        authority_ceiling=authority,
        session_id="command-session",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="run a project-local command",
        context="Requested effect: run_project_command.",
        authority=authority,
    )
    sessions.enqueue_turn(state.session_id, turn)
    sessions.update_runtime(state.session_id, status="running")

    _worker_delivery(tmp_path, sessions).pump()

    recovered = sessions.load(state.session_id)
    assert recovered.status == "blocked"
    assert recovered.current_turn_id == turn.turn_id
    result = sessions.load_result(state.session_id, turn.turn_id)
    assert result.status == "blocked"
    assert "不允许自动重放" in result.message
    assert "重复副作用" in result.message
    assert not any(event.kind == "accepted" for event in sessions.events(state.session_id)[1:])


def test_interrupted_maintainer_replay_discards_partial_uncommitted_edits(tmp_path: Path) -> None:
    repository = _source_repository(tmp_path / "repo")
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    authority = project_maintainer_authority()
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=authority,
        session_id="maintain-session",
    )
    sessions.create(state)
    turn = EngineeringTurn.create(
        intent="update README safely",
        context="Requested effect: maintain_project.",
        authority=authority,
    )
    sessions.enqueue_turn(state.session_id, turn)
    workspace = EngineeringWorkspace.create(repository, state.session_id)
    sessions.update_runtime(
        state.session_id,
        status="running",
        workspace_path=str(workspace.path),
        workspace_branch=workspace.branch,
        baseline_commit=workspace.baseline_commit,
    )
    (workspace.path / "README.md").write_text("partial crash edit\n", encoding="utf-8")
    (workspace.path / "partial.tmp").write_text("unfinished\n", encoding="utf-8")
    assert set(workspace.uncommitted_files()) == {"README.md", "partial.tmp"}

    _worker_delivery(tmp_path, sessions).pump()

    recovered = sessions.load(state.session_id)
    assert recovered.status == "pending"
    assert recovered.current_turn_id == turn.turn_id
    resumed = EngineeringWorkspace.resume(
        repository=repository,
        workspace_path=recovered.workspace_path or "",
        branch=recovered.workspace_branch or "",
        baseline_commit=recovered.baseline_commit or "",
    )
    assert resumed.uncommitted_files() == ()
    assert (resumed.path / "README.md").read_text(encoding="utf-8") == "baseline\n"
    assert not (resumed.path / "partial.tmp").exists()
