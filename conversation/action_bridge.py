from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from threading import Lock
import time
from typing import Callable, Mapping

from actions import (
    ActionAuthorizationPolicy,
    ActionCatalog,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    AuthorizationDecision,
    ForgeProjectProfile,
    ForgeProjectRegistry,
    ForgeTaskAdapter,
    ModelActionPlanner,
    forge_task_action_spec,
)
from attention import AttentionDecision
from brain import ChatProvider
from events import Event

from .engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from .models import AssistantReply, UserTurn


logger = logging.getLogger(__name__)

DEFAULT_CONFIRMATION_TTL_SECONDS = 600.0
DEFAULT_FORGE_PROJECT_ID = "hikari"
DEFAULT_FORGE_BACKEND = "claude"
DEFAULT_FORGE_EXECUTABLE = "forge"
DEFAULT_FORGE_MAX_ATTEMPTS = 3
DEFAULT_FORGE_CLAUDE_PERMISSION_MODE = "auto"
DEFAULT_FORGE_CLAUDE_MAX_TURNS = 30
DEFAULT_FORGE_VERIFICATION = (
    subprocess.list2cmdline([sys.executable, "-m", "pytest", "-q"]),
)

_CONFIRM_WORDS = frozenset({"yes", "y", "确认", "确认执行"})
_CANCEL_WORDS = frozenset({"no", "n", "取消", "取消执行"})
_ENGINEERING_NOUNS = (
    "forge",
    "代码",
    "仓库",
    "模块",
    "功能",
    "项目",
    "hikari",
    "光",
)
_ENGINEERING_VERBS = (
    "修改",
    "改",
    "修复",
    "实现",
    "添加",
    "新增",
    "接入",
    "重构",
    "开发",
    "写",
    "补",
)


@dataclass(frozen=True)
class PendingForgeAction:
    channel: str
    conversation_id: str
    proposal: ActionProposal
    created_at: float

    def __post_init__(self) -> None:
        if not self.channel.strip():
            raise ValueError("pending Forge action channel must not be empty")
        if not self.conversation_id.strip():
            raise ValueError("pending Forge action conversation_id must not be empty")
        if not isinstance(self.proposal, ActionProposal):
            raise TypeError("pending Forge action requires ActionProposal")
        object.__setattr__(self, "channel", self.channel.strip())
        object.__setattr__(self, "conversation_id", self.conversation_id.strip())
        object.__setattr__(self, "created_at", float(self.created_at))

    def expired(self, now: float, ttl_seconds: float) -> bool:
        return float(now) - self.created_at >= float(ttl_seconds)


