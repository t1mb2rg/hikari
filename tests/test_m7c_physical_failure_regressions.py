from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

import engineering.github_publish as github_publish
from core.delivery import DeliveryOutbox
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.delivery import EngineeringCompletionDelivery, EngineeringCompletionFacts
from engineering.goal import (
    EngineeringGoalState,
    EngineeringGoalStep,
    EngineeringGoalStore,
)
from engineering.maintainer import _commit_subject, project_session_authority_ceiling
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import (
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
)


_BRANCH = "hikari/engineering/m7c-remote-visibility"
_HEAD = "b" * 40


def test_remote_head_verification_retries_transient_unavailable_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_git(root, *args, environment, check=True):
        nonlocal calls
        calls += 1
        if calls < 3:
            return subprocess.CompletedProcess(["git", *args], 2, "", "temporary unavailable")
        return subprocess.CompletedProcess(
            ["git", *args],
            0,
            f"{_HEAD}\trefs/heads/{_BRANCH}\n",
            "",
        )

    monkeypatch.setattr(github_publish, "_git", fake_git)
    monkeypatch.setattr(github_publish.time, "sleep", lambda delay: sleeps.append(delay))

    github_publish._require_remote_head(
        tmp_path,
        _BRANCH,
        _HEAD,
        environment={},
    )

    assert calls == 3
    assert sleeps == [0.5, 1.0]


def _publish_retry_runtime(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id="publish-session",
    )
    sessions.create(session)
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="open the graduation Draft PR",
        steps=(
            EngineeringGoalStep.create(
                effect="open_or_update_draft_pr",
                instruction="open the graduation Draft PR",
                step_id="step-1",
            ),
        ),
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="publish-goal",
    )
    goals.create(goal)
    return sessions, goals, PersistentMaintainerLoop(goals, sessions)


def _fail_current_publish_turn(sessions: EngineeringSessionStore, message: str) -> None:
    state = sessions.load("publish-session")
    assert state.current_turn_id is not None
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=state.current_turn_id,
            status="failed",
            message=message,
        ),
    )


def test_idempotent_publish_effect_gets_third_bounded_attempt(tmp_path: Path) -> None:
    sessions, goals, loop = _publish_retry_runtime(tmp_path)

    first = loop.advance_once("publish-goal")
    assert first.turn_id is not None
    _fail_current_publish_turn(sessions, "remote head temporarily unavailable")

    loop.advance_once("publish-goal")
    goal = goals.load("publish-goal")
    assert goal.current_step.attempts == 2
    _fail_current_publish_turn(sessions, "remote head temporarily unavailable again")

    third = loop.advance_once("publish-goal")
    goal = goals.load("publish-goal")
    assert goal.status == "active"
    assert goal.current_step.attempts == 3
    assert third.action in {"recovered_enqueue", "waiting"}

    _fail_current_publish_turn(sessions, "persistent remote failure")
    terminal = loop.advance_once("publish-goal")
    goal = goals.load("publish-goal")
    assert terminal.status == "failed"
    assert goal.status == "failed"
    assert goal.current_step.attempts == 3


def test_failed_whole_goal_delivery_summary_uses_terminal_failure_only(tmp_path: Path) -> None:
    root = tmp_path / "resident"
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(root / "engineering")
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id="goal-session",
    )
    sessions.create(session)
    bindings = EngineeringConversationBindingStore(root / "engineering_bindings.json")
    bindings.bind(
        EngineeringConversationBinding(
            session_id=session.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )
    goals = EngineeringGoalStore(root / "engineering_goals")
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="edit then publish",
        steps=(
            EngineeringGoalStep(
                step_id="step-1",
                effect="maintain_project",
                instruction="edit",
                status="completed",
                turn_id="turn-1",
                attempts=1,
                result_status="completed",
                result_message="任务已完成。修改：docs/file.md",
            ),
            EngineeringGoalStep(
                step_id="step-2",
                effect="open_or_update_draft_pr",
                instruction="publish",
                status="failed",
                turn_id="turn-2",
                attempts=2,
                result_status="failed",
                result_message="Draft PR 发布失败：remote branch unavailable",
            ),
        ),
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="failed-goal",
    )
    goals.create(
        EngineeringGoalState(
            **{
                **goal.__dict__,
                "status": "failed",
                "current_step_index": 1,
                "final_summary": "Draft PR 发布失败：remote branch unavailable",
            }
        )
    )

    rendered: list[EngineeringCompletionFacts] = []

    def renderer(facts: EngineeringCompletionFacts, channel: str, conversation_id: str) -> str:
        rendered.append(facts)
        return "failed"

    delivery = EngineeringCompletionDelivery(
        sessions,
        bindings,
        DeliveryOutbox(root / "proactive_delivery.db"),
        renderer=renderer,
    )

    assert delivery.pump() == 1
    assert len(rendered) == 1
    assert rendered[0].status == "failed"
    assert rendered[0].summary == "Draft PR 发布失败：remote branch unavailable"
    assert "任务已完成" not in rendered[0].summary


def test_commit_subject_uses_only_first_semantic_intent_line() -> None:
    subject = _commit_subject(
        "create graduation evidence and open a Draft PR\n"
        "完成这个持久工程目标需要的仓库修改、必要验证和提交。\n"
        "原始请求：内部约束不应出现在 commit subject"
    )

    assert subject == "hikari: create graduation evidence and open a Draft PR"
