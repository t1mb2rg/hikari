from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .goal import EngineeringGoalState, EngineeringGoalStore
from .maintainer_loop import PersistentMaintainerLoop
from .session import EngineeringProtocolError, EngineeringSessionStore


@dataclass(frozen=True)
class EngineeringCompletionFacts:
    """Machine truth projected from one durable terminal engineering unit.

    ``goal`` may describe a legacy single turn or a persistent multi-step goal. This
    object deliberately contains no user-facing prose. Engineering owns the grounded
    result; a Conversation-owned renderer decides how Jarvis says it.
    """

    status: str
    goal: str
    summary: str
    changed_files: tuple[str, ...] = ()
    branch: str | None = None
    historical: bool = False


EngineeringCompletionRenderer = Callable[[EngineeringCompletionFacts, str, str], str]


class EngineeringCompletionDelivery:
    """Advance goal truth and project terminal facts into Hikari's durable outbox.

    Resident constructs this class with a Conversation-owned renderer. In that mode the
    same one-second pump first advances persistent Engineering Goals, then delivers only
    whole-goal terminal facts. Intermediate goal steps are deliberately suppressed so a
    completed edit turn cannot masquerade as completion of a still-pending push/PR goal.

    Engineering Worker constructs this class without a renderer. It therefore persists
    turn truth only and never owns persistent-goal continuation or Jarvis-facing prose.
    Existing DeliveryOutbox rows remain immutable historical facts.
    """

    def __init__(
        self,
        sessions: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        outbox: DeliveryOutbox,
        *,
        renderer: EngineeringCompletionRenderer | None = None,
    ) -> None:
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringConversationBindingStore")
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("EngineeringCompletionDelivery requires DeliveryOutbox")
        if renderer is not None and not callable(renderer):
            raise TypeError("EngineeringCompletionDelivery renderer must be callable or None")
        self.sessions = sessions
        self.bindings = bindings
        self.router = DeliveryRouter(outbox)
        self.renderer = renderer
        self.goals = EngineeringGoalStore(sessions.root.parent / "engineering_goals")
        self.maintainer_loop = (
            PersistentMaintainerLoop(self.goals, sessions) if renderer is not None else None
        )

    def pump(self) -> int:
        """Idempotently advance persistent goals and enqueue terminal delivery facts."""

        if self.maintainer_loop is not None:
            self.maintainer_loop.advance_all()

        managed_turn_ids = self._managed_turn_ids()
        submitted = self._pump_single_turns(managed_turn_ids)
        submitted += self._pump_terminal_goals()
        return submitted

    def _managed_turn_ids(self) -> set[str]:
        managed: set[str] = set()
        for goal in self.goals.list_states():
            for step in goal.steps:
                if step.turn_id:
                    managed.add(step.turn_id)
        return managed

    def _pump_single_turns(self, managed_turn_ids: set[str]) -> int:
        submitted = 0
        for binding in self.bindings.all():
            if binding.channel != "qq":
                continue
            try:
                state = self.sessions.load(binding.session_id)
            except EngineeringProtocolError:
                continue
            if state.status not in {"completed", "failed", "blocked"}:
                continue
            turn_id = state.current_turn_id
            if not turn_id or turn_id in managed_turn_ids:
                continue
            try:
                result = self.sessions.load_result(state.session_id, turn_id)
                turn = self.sessions.load_turn(state.session_id, turn_id)
            except EngineeringProtocolError:
                continue
            recipient = self._qq_recipient(binding.channel, binding.conversation_id)
            if recipient is None:
                continue

            delivery_id = f"engineering:{state.session_id}:{turn_id}"
            if self._already_submitted(delivery_id):
                submitted += 1
                continue
            if self.renderer is None:
                continue

            current = self.bindings.for_conversation(binding.channel, binding.conversation_id)
            historical = current is not None and current.session_id != state.session_id
            facts = EngineeringCompletionFacts(
                status=result.status,
                goal=turn.intent,
                summary=result.message,
                changed_files=tuple(result.changed_files),
                branch=state.workspace_branch,
                historical=historical,
            )
            if self._submit(
                delivery_id,
                recipient,
                facts,
                binding.channel,
                binding.conversation_id,
            ):
                submitted += 1
        return submitted

    def _pump_terminal_goals(self) -> int:
        if self.renderer is None:
            return 0
        submitted = 0
        for goal in self.goals.list_states():
            if not goal.terminal:
                continue
            if goal.source_channel is None or goal.source_conversation_id is None:
                continue
            recipient = self._qq_recipient(
                goal.source_channel,
                goal.source_conversation_id,
            )
            if recipient is None:
                continue
            delivery_id = f"engineering-goal:{goal.goal_id}"
            if self._already_submitted(delivery_id):
                submitted += 1
                continue
            try:
                state = self.sessions.load(goal.session_id)
            except EngineeringProtocolError:
                continue

            current = self.bindings.for_conversation(
                goal.source_channel,
                goal.source_conversation_id,
            )
            historical = current is not None and current.session_id != goal.session_id
            facts = EngineeringCompletionFacts(
                status=goal.status,
                goal=goal.goal,
                summary=self._goal_summary(goal),
                changed_files=self._goal_changed_files(goal),
                branch=state.workspace_branch,
                historical=historical,
            )
            if self._submit(
                delivery_id,
                recipient,
                facts,
                goal.source_channel,
                goal.source_conversation_id,
            ):
                submitted += 1
        return submitted

    def _goal_summary(self, goal: EngineeringGoalState) -> str:
        lines: list[str] = []
        for index, step in enumerate(goal.steps, start=1):
            if not step.result_message:
                continue
            lines.append(
                f"step {index}/{len(goal.steps)} {step.effect} [{step.status}]: "
                f"{step.result_message}"
            )
        if lines:
            return "\n".join(lines)
        return goal.final_summary or f"persistent engineering goal ended with {goal.status}"

    def _goal_changed_files(self, goal: EngineeringGoalState) -> tuple[str, ...]:
        changed: list[str] = []
        seen: set[str] = set()
        for step in goal.steps:
            if not step.turn_id:
                continue
            try:
                result = self.sessions.load_result(goal.session_id, step.turn_id)
            except EngineeringProtocolError:
                continue
            for item in result.changed_files:
                if item not in seen:
                    seen.add(item)
                    changed.append(item)
        return tuple(changed)

    @staticmethod
    def _qq_recipient(channel: str, conversation_id: str) -> str | None:
        if channel != "qq" or not conversation_id.startswith("private:"):
            return None
        recipient = conversation_id.removeprefix("private:").strip()
        return recipient or None

    def _already_submitted(self, delivery_id: str) -> bool:
        try:
            existing = self.router.outbox.get(delivery_id)
        except Exception:
            existing = None
        return existing is not None and existing.state in {
            "pending",
            "sending",
            "sent",
            "uncertain",
        }

    def _submit(
        self,
        delivery_id: str,
        recipient: str,
        facts: EngineeringCompletionFacts,
        channel: str,
        conversation_id: str,
    ) -> bool:
        if self.renderer is None:
            return False
        text = self.renderer(facts, channel, conversation_id)
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
        return record.state in {"pending", "sending", "sent", "uncertain"}
