from __future__ import annotations

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .delivery import EngineeringCompletionFacts, EngineeringCompletionRenderer
from .goal import EngineeringGoalStore
from .goal_index import goal_for_turn
from .session import EngineeringProtocolError, EngineeringSessionStore


class EngineeringGoalAwareCompletionDelivery:
    """Deliver one Jarvis terminal reply per persistent goal, not per internal step.

    Single-turn EngineeringSession behavior remains unchanged. When a terminal turn belongs
    to a persistent goal, intermediate step results are suppressed while the goal is active.
    Once the goal itself becomes terminal, one durable goal-level delivery is emitted with
    aggregated changed-file evidence.
    """

    def __init__(
        self,
        sessions: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        goals: EngineeringGoalStore,
        outbox: DeliveryOutbox,
        *,
        renderer: EngineeringCompletionRenderer | None = None,
    ) -> None:
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringGoalAwareCompletionDelivery requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError(
                "EngineeringGoalAwareCompletionDelivery requires EngineeringConversationBindingStore"
            )
        if not isinstance(goals, EngineeringGoalStore):
            raise TypeError("EngineeringGoalAwareCompletionDelivery requires EngineeringGoalStore")
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("EngineeringGoalAwareCompletionDelivery requires DeliveryOutbox")
        if renderer is not None and not callable(renderer):
            raise TypeError("engineering completion renderer must be callable or None")
        self.sessions = sessions
        self.bindings = bindings
        self.goals = goals
        self.router = DeliveryRouter(outbox)
        self.renderer = renderer

    def pump(self) -> int:
        submitted = 0
        for binding in self.bindings.all():
            if binding.channel != "qq" or not binding.conversation_id.startswith("private:"):
                continue
            try:
                state = self.sessions.load(binding.session_id)
            except EngineeringProtocolError:
                continue
            if state.status not in {"completed", "failed", "blocked"}:
                continue
            turn_id = state.current_turn_id
            if not turn_id:
                continue
            try:
                result = self.sessions.load_result(state.session_id, turn_id)
                turn = self.sessions.load_turn(state.session_id, turn_id)
            except EngineeringProtocolError:
                continue

            recipient = binding.conversation_id.removeprefix("private:").strip()
            if not recipient:
                continue
            current = self.bindings.for_conversation(binding.channel, binding.conversation_id)
            historical = current is not None and current.session_id != state.session_id

            goal = goal_for_turn(self.goals, state.session_id, turn_id)
            if goal is not None:
                if not goal.terminal:
                    # The EngineeringSession is terminal for this step, but the persistent
                    # user goal still has work left. Resident must not say the task is done.
                    continue
                delivery_id = f"engineering-goal:{goal.goal_id}"
                facts = EngineeringCompletionFacts(
                    status=goal.status,
                    goal=goal.goal,
                    summary=goal.final_summary or result.message,
                    changed_files=self._goal_changed_files(goal.session_id, goal),
                    branch=state.workspace_branch,
                    historical=historical,
                )
            else:
                delivery_id = f"engineering:{state.session_id}:{turn_id}"
                facts = EngineeringCompletionFacts(
                    status=result.status,
                    goal=turn.intent,
                    summary=result.message,
                    changed_files=tuple(result.changed_files),
                    branch=state.workspace_branch,
                    historical=historical,
                )

            try:
                existing = self.router.outbox.get(delivery_id)
            except Exception:
                existing = None
            if existing is not None:
                if existing.state in {"pending", "sending", "sent", "uncertain"}:
                    submitted += 1
                continue
            if self.renderer is None:
                continue

            text = self.renderer(facts, binding.channel, binding.conversation_id)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("engineering completion renderer returned empty text")
            record = self.router.submit(
                DeliveryRequest(
                    delivery_id=delivery_id,
                    channel="qq",
                    recipient=recipient,
                    text=text.strip(),
                    source="engineering",
                )
            )
            if record.state in {"pending", "sending", "sent", "uncertain"}:
                submitted += 1
        return submitted

    def _goal_changed_files(self, session_id: str, goal) -> tuple[str, ...]:
        files: list[str] = []
        seen: set[str] = set()
        for step in goal.steps:
            if not step.turn_id:
                continue
            try:
                result = self.sessions.load_result(session_id, step.turn_id)
            except EngineeringProtocolError:
                continue
            for path in result.changed_files:
                normalized = str(path).strip()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    files.append(normalized)
        return tuple(files)
