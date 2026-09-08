from __future__ import annotations

from dataclasses import replace
import time
from uuid import NAMESPACE_URL, uuid5

from .goal import (
    EngineeringGoalAdvanceOutcome,
    EngineeringGoalCoordinator,
    EngineeringGoalState,
    EngineeringGoalStep,
    EngineeringGoalStore,
)
from .session import EngineeringSessionStore


_RETRYABLE_EFFECTS = frozenset(
    {
        "inspect_project",
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    }
)


class PersistentMaintainerLoop:
    """Resident-owned continuation policy above deterministic goal coordination.

    One failed step may receive one bounded recovery attempt when the effect is safe to
    resume in the same isolated EngineeringSession. Blocked work never retries, command
    turns never retry automatically, and authority is never expanded. Publication turns
    are safe to retry because push is non-force and Draft PR publication is head-idempotent.
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
        self.coordinator = EngineeringGoalCoordinator(goals, sessions)

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
        outcomes: list[EngineeringGoalAdvanceOutcome] = []
        for goal in self.goals.list_states():
            if goal.status == "active" or self._can_retry(goal):
                outcomes.append(self.advance_once(goal.goal_id))
        return outcomes

    def _recover_retryable_terminal(
        self,
        goal: EngineeringGoalState,
    ) -> EngineeringGoalState | None:
        if not self._can_retry(goal):
            return None
        step = goal.current_step
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
