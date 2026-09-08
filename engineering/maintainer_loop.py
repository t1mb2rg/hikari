from __future__ import annotations

from dataclasses import replace
import time
from uuid import NAMESPACE_URL, uuid5

from .goal import (
    EngineeringGoalAdvanceOutcome,
    EngineeringGoalCoordinator,
    EngineeringGoalState,
    EngineeringGoalStore,
)
from .session import EngineeringSessionStore, EngineeringTurn
from .workspace import EngineeringWorkspace, EngineeringWorkspaceError


_RETRYABLE_EFFECTS = frozenset(
    {
        "inspect_project",
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    }
)


class _WorkerCompatibleGoalCoordinator(EngineeringGoalCoordinator):
    """Keep persistent turn machine fields compatible with the proven Worker parser.

    M7-A turns terminate ``Requested effect`` with a period. Persistent goal context is
    multiline, so canonicalize that one machine field before enqueue rather than letting
    following prose become part of the effect token.
    """

    def _turn_for_step(self, goal, step) -> EngineeringTurn:
        turn = super()._turn_for_step(goal, step)
        marker = f"Requested effect: {step.effect}\n"
        canonical = f"Requested effect: {step.effect}.\n"
        if marker not in turn.context:
            return turn
        return replace(turn, context=turn.context.replace(marker, canonical, 1))


class PersistentMaintainerLoop:
    """Resident-owned selection, continuation, and bounded recovery policy.

    At most one durable goal per project is advanced by each Resident pump. The oldest
    unfinished goal wins, giving Hikari a deterministic project-local work queue instead
    of starting every goal concurrently. One failed safe step may receive one bounded
    recovery attempt in the same isolated EngineeringSession. Blocked work never retries,
    command turns never replay automatically, and authority is never expanded.

    Every automatic maintainer replay starts from the last durable commit. Partial edits
    from a failed agent attempt are discarded only inside Hikari's isolated worktree so a
    retry cannot accidentally stack the same unfinished modification twice.
    """

    def __init__(
        self,
        goals: EngineeringGoalStore,
        sessions: EngineeringSessionStore,
        *,
        max_attempts: int = 2,
    ) -> None:
        if not isinstance(goals, EngineeringGoalStore):
            raise TypeError("PersistentMaintainerLoop requires EngineeringGoalStore")
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("PersistentMaintainerLoop requires EngineeringSessionStore")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self.goals = goals
        self.sessions = sessions
        self.max_attempts = int(max_attempts)
        self.coordinator = _WorkerCompatibleGoalCoordinator(goals, sessions)

    def advance_once(self, goal_id: str) -> EngineeringGoalAdvanceOutcome:
        goal = self.goals.load(goal_id)
        recovered = self._recover_retryable_terminal(goal)
        if recovered is not None:
            self.goals.save(recovered)

        outcome = self.coordinator.advance_once(goal_id)
        if outcome.status != "failed":
            return outcome

        goal = self.goals.load(goal_id)
        recovered = self._recover_retryable_terminal(goal)
        if recovered is None:
            return outcome
        self.goals.save(recovered)
        return self.coordinator.advance_once(goal_id)

    def advance_all(self) -> list[EngineeringGoalAdvanceOutcome]:
        candidates = [
            goal
            for goal in self.goals.list_states()
            if goal.status == "active" or self._can_retry(goal)
        ]
        candidates.sort(key=lambda goal: (goal.created_at, goal.goal_id))

        selected: dict[str, EngineeringGoalState] = {}
        for goal in candidates:
            selected.setdefault(goal.project_id, goal)
        return [self.advance_once(goal.goal_id) for goal in selected.values()]

    def _recover_retryable_terminal(
        self,
        goal: EngineeringGoalState,
    ) -> EngineeringGoalState | None:
        if not self._can_retry(goal):
            return None
        step = goal.current_step

        if step.effect == "maintain_project":
            cleanup_error = self._clean_failed_maintainer_attempt(goal)
            if cleanup_error is not None:
                blocked_step = replace(
                    step,
                    status="blocked",
                    result_status="blocked",
                    result_message=cleanup_error,
                    updated_at=time.time(),
                )
                steps = list(goal.steps)
                steps[goal.current_step_index] = blocked_step
                return replace(
                    goal,
                    status="blocked",
                    steps=tuple(steps),
                    final_summary=cleanup_error,
                    updated_at=time.time(),
                )

        next_attempt = step.attempts + 1
        turn_id = self._stable_turn_id(goal.goal_id, step.step_id, next_attempt)
        retried = replace(
            step,
            status="queued",
            turn_id=turn_id,
            attempts=next_attempt,
            # Preserve the previous failure as durable context until the new result replaces it.
            result_status=step.result_status,
            result_message=step.result_message,
            updated_at=time.time(),
        )
        steps = list(goal.steps)
        steps[goal.current_step_index] = retried
        return replace(
            goal,
            status="active",
            steps=tuple(steps),
            final_summary="",
            updated_at=time.time(),
        )

    def _clean_failed_maintainer_attempt(self, goal: EngineeringGoalState) -> str | None:
        state = self.sessions.load(goal.session_id)
        if not (state.workspace_path and state.workspace_branch and state.baseline_commit):
            return None
        try:
            workspace = EngineeringWorkspace.resume(
                repository=state.repository,
                workspace_path=state.workspace_path,
                branch=state.workspace_branch,
                baseline_commit=state.baseline_commit,
            )
            workspace.discard_uncommitted_changes()
        except (EngineeringWorkspaceError, OSError) as exc:
            return (
                "failed maintainer attempt left an untrusted worktree that could not be "
                f"restored before retry ({type(exc).__name__}); automatic continuation stopped"
            )
        return None

    def _can_retry(self, goal: EngineeringGoalState) -> bool:
        if goal.status != "failed":
            return False
        step = goal.current_step
        return (
            step.status == "failed"
            and step.effect in _RETRYABLE_EFFECTS
            and step.attempts < self.max_attempts
        )

    @staticmethod
    def _stable_turn_id(goal_id: str, step_id: str, attempt: int) -> str:
        return uuid5(
            NAMESPACE_URL,
            f"hikari-engineering-goal:{goal_id}:{step_id}:{attempt}",
        ).hex
