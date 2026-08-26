from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import json
from numbers import Real
from typing import Any

from attention import AttentionDecision
from brain import ChatMessage, ChatProvider, Feedback
from events import Event

from .contract import ActionCatalog, ActionProposal


ACTION_PLANNER_INSTRUCTIONS = """You are Hikari's bounded action-planning layer.
You may propose at most one action from the explicit allowed-actions catalog supplied in the request.
A proposal is only an intent for later review. It does not execute anything.
Never invent an action name, permission, capability, or argument that is not justified by the current event and context.
Do not decide risk or confirmation requirements; those are controlled by trusted code outside the model.
If no allowed action is clearly useful, return decision `none`.
Return JSON only, using exactly one of these shapes:
{"decision":"none","reason":"..."}
{"decision":"propose","action":"registered_action_name","arguments":{},"effect":"...","reason":"...","confidence":0.0}
Confidence must be between 0 and 1."""


class ActionPlanningError(RuntimeError):
    """Raised when the model returns an invalid or unauthorized action proposal."""


class ModelActionPlanner:
    """Turns cognition into one reviewable ActionProposal without execution."""

    def __init__(self, provider: ChatProvider, catalog: ActionCatalog) -> None:
        self.provider = provider
        self.catalog = catalog

    def plan(
        self,
        event: Event,
        decision: AttentionDecision,
        *,
        feedback: Feedback | None = None,
    ) -> ActionProposal | None:
        if not decision.should_intervene:
            return None

        payload = {
            "event": {
                "type": event.event_type,
                "source": event.source,
                "content": event.content,
                "occurred_at": event.occurred_at,
            },
            "attention": {
                "importance": decision.importance,
                "reason": decision.reason,
            },
            "context": event.context,
            "cognition_feedback": feedback.text if feedback is not None else None,
            "allowed_actions": self.catalog.describe(),
        }
        messages: Sequence[ChatMessage] = (
            ChatMessage(role="system", content=ACTION_PLANNER_INSTRUCTIONS),
            ChatMessage(
                role="user",
                content=json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    default=_json_default,
                ),
            ),
        )

        raw = self.provider.complete(messages).strip()
        response = _parse_response(raw)
        model_decision = response.get("decision")

        if model_decision == "none":
            reason = response.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise ActionPlanningError("action `none` decision requires a reason")
            return None

        if model_decision != "propose":
            raise ActionPlanningError("action decision must be `none` or `propose`")

        action_name = response.get("action")
        if not isinstance(action_name, str) or not action_name.strip():
            raise ActionPlanningError("action proposal requires a non-empty action name")
        spec = self.catalog.get(action_name.strip())
        if spec is None:
            raise ActionPlanningError(f"model proposed unregistered action: {action_name!r}")

        arguments = response.get("arguments")
        if not isinstance(arguments, dict):
            raise ActionPlanningError("action arguments must be a JSON object")
        _ensure_json(arguments, "action arguments")

        effect = response.get("effect")
        reason = response.get("reason")
        if not isinstance(effect, str) or not effect.strip():
            raise ActionPlanningError("action effect must not be empty")
        if not isinstance(reason, str) or not reason.strip():
            raise ActionPlanningError("action reason must not be empty")

        confidence_raw = response.get("confidence")
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, Real):
            raise ActionPlanningError("action confidence must be numeric")
        confidence = float(confidence_raw)
        if not 0.0 <= confidence <= 1.0:
            raise ActionPlanningError("action confidence must be between 0 and 1")

        return ActionProposal(
            action_name=spec.name,
            arguments=arguments,
            effect=effect,
            reason=reason,
            confidence=confidence,
            risk=spec.risk,
            requires_confirmation=spec.requires_confirmation,
        )


def _parse_response(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
            if text.startswith("json\n"):
                text = text[5:].lstrip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ActionPlanningError("action planner returned invalid JSON") from exc

    if not isinstance(value, dict):
        raise ActionPlanningError("action planner response must be a JSON object")
    return value


def _ensure_json(value: object, label: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ActionPlanningError(f"{label} must be JSON-serializable") from exc


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)