class PendingForgeActionStore:
    """Tiny durable confirmation store scoped to channel + conversation.

    Pending proposals contain only planner output already restricted by the
    trusted action catalog. They never contain Forge credentials or model API
    secrets. Writes are atomic so a restart cannot turn one acknowledgement into
    two executions.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = Lock()

    @staticmethod
    def _key(channel: str, conversation_id: str) -> str:
        return json.dumps([channel, conversation_id], ensure_ascii=False, separators=(",", ":"))

    def get(self, channel: str, conversation_id: str) -> PendingForgeAction | None:
        key = self._key(channel, conversation_id)
        with self._lock:
            payload = self._load_all()
            raw = payload.get(key)
            if not isinstance(raw, dict):
                return None
            try:
                return _pending_from_payload(raw)
            except (TypeError, ValueError, KeyError):
                return None

    def put(self, pending: PendingForgeAction) -> None:
        key = self._key(pending.channel, pending.conversation_id)
        with self._lock:
            payload = self._load_all()
            payload[key] = _pending_to_payload(pending)
            self._save_all(payload)

    def delete(self, channel: str, conversation_id: str) -> None:
        key = self._key(channel, conversation_id)
        with self._lock:
            payload = self._load_all()
            if key not in payload:
                return
            del payload[key]
            self._save_all(payload)

    def _load_all(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _save_all(self, payload: Mapping[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


def _pending_to_payload(pending: PendingForgeAction) -> dict[str, object]:
    proposal = pending.proposal
    return {
        "version": 1,
        "channel": pending.channel,
        "conversation_id": pending.conversation_id,
        "created_at": pending.created_at,
        "proposal": {
            "action_name": proposal.action_name,
            "arguments": proposal.arguments,
            "effect": proposal.effect,
            "reason": proposal.reason,
            "confidence": proposal.confidence,
            "risk": proposal.risk.value,
            "requires_confirmation": proposal.requires_confirmation,
        },
    }


def _pending_from_payload(payload: Mapping[str, object]) -> PendingForgeAction:
    if payload.get("version") != 1:
        raise ValueError("unsupported pending Forge action version")
    proposal_raw = payload["proposal"]
    if not isinstance(proposal_raw, Mapping):
        raise TypeError("pending Forge proposal must be an object")
    arguments = proposal_raw.get("arguments")
    if not isinstance(arguments, dict):
        raise TypeError("pending Forge arguments must be an object")
    proposal = ActionProposal(
        action_name=str(proposal_raw["action_name"]),
        arguments=dict(arguments),
        effect=str(proposal_raw["effect"]),
        reason=str(proposal_raw["reason"]),
        confidence=float(proposal_raw["confidence"]),
        risk=ActionRisk(str(proposal_raw["risk"])),
        requires_confirmation=bool(proposal_raw["requires_confirmation"]),
    )
    return PendingForgeAction(
        channel=str(payload["channel"]),
        conversation_id=str(payload["conversation_id"]),
        proposal=proposal,
        created_at=float(payload["created_at"]),
    )


def _normalized_control_text(text: str) -> str:
    normalized = text.strip().casefold()
    return normalized.strip("。.!！?？ ")


def _looks_like_engineering_intent(text: str) -> bool:
    normalized = text.casefold()
    if "forge" in normalized:
        return True
    return any(noun in normalized for noun in _ENGINEERING_NOUNS) and any(
        verb in normalized for verb in _ENGINEERING_VERBS
    )


def _control_reply(turn: UserTurn, text: str) -> AssistantReply:
    return AssistantReply(
        channel=turn.channel,
        conversation_id=turn.conversation_id,
        text=text,
    )


def _remember_control_exchange(
    engine: ConversationEngine,
    turn: UserTurn,
    reply: AssistantReply,
) -> None:
    """Keep deterministic action-control turns in ordinary conversation history."""

    try:
        engine.memory.remember_event(
            USER_EVENT_TYPE,
            turn.text,
            context={
                "channel": turn.channel,
                "conversation_id": turn.conversation_id,
                "role": "user",
            },
            importance=1.0,
        )
        engine.memory.remember_event(
            ASSISTANT_EVENT_TYPE,
            reply.text,
            context={
                "channel": turn.channel,
                "conversation_id": turn.conversation_id,
                "role": "assistant",
            },
            importance=1.0,
        )
    except Exception as exc:
        logger.warning(
            "Hikari Conversation Forge control-memory write degraded: %s",
            type(exc).__name__,
        )


class ConversationForgeBridge:
    """Explicit chat -> proposal -> confirmation -> one bounded Forge dispatch."""

    def __init__(
        self,
        planner: ModelActionPlanner,
        policy: ActionAuthorizationPolicy,
        executor: ActionExecutor,
        pending_store: PendingForgeActionStore,
        *,
        confirmation_ttl_seconds: float = DEFAULT_CONFIRMATION_TTL_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        ttl = float(confirmation_ttl_seconds)
        if not 60 <= ttl <= 3600:
            raise ValueError("Forge confirmation TTL must be between 60 and 3600 seconds")
        self.planner = planner
        self.policy = policy
        self.executor = executor
        self.pending_store = pending_store
        self.confirmation_ttl_seconds = ttl
        self.clock = clock

    def _plan_with_timeout_retry(
        self,
        event: Event,
        decision: AttentionDecision,
    ) -> ActionProposal | None:
        """Retry one transient planner timeout before failing closed.

        Planning has no side effect, so one bounded retry is safe. Execution is
        still impossible until a durable proposal exists and the user confirms it.
        """

        try:
            return self.planner.plan(event, decision)
        except TimeoutError:
            logger.warning("Hikari Conversation Forge planning timed out; retrying once")
            return self.planner.plan(event, decision)

    def respond(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        *,
        source_ref: str | None = None,
    ) -> AssistantReply:
        if not isinstance(engine, ConversationEngine):
            raise TypeError("ConversationForgeBridge requires ConversationEngine")
        if not isinstance(turn, UserTurn):
            raise TypeError("ConversationForgeBridge respond requires UserTurn")

        control = _normalized_control_text(turn.text)
        try:
            pending = self.pending_store.get(turn.channel, turn.conversation_id)
        except Exception as exc:
            logger.warning(
                "Hikari Conversation Forge pending-state read degraded: %s",
                type(exc).__name__,
            )
            pending = None

        if pending is not None and pending.expired(
            self.clock(), self.confirmation_ttl_seconds
        ):
            try:
                self.pending_store.delete(turn.channel, turn.conversation_id)
            except Exception as exc:
                logger.warning(
                    "Hikari Conversation Forge pending-state expiry cleanup degraded: %s",
                    type(exc).__name__,
                )
            pending = None
            if control in _CONFIRM_WORDS or control in _CANCEL_WORDS:
                reply = _control_reply(
                    turn,
                    "刚才那项 Forge 操作已经过期了，没有执行。需要的话重新提出一次。",
                )
                _remember_control_exchange(engine, turn, reply)
                return reply

        if pending is not None:
            if control in _CANCEL_WORDS:
                try:
                    self.pending_store.delete(turn.channel, turn.conversation_id)
                except Exception as exc:
                    logger.warning(
                        "Hikari Conversation Forge cancellation state write failed: %s",
                        type(exc).__name__,
                    )
                    reply = _control_reply(
                        turn,
                        "我暂时不能安全地清掉这项待确认操作，所以不会执行它。",
                    )
                    _remember_control_exchange(engine, turn, reply)
                    return reply
                reply = _control_reply(turn, "取消了。这个 Forge 任务没有执行。")
                _remember_control_exchange(engine, turn, reply)
                return reply

            if control in _CONFIRM_WORDS:
                # Clear the durable grant before the side effect. If the process
                # crashes after Forge starts but before the reply is receipted,
                # a retried confirmation still cannot execute the same proposal twice.
                try:
                    self.pending_store.delete(turn.channel, turn.conversation_id)
                except Exception as exc:
                    logger.warning(
                        "Hikari Conversation Forge confirmation state write failed: %s",
                        type(exc).__name__,
                    )
                    reply = _control_reply(
                        turn,
                        "我现在不能安全地消费这次确认，所以没有启动 Forge。",
                    )
                    _remember_control_exchange(engine, turn, reply)
                    return reply

                authorization = self.policy.confirm(pending.proposal, approved=True)
                if (
                    authorization.decision is not AuthorizationDecision.AUTHORIZE
                    or authorization.authorized_action is None
                ):
                    reply = _control_reply(
                        turn,
                        f"这项操作没有通过授权边界：{authorization.reason}",
                    )
                    _remember_control_exchange(engine, turn, reply)
                    return reply

                try:
                    result = self.executor.execute(authorization.authorized_action)
                except Exception as exc:
                    logger.warning(
                        "Hikari Conversation Forge execution failed: %s",
                        type(exc).__name__,
                    )
                    reply = _control_reply(
                        turn,
                        "Forge 没能完成这次执行。确认已经消费，不会自动重试；需要的话重新发起。",
                    )
                    _remember_control_exchange(engine, turn, reply)
                    return reply

                if result.success:
                    text = (
                        f"Forge 已执行完成：{result.summary}。"
                        "这条通道没有自动合并、发布或部署权限。"
                    )
                else:
                    text = (
                        f"Forge 执行结束但没有通过：{result.summary}。"
                        "我不会自动重试或合并。"
                    )
                reply = _control_reply(turn, text)
                _remember_control_exchange(engine, turn, reply)
                return reply

            reply = _control_reply(
                turn,
                "我还在等刚才那项 Forge 操作的明确确认。回复“确认”开始，或回复“取消”放弃。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        if not _looks_like_engineering_intent(turn.text):
            return engine.respond(turn, source_ref=source_ref)

        event = Event(
            event_type="conversation.action_intent",
            source=f"conversation:{turn.channel}",
            content=turn.text,
            context={
                "channel": turn.channel,
                "conversation_id": turn.conversation_id,
                "source_ref": source_ref,
                "explicit_user_intent": True,
            },
        )
        decision = AttentionDecision(
            should_intervene=True,
            importance=1.0,
            reason="explicit direct-conversation engineering intent",
        )
        try:
            proposal = self._plan_with_timeout_retry(event, decision)
        except Exception as exc:
            logger.warning(
                "Hikari Conversation Forge planning degraded: %s",
                type(exc).__name__,
            )
            reply = _control_reply(
                turn,
                "Forge 规划没有完成，所以我没有启动 Forge，也没有执行任何改动。请稍后重新发起。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        if proposal is None:
            reply = _control_reply(
                turn,
                "这次没有生成可执行的 Forge 提案，所以我没有启动 Forge，也没有执行任何改动。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        authorization = self.policy.authorize(proposal)
        if authorization.decision is AuthorizationDecision.DENY:
            reply = _control_reply(
                turn,
                f"这项 Forge 请求没有通过授权边界：{authorization.reason}。没有执行任何改动。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply
        if authorization.decision is AuthorizationDecision.AUTHORIZE:
            # Direct chat must never turn a newly planned Forge task into an
            # implicit side effect, even if a future spec is accidentally loosened.
            logger.error("Hikari Conversation Forge proposal unexpectedly auto-authorized")
            reply = _control_reply(
                turn,
                "Forge 提案出现了不符合直接对话确认边界的授权状态，所以我已阻止执行。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        pending = PendingForgeAction(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            proposal=proposal,
            created_at=self.clock(),
        )
        try:
            self.pending_store.put(pending)
        except Exception as exc:
            logger.warning(
                "Hikari Conversation Forge pending-state write failed: %s",
                type(exc).__name__,
            )
            reply = _control_reply(
                turn,
                "我无法安全保存这项 Forge 待确认操作，所以没有启动 Forge，也没有执行任何改动。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        arguments = proposal.arguments
        goal = str(arguments.get("goal", "")).strip()
        project_id = str(arguments.get("project_id", "")).strip()
        reply = _control_reply(
            turn,
            "我可以把这件事交给 Forge，但要先经过你确认。\n"
            f"项目：{project_id}\n"
            f"目标：{goal}\n"
            f"影响：{proposal.effect}\n"
            "Forge 只会在受控工程工作区里执行并验证；这条通道没有自动合并、发布或部署权限。\n"
            "回复“确认”开始，或回复“取消”放弃。",
        )
        _remember_control_exchange(engine, turn, reply)
        return reply


def _runtime_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    *,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name, "").strip()
    try:
        value = default if not raw else int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _runtime_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(name, "").strip()
    try:
        value = default if not raw else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def build_conversation_forge_bridge(
    values: Mapping[str, str],
    provider: ChatProvider,
    *,
    repository: str | Path,
    state_dir: str | Path,
) -> ConversationForgeBridge:
    """Build the first trusted Hikari self-engineering Forge profile."""

    repository_value = values.get("HIKARI_FORGE_REPOSITORY", "").strip()
    trusted_repository = (
        Path(repository_value).expanduser().resolve()
        if repository_value
        else Path(repository).expanduser().resolve()
    )
    project_id = values.get("HIKARI_FORGE_PROJECT_ID", DEFAULT_FORGE_PROJECT_ID).strip()
    executable = values.get("HIKARI_FORGE_EXECUTABLE", DEFAULT_FORGE_EXECUTABLE).strip()
    backend = values.get("HIKARI_FORGE_BACKEND", DEFAULT_FORGE_BACKEND).strip()
    permission_mode = values.get(
        "HIKARI_FORGE_CLAUDE_PERMISSION_MODE",
        DEFAULT_FORGE_CLAUDE_PERMISSION_MODE,
    ).strip()
    max_attempts = _runtime_int(
        values,
        "HIKARI_FORGE_MAX_ATTEMPTS",
        DEFAULT_FORGE_MAX_ATTEMPTS,
        minimum=1,
        maximum=10,
    )
    max_turns = _runtime_int(
        values,
        "HIKARI_FORGE_CLAUDE_MAX_TURNS",
        DEFAULT_FORGE_CLAUDE_MAX_TURNS,
        minimum=1,
        maximum=100,
    )
    ttl = _runtime_float(
        values,
        "HIKARI_FORGE_CONFIRMATION_TTL_SECONDS",
        DEFAULT_CONFIRMATION_TTL_SECONDS,
        minimum=60.0,
        maximum=3600.0,
    )

    profile = ForgeProjectProfile(
        project_id=project_id,
        repository=trusted_repository,
        verification=DEFAULT_FORGE_VERIFICATION,
        executable=executable,
        backend=backend,
        max_attempts=max_attempts,
        claude_permission_mode=permission_mode,
        claude_max_turns=max_turns,
    )
    registry = ForgeProjectRegistry([profile])
    catalog = ActionCatalog([forge_task_action_spec()])
    planner = ModelActionPlanner(provider, catalog)
    policy = ActionAuthorizationPolicy()
    executor = ActionExecutor([ForgeTaskAdapter(registry)])
    pending_store = PendingForgeActionStore(
        Path(state_dir).expanduser().resolve() / "conversation_forge_pending.json"
    )
    return ConversationForgeBridge(
        planner,
        policy,
        executor,
        pending_store,
        confirmation_ttl_seconds=ttl,
    )