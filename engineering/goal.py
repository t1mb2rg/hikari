from __future__ import annotations

from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import time
from typing import Iterable, Mapping
from uuid import NAMESPACE_URL, uuid4, uuid5

from .effects import SUPPORTED_ENGINEERING_EFFECTS, authority_for_effect
from .session import (
    EngineeringProtocolError,
    EngineeringResult,
    EngineeringSessionStore,
    EngineeringTurn,
    _optional_text,
    _text_items,
)


_GOAL_STATUSES = frozenset({"active", "completed", "failed", "blocked"})
_STEP_STATUSES = frozenset({"pending", "queued", "running", "completed", "failed", "blocked"})
_TERMINAL_GOAL_STATUSES = frozenset({"completed", "failed", "blocked"})
_TERMINAL_STEP_STATUSES = frozenset({"completed", "failed", "blocked"})


@dataclass(frozen=True, slots=True)
class EngineeringGoalStep:
    step_id: str
    effect: str
    instruction: str
    status: str = "pending"
    turn_id: str | None = None
    attempts: int = 0
    result_status: str | None = None
    result_message: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        step_id = self.step_id.strip()
        effect = self.effect.strip()
        instruction = self.instruction.strip()
        status = self.status.strip().lower()
        turn_id = self.turn_id.strip() if isinstance(self.turn_id, str) and self.turn_id.strip() else None
        result_status = (
            self.result_status.strip().lower()
            if isinstance(self.result_status, str) and self.result_status.strip()
            else None
        )
        if not step_id:
            raise EngineeringProtocolError("engineering goal step_id must not be empty")
        if effect not in SUPPORTED_ENGINEERING_EFFECTS:
            raise EngineeringProtocolError(f"unsupported engineering goal effect: {effect!r}")
        if not instruction:
            raise EngineeringProtocolError("engineering goal step instruction must not be empty")
        if status not in _STEP_STATUSES:
            raise EngineeringProtocolError(f"unsupported engineering goal step status: {status!r}")
        if not isinstance(self.attempts, int) or isinstance(self.attempts, bool) or self.attempts < 0:
            raise EngineeringProtocolError("engineering goal step attempts must be an integer >= 0")
        if result_status is not None and result_status not in {"completed", "failed", "blocked"}:
            raise EngineeringProtocolError(
                f"unsupported engineering goal result status: {result_status!r}"
            )
        if status in _TERMINAL_STEP_STATUSES and result_status != status:
            raise EngineeringProtocolError(
                "terminal engineering goal step status requires matching result_status"
            )
        if turn_id is None and self.attempts:
            raise EngineeringProtocolError("engineering goal step attempts require a turn_id")
        now = time.time()
        created_at = float(self.created_at or now)
        updated_at = float(self.updated_at or created_at)
        object.__setattr__(self, "step_id", step_id)
        object.__setattr__(self, "effect", effect)
        object.__setattr__(self, "instruction", instruction)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "result_status", result_status)
        object.__setattr__(self, "result_message", self.result_message.strip())
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def create(cls, *, effect: str, instruction: str, step_id: str | None = None) -> "EngineeringGoalStep":
        now = time.time()
        return cls(
            step_id=step_id or uuid4().hex,
            effect=effect,
            instruction=instruction,
            created_at=now,
            updated_at=now,
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "step_id": self.step_id,
            "effect": self.effect,
            "instruction": self.instruction,
            "status": self.status,
            "turn_id": self.turn_id,
            "attempts": self.attempts,
            "result_status": self.result_status,
            "result_message": self.result_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringGoalStep":
        return cls(
            step_id=str(payload.get("step_id", "")),
            effect=str(payload.get("effect", "")),
            instruction=str(payload.get("instruction", "")),
            status=str(payload.get("status", "")),
            turn_id=(str(payload["turn_id"]) if payload.get("turn_id") is not None else None),
            attempts=int(payload.get("attempts", 0)),
            result_status=(
                str(payload["result_status"])
                if payload.get("result_status") is not None
                else None
            ),
            result_message=str(payload.get("result_message", "")),
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class EngineeringGoalState:
    goal_id: str
    project_id: str
    session_id: str
    goal: str
    steps: tuple[EngineeringGoalStep, ...]
    status: str = "active"
    current_step_index: int = 0
    source_channel: str | None = None
    source_conversation_id: str | None = None
    final_summary: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0
    constraints: tuple[str, ...] = ()
    acceptance_criteria: tuple[str, ...] = ()
    source_request_id: str | None = None

    def __post_init__(self) -> None:
        goal_id = self.goal_id.strip()
        project_id = self.project_id.strip()
        session_id = self.session_id.strip()
        goal = self.goal.strip()
        status = self.status.strip().lower()
        source_channel = (
            self.source_channel.strip()
            if isinstance(self.source_channel, str) and self.source_channel.strip()
            else None
        )
        source_conversation_id = (
            self.source_conversation_id.strip()
            if isinstance(self.source_conversation_id, str) and self.source_conversation_id.strip()
            else None
        )
        if not goal_id or not project_id or not session_id or not goal:
            raise EngineeringProtocolError(
                "engineering goal requires goal_id, project_id, session_id, and goal"
            )
        if status not in _GOAL_STATUSES:
            raise EngineeringProtocolError(f"unsupported engineering goal status: {status!r}")
        if not self.steps:
            raise EngineeringProtocolError("engineering goal requires at least one step")
        if not all(isinstance(step, EngineeringGoalStep) for step in self.steps):
            raise TypeError("engineering goal steps must be EngineeringGoalStep values")
        if len({step.step_id for step in self.steps}) != len(self.steps):
            raise EngineeringProtocolError("engineering goal step_id values must be unique")
        if not isinstance(self.current_step_index, int) or isinstance(self.current_step_index, bool):
            raise TypeError("engineering goal current_step_index must be an integer")
        if not 0 <= self.current_step_index < len(self.steps):
            raise EngineeringProtocolError("engineering goal current_step_index is out of range")
        if (source_channel is None) != (source_conversation_id is None):
            raise EngineeringProtocolError(
                "engineering goal source channel and conversation id must be supplied together"
            )
        now = time.time()
        created_at = float(self.created_at or now)
        updated_at = float(self.updated_at or created_at)
        object.__setattr__(self, "goal_id", goal_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "source_channel", source_channel)
        object.__setattr__(self, "source_conversation_id", source_conversation_id)
        object.__setattr__(self, "constraints", _text_items(self.constraints, name="constraints"))
        object.__setattr__(self, "acceptance_criteria", _text_items(self.acceptance_criteria, name="acceptance_criteria"))
        object.__setattr__(self, "source_request_id", _optional_text(self.source_request_id, name="source_request_id"))
        object.__setattr__(self, "final_summary", self.final_summary.strip())
        object.__setattr__(self, "created_at", created_at)
        object.__setattr__(self, "updated_at", updated_at)

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        session_id: str,
        goal: str,
        steps: Iterable[EngineeringGoalStep],
        source_channel: str | None = None,
        source_conversation_id: str | None = None,
        goal_id: str | None = None,
        constraints: tuple[str, ...] = (),
        acceptance_criteria: tuple[str, ...] = (),
        source_request_id: str | None = None,
    ) -> "EngineeringGoalState":
        now = time.time()
        return cls(
            goal_id=goal_id or uuid4().hex,
            project_id=project_id,
            session_id=session_id,
            goal=goal,
            steps=tuple(steps),
            source_channel=source_channel,
            source_conversation_id=source_conversation_id,
            constraints=constraints,
            acceptance_criteria=acceptance_criteria,
            source_request_id=source_request_id,
            created_at=now,
            updated_at=now,
        )

    @property
    def current_step(self) -> EngineeringGoalStep:
        return self.steps[self.current_step_index]

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_GOAL_STATUSES

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "goal_id": self.goal_id,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "goal": self.goal,
            "status": self.status,
            "current_step_index": self.current_step_index,
            "source_channel": self.source_channel,
            "source_conversation_id": self.source_conversation_id,
            "constraints": list(self.constraints),
            "acceptance_criteria": list(self.acceptance_criteria),
            "source_request_id": self.source_request_id,
            "final_summary": self.final_summary,
            "steps": [step.to_mapping() for step in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringGoalState":
        if payload.get("version") != 1:
            raise EngineeringProtocolError("unsupported engineering goal version")
        raw_steps = payload.get("steps")
        if not isinstance(raw_steps, list):
            raise TypeError("engineering goal steps must be a list")
        steps: list[EngineeringGoalStep] = []
        for item in raw_steps:
            if not isinstance(item, Mapping):
                raise TypeError("engineering goal step must be an object")
            steps.append(EngineeringGoalStep.from_mapping(item))
        return cls(
            goal_id=str(payload.get("goal_id", "")),
            project_id=str(payload.get("project_id", "")),
            session_id=str(payload.get("session_id", "")),
            goal=str(payload.get("goal", "")),
            steps=tuple(steps),
            status=str(payload.get("status", "")),
            current_step_index=int(payload.get("current_step_index", 0)),
            source_channel=(
                str(payload["source_channel"])
                if payload.get("source_channel") is not None
                else None
            ),
            source_conversation_id=(
                str(payload["source_conversation_id"])
                if payload.get("source_conversation_id") is not None
                else None
            ),
            final_summary=str(payload.get("final_summary", "")),
            constraints=_text_items(payload.get("constraints", ()), name="constraints"),
            acceptance_criteria=_text_items(payload.get("acceptance_criteria", ()), name="acceptance_criteria"),
            source_request_id=_optional_text(payload.get("source_request_id"), name="source_request_id"),
            created_at=float(payload.get("created_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )


class EngineeringGoalStoreError(EngineeringProtocolError):
    """Goal ownership cannot be determined; intake, scheduling and delivery must stop."""


class EngineeringGoalStore:
    """Durable goal-level truth above single-turn EngineeringSession execution."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def create(self, goal: EngineeringGoalState) -> EngineeringGoalState:
        path = self._path(goal.goal_id)
        if path.exists():
            raise EngineeringProtocolError(f"engineering goal already exists: {goal.goal_id}")
        self.save(goal)
        return goal

    def load(self, goal_id: str) -> EngineeringGoalState:
        path = self._path(goal_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EngineeringProtocolError(f"unknown engineering goal: {goal_id}") from None
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise EngineeringProtocolError(f"engineering goal is unreadable: {goal_id}") from None
        if not isinstance(payload, Mapping):
            raise EngineeringProtocolError("engineering goal must be an object")
        try:
            goal = EngineeringGoalState.from_mapping(payload)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise EngineeringProtocolError(f"engineering goal schema is invalid: {goal_id}") from None
        if goal.goal_id != goal_id:
            raise EngineeringProtocolError(f"engineering goal identity does not match filename: {goal_id}")
        return goal

    def save(self, goal: EngineeringGoalState) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(goal.goal_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(goal.to_mapping(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def list_states(self) -> list[EngineeringGoalState]:
        if not self.root.exists():
            return []
        if not self.root.is_dir():
            raise EngineeringGoalStoreError("engineering goal store is not a directory")
        goals: list[EngineeringGoalState] = []
        unreadable: list[str] = []
        try:
            paths = sorted(path for path in self.root.iterdir() if path.suffix == ".json")
        except OSError:
            raise EngineeringGoalStoreError("engineering goal store cannot be listed") from None
        for path in paths:
            try:
                goals.append(self.load(path.stem))
            except EngineeringProtocolError:
                unreadable.append(path.stem)
        if unreadable:
            raise EngineeringGoalStoreError(
                "unreadable engineering goal record(s): " + ", ".join(unreadable)
            )
        return sorted(goals, key=lambda item: item.updated_at)

    def _path(self, goal_id: str) -> Path:
        normalized = goal_id.strip()
        if not normalized or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for ch in normalized
        ):
            raise EngineeringProtocolError("engineering goal_id contains unsupported characters")
        return self.root / f"{normalized}.json"


@dataclass(frozen=True, slots=True)
class EngineeringGoalAdvanceOutcome:
    goal_id: str
    status: str
    action: str
    step_id: str | None = None
    turn_id: str | None = None
    message: str = ""


class EngineeringGoalCoordinator:
    """Advance persistent goals strictly from durable EngineeringSession/Result truth.

    The coordinator does not interpret natural language, call a model, expand authority, or
    retry failed work. Its first responsibility is idempotent continuation across process
    restarts. Retry policy is deliberately a later layer.
    """

    def __init__(self, goals: EngineeringGoalStore, sessions: EngineeringSessionStore) -> None:
        if not isinstance(goals, EngineeringGoalStore):
            raise TypeError("EngineeringGoalCoordinator requires EngineeringGoalStore")
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringGoalCoordinator requires EngineeringSessionStore")
        self.goals = goals
        self.sessions = sessions

    def advance_once(self, goal_id: str) -> EngineeringGoalAdvanceOutcome:
        goal = self.goals.load(goal_id)
        if goal.terminal:
            return EngineeringGoalAdvanceOutcome(
                goal.goal_id,
                goal.status,
                "terminal",
                step_id=goal.current_step.step_id,
                turn_id=goal.current_step.turn_id,
                message=goal.final_summary,
            )

        while True:
            step = goal.current_step
            session = self.sessions.load(goal.session_id)

            if step.turn_id is not None:
                result = self._load_result_or_none(goal.session_id, step.turn_id)
                if result is not None:
                    goal = self._apply_terminal_result(goal, result)
                    self.goals.save(goal)
                    if goal.terminal:
                        return EngineeringGoalAdvanceOutcome(
                            goal.goal_id,
                            goal.status,
                            "goal_terminal",
                            step_id=goal.current_step.step_id,
                            turn_id=goal.current_step.turn_id,
                            message=goal.final_summary,
                        )
                    continue

                if session.current_turn_id == step.turn_id:
                    if session.status in {"pending", "running"}:
                        desired = "queued" if session.status == "pending" else "running"
                        if step.status != desired:
                            goal = self._replace_current_step(
                                goal,
                                replace(step, status=desired, updated_at=time.time()),
                            )
                            self.goals.save(goal)
                        return EngineeringGoalAdvanceOutcome(
                            goal.goal_id,
                            goal.status,
                            "waiting",
                            step_id=step.step_id,
                            turn_id=step.turn_id,
                            message=session.latest_summary,
                        )
                    if session.status in {"completed", "failed", "blocked"}:
                        return self._block_for_missing_result(goal, session.status)

                if session.status in {"pending", "running"}:
                    return self._block_for_conflicting_turn(goal, session.current_turn_id)

                # Goal state was persisted before enqueue, or Resident died between those writes.
                # Reconstruct exactly the same deterministic turn id and enqueue it again.
                turn = self._turn_for_step(goal, step)
                try:
                    self.sessions.enqueue_turn(goal.session_id, turn)
                except EngineeringProtocolError as exc:
                    return EngineeringGoalAdvanceOutcome(
                        goal.goal_id,
                        goal.status,
                        "deferred",
                        step_id=step.step_id,
                        turn_id=step.turn_id,
                        message=str(exc),
                    )
                return EngineeringGoalAdvanceOutcome(
                    goal.goal_id,
                    goal.status,
                    "recovered_enqueue",
                    step_id=step.step_id,
                    turn_id=step.turn_id,
                )

            if session.status in {"pending", "running"}:
                return self._block_for_conflicting_turn(goal, session.current_turn_id)

            turn_id = self._stable_turn_id(goal.goal_id, step.step_id, step.attempts + 1)
            queued = replace(
                step,
                status="queued",
                turn_id=turn_id,
                attempts=step.attempts + 1,
                updated_at=time.time(),
            )
            goal = self._replace_current_step(goal, queued)
            self.goals.save(goal)
            turn = self._turn_for_step(goal, queued)
            try:
                self.sessions.enqueue_turn(goal.session_id, turn)
            except EngineeringProtocolError as exc:
                return EngineeringGoalAdvanceOutcome(
                    goal.goal_id,
                    goal.status,
                    "deferred",
                    step_id=queued.step_id,
                    turn_id=queued.turn_id,
                    message=str(exc),
                )
            return EngineeringGoalAdvanceOutcome(
                goal.goal_id,
                goal.status,
                "enqueued",
                step_id=queued.step_id,
                turn_id=queued.turn_id,
            )

    def advance_all(self) -> list[EngineeringGoalAdvanceOutcome]:
        outcomes: list[EngineeringGoalAdvanceOutcome] = []
        for goal in self.goals.list_states():
            if goal.status == "active":
                outcomes.append(self.advance_once(goal.goal_id))
        return outcomes

    @staticmethod
    def _stable_turn_id(goal_id: str, step_id: str, attempt: int) -> str:
        return uuid5(NAMESPACE_URL, f"hikari-engineering-goal:{goal_id}:{step_id}:{attempt}").hex

    @staticmethod
    def _replace_current_step(
        goal: EngineeringGoalState,
        step: EngineeringGoalStep,
    ) -> EngineeringGoalState:
        steps = list(goal.steps)
        steps[goal.current_step_index] = step
        return replace(goal, steps=tuple(steps), updated_at=time.time())

    def _turn_for_step(
        self,
        goal: EngineeringGoalState,
        step: EngineeringGoalStep,
    ) -> EngineeringTurn:
        if step.turn_id is None:
            raise EngineeringProtocolError("engineering goal step has no deterministic turn_id")
        return EngineeringTurn(
            turn_id=step.turn_id,
            intent=step.instruction,
            context=(
                "This turn belongs to a persistent Hikari Engineering Goal.\n"
                f"Persistent goal id: {goal.goal_id}\n"
                f"Persistent step id: {step.step_id}\n"
                f"Persistent goal: {goal.goal}\n"
                f"User constraints: {json.dumps(goal.constraints, ensure_ascii=False)}\n"
                f"Acceptance criteria: {json.dumps(goal.acceptance_criteria, ensure_ascii=False)}\n"
                f"Requested effect: {step.effect}\n"
                "Do only this step. Hikari owns continuation to later steps."
            ),
            authority=authority_for_effect(step.effect),
            created_at=step.created_at,
            effect=step.effect,
            constraints=goal.constraints,
            acceptance_criteria=goal.acceptance_criteria,
            source_request_id=goal.source_request_id,
        )

    def _load_result_or_none(self, session_id: str, turn_id: str) -> EngineeringResult | None:
        try:
            return self.sessions.load_result(session_id, turn_id)
        except EngineeringProtocolError as exc:
            if str(exc).startswith("unknown engineering result:"):
                return None
            raise

    def _apply_terminal_result(
        self,
        goal: EngineeringGoalState,
        result: EngineeringResult,
    ) -> EngineeringGoalState:
        step = goal.current_step
        if result.turn_id != step.turn_id:
            raise EngineeringProtocolError("engineering goal result does not match current step")
        terminal_step = replace(
            step,
            status=result.status,
            result_status=result.status,
            result_message=result.message,
            updated_at=result.completed_at,
        )
        goal = self._replace_current_step(goal, terminal_step)
        if result.status == "failed":
            return replace(
                goal,
                status="failed",
                final_summary=result.message,
                updated_at=result.completed_at,
            )
        if result.status == "blocked":
            return replace(
                goal,
                status="blocked",
                final_summary=result.message,
                updated_at=result.completed_at,
            )
        if goal.current_step_index == len(goal.steps) - 1:
            return replace(
                goal,
                status="completed",
                final_summary=result.message,
                updated_at=result.completed_at,
            )
        return replace(
            goal,
            current_step_index=goal.current_step_index + 1,
            updated_at=result.completed_at,
        )

    def _block_for_conflicting_turn(
        self,
        goal: EngineeringGoalState,
        turn_id: str | None,
    ) -> EngineeringGoalAdvanceOutcome:
        message = (
            "persistent engineering goal detected a different active EngineeringSession turn; "
            "automatic continuation stopped to avoid duplicate or interleaved execution"
        )
        blocked = replace(goal, status="blocked", final_summary=message, updated_at=time.time())
        self.goals.save(blocked)
        return EngineeringGoalAdvanceOutcome(
            goal.goal_id,
            "blocked",
            "conflict",
            step_id=goal.current_step.step_id,
            turn_id=turn_id,
            message=message,
        )

    def _block_for_missing_result(
        self,
        goal: EngineeringGoalState,
        session_status: str,
    ) -> EngineeringGoalAdvanceOutcome:
        message = (
            f"EngineeringSession is terminal ({session_status}) for the persistent goal step, "
            "but the durable EngineeringResult is missing; automatic continuation stopped"
        )
        blocked = replace(goal, status="blocked", final_summary=message, updated_at=time.time())
        self.goals.save(blocked)
        return EngineeringGoalAdvanceOutcome(
            goal.goal_id,
            "blocked",
            "missing_result",
            step_id=goal.current_step.step_id,
            turn_id=goal.current_step.turn_id,
            message=message,
        )
