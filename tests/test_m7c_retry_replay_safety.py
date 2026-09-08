from __future__ import annotations

from pathlib import Path
import subprocess

from core.delivery import DeliveryOutbox
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.delivery import EngineeringCompletionDelivery, EngineeringCompletionFacts
from engineering.effects import (
    RESTART_REPLAY_SAFE_EFFECTS,
    SUPPORTED_ENGINEERING_EFFECTS,
    authority_for_effect,
    turn_effect,
)
from engineering.goal import EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore
from engineering.maintainer import project_maintainer_authority, project_session_authority_ceiling
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import (
    EngineeringAuthority,
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from engineering.worker import _turn_effect as worker_turn_effect
from engineering.workspace import EngineeringWorkspace


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "hikari@example.invalid")
    _git(path, "config", "user.name", "Hikari Tests")
    (path / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "baseline")
    return path


def test_recovery_and_worker_effect_parsers_agree_for_all_supported_machine_markers() -> None:
    for effect in SUPPORTED_ENGINEERING_EFFECTS:
        turn = EngineeringTurn.create(
            intent=f"execute {effect}",
            context=(
                "Semantic goal may mention Requested effect: inspect_project. earlier. "
                f"Requested effect: {effect}. Machine field wins."
            ),
            authority=authority_for_effect(effect),
        )
        assert turn_effect(turn) == effect
        assert worker_turn_effect(turn) == effect


def test_legacy_command_authority_stays_non_replayable_without_effect_marker() -> None:
    turn = EngineeringTurn.create(
        intent="legacy explicit command",
        authority=EngineeringAuthority(repository_read=True, run_commands=True),
    )

    effect = turn_effect(turn)

    assert effect == "run_project_command"
    assert effect not in RESTART_REPLAY_SAFE_EFFECTS


def test_failed_maintainer_retry_discards_partial_attempt_before_attempt_two(tmp_path: Path) -> None:
    repository = _repo(tmp_path / "repo")
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id="retry-session",
    )
    sessions.create(session)
    workspace = EngineeringWorkspace.create(repository, session.session_id)
    sessions.update_runtime(
        session.session_id,
        workspace_path=str(workspace.path),
        workspace_branch=workspace.branch,
        baseline_commit=workspace.baseline_commit,
    )

    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="repair README",
        steps=(
            EngineeringGoalStep.create(
                effect="maintain_project",
                instruction="repair README\n原始请求：只修改 README",
                step_id="step-1",
            ),
        ),
        goal_id="retry-goal",
    )
    goals.create(goal)
    loop = PersistentMaintainerLoop(goals, sessions)

    first = loop.advance_once(goal.goal_id)
    assert first.turn_id is not None
    sessions.update_runtime(session.session_id, status="running")
    (workspace.path / "README.md").write_text("partial failed edit\n", encoding="utf-8")
    (workspace.path / "leftover.txt").write_text("partial\n", encoding="utf-8")
    sessions.save_result(
        session.session_id,
        EngineeringResult(
            turn_id=first.turn_id,
            status="failed",
            message="backend failed after partial edits",
            changed_files=("README.md", "leftover.txt"),
        ),
    )

    retry = loop.advance_once(goal.goal_id)

    current = goals.load(goal.goal_id)
    assert current.status == "active"
    assert current.current_step.attempts == 2
    assert current.current_step.turn_id is not None
    assert current.current_step.turn_id != first.turn_id
    assert retry.action in {"recovered_enqueue", "waiting"}
    resumed = EngineeringWorkspace.resume(
        repository=repository,
        workspace_path=workspace.path,
        branch=workspace.branch,
        baseline_commit=workspace.baseline_commit,
    )
    assert resumed.uncommitted_files() == ()
    assert (resumed.path / "README.md").read_text(encoding="utf-8") == "baseline\n"
    assert not (resumed.path / "leftover.txt").exists()


def test_interrupted_command_becomes_grounded_blocked_delivery_not_a_replay(tmp_path: Path) -> None:
    root = tmp_path / "resident"
    sessions = EngineeringSessionStore(root / "engineering")
    bindings = EngineeringConversationBindingStore(root / "engineering_bindings.json")
    outbox = DeliveryOutbox(root / "proactive_delivery.db")
    authority = EngineeringAuthority(repository_read=True, run_commands=True)
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path / "repo",
        authority_ceiling=authority,
        session_id="command-session",
    )
    sessions.create(session)
    turn = EngineeringTurn.create(
        intent="run explicit project command",
        context="Requested effect: run_project_command.",
        authority=authority,
    )
    sessions.enqueue_turn(session.session_id, turn)
    sessions.update_runtime(session.session_id, status="running")
    bindings.bind(
        EngineeringConversationBinding(
            session_id=session.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    # Worker ownership changes. The command must not be replayed.
    EngineeringCompletionDelivery(sessions, bindings, outbox).pump()
    blocked = sessions.load(session.session_id)
    assert blocked.status == "blocked"
    result = sessions.load_result(session.session_id, turn.turn_id)
    assert result.status == "blocked"
    assert "不允许自动重放" in result.message

    rendered: list[EngineeringCompletionFacts] = []

    def renderer(facts: EngineeringCompletionFacts, channel: str, conversation_id: str) -> str:
        rendered.append(facts)
        return "命令执行结果在重启时无法确认，我没有自动再执行一次。"

    delivery = EngineeringCompletionDelivery(
        sessions,
        bindings,
        outbox,
        renderer=renderer,
    )
    assert delivery.pump() == 1
    assert len(rendered) == 1
    assert rendered[0].status == "blocked"
    assert "结果不确定" in rendered[0].summary
    record = outbox.get(f"engineering:{session.session_id}:{turn.turn_id}")
    assert record is not None
    assert record.state == "pending"
    assert record.request.text == "命令执行结果在重启时无法确认，我没有自动再执行一次。"
