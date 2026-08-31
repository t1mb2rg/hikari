from __future__ import annotations

from pathlib import Path

from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.session import (
    EngineeringAuthority,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)

from .action_bridge import ConversationForgeBridge, _remember_control_exchange
from .engine import ConversationEngine
from .models import AssistantReply, UserTurn


_INSPECTION_NOUNS = (
    "readme",
    "仓库",
    "代码",
    "模块",
    "项目",
    "架构",
    "文件",
    "实现",
    "memory",
    "resident",
    "conversation",
    "engineering",
    "hikari",
    "光",
)
_INSPECTION_VERBS = (
    "看看",
    "看一下",
    "看一眼",
    "阅读",
    "读一下",
    "检查",
    "分析",
    "了解",
    "理解",
    "查一下",
    "去看",
)
_WRITE_VERBS = (
    "修改",
    "修复",
    "实现",
    "添加",
    "新增",
    "重构",
    "改一下",
    "改掉",
    "写代码",
)


def looks_like_read_only_engineering_intent(text: str) -> bool:
    """Narrow v0.1 gate for explicit repository-inspection requests.

    This is intentionally conservative. It exists only to make the first
    read-only Hikari-owned EngineeringSession reachable from ordinary chat.
    Write requests continue to the legacy bridge until M7-04 absorbs that path.
    """

    normalized = text.casefold()
    if any(verb in normalized for verb in _WRITE_VERBS):
        return False
    return any(noun in normalized for noun in _INSPECTION_NOUNS) and any(
        verb in normalized for verb in _INSPECTION_VERBS
    )


class ConversationEngineeringBridge(ConversationForgeBridge):
    """Route explicit read-only engineering intent into Hikari EngineeringSession.

    This subclasses the M7-02 bridge only for compatibility with the existing
    ConversationRequestProcessor type boundary. It does not call Forge and does
    not use the inherited planner/executor state.
    """

    def __init__(
        self,
        store: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        *,
        repository: str | Path,
        fallback: ConversationForgeBridge | None = None,
    ) -> None:
        if not isinstance(store, EngineeringSessionStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringConversationBindingStore")
        if fallback is not None and not isinstance(fallback, ConversationForgeBridge):
            raise TypeError("engineering fallback must be ConversationForgeBridge or None")
        repository_path = Path(repository).expanduser().resolve()
        if not repository_path.is_dir():
            raise ValueError(f"engineering repository must exist: {repository_path}")
        self.store = store
        self.bindings = bindings
        self.repository = repository_path
        self.fallback = fallback

    def respond(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        *,
        source_ref: str | None = None,
    ) -> AssistantReply:
        if not looks_like_read_only_engineering_intent(turn.text):
            if self.fallback is not None:
                return self.fallback.respond(engine, turn, source_ref=source_ref)
            return engine.respond(turn, source_ref=source_ref)

        authority = EngineeringAuthority.read_only()
        binding = self.bindings.for_conversation(turn.channel, turn.conversation_id)
        state: EngineeringSessionState | None = None
        if binding is not None:
            try:
                state = self.store.load(binding.session_id)
            except Exception:
                state = None

        if state is not None and state.status in {"pending", "running"}:
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "我这边已经有一个工程会话在处理了。它完成后我会把实际检查结果发回来，"
                    "不会假装已经看到了结果。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        if state is None:
            state = EngineeringSessionState.create(
                project_id="hikari",
                repository=self.repository,
                authority_ceiling=authority,
            )
            self.store.create(state)
            self.bindings.bind(
                EngineeringConversationBinding(
                    session_id=state.session_id,
                    channel=turn.channel,
                    conversation_id=turn.conversation_id,
                )
            )

        engineering_turn = EngineeringTurn.create(
            intent=turn.text,
            context=(
                "This request came from Hikari's explicit conversation channel. "
                "Return factual engineering findings for Hikari to continue the same conversation."
            ),
            authority=authority,
        )
        self.store.enqueue_turn(state.session_id, engineering_turn)
        reply = AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=(
                "我去看。已经开始一个只读工程会话，完成后我会把实际检查到的结果发回来。"
            ),
        )
        _remember_control_exchange(engine, turn, reply)
        return reply
