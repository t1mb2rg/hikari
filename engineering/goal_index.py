from __future__ import annotations

from .goal import EngineeringGoalState, EngineeringGoalStore


def active_goal_for_session(
    goals: EngineeringGoalStore,
    session_id: str,
) -> EngineeringGoalState | None:
    matches = [
        goal
        for goal in goals.list_states()
        if goal.session_id == session_id and goal.status == "active"
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: item.updated_at)


def latest_goal_for_session(
    goals: EngineeringGoalStore,
    session_id: str,
) -> EngineeringGoalState | None:
    matches = [goal for goal in goals.list_states() if goal.session_id == session_id]
    if not matches:
        return None
    return max(matches, key=lambda item: item.updated_at)


def goal_for_turn(
    goals: EngineeringGoalStore,
    session_id: str,
    turn_id: str,
) -> EngineeringGoalState | None:
    for goal in reversed(goals.list_states()):
        if goal.session_id != session_id:
            continue
        if any(step.turn_id == turn_id for step in goal.steps):
            return goal
    return None
