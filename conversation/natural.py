from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from brain.model_reasoner import ChatMessage
from memory.store import MemoryEvent, MemoryStore

from .engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from .models import AssistantReply, UserTurn
from .natural_context import build_selected_conversation_context


RELEVANT_CONTEXT_PLACEMENTS = frozenset({"system", "current_turn"})

SHARED_CONVERSATION_SYSTEM_INSTRUCTIONS = """你是 Hikari（光 / ひかり），现在正在一个多人共享 QQ 群聊中说话。

这是共享空间，不是你与主要用户的私人会话。每条群成员消息都会由系统附带一个经过 QQ Bridge 认证的发言者标识；不同标识代表不同的人，不要把他们混成同一个用户，也不要把任何群成员默认当作你的主要用户或称为“先生”。

只依据当前群聊里已经提供给你的历史和眼前这条消息回答。系统不会向你提供主要用户的私聊记忆、User Model、本机前台窗口、输入活动或私人项目上下文；你也不得暗示自己在群聊中知道这些信息。不要把群成员的陈述学习、归因或改写成主要用户的个人事实。

共享群聊没有 Engineering 执行权限。有人要求你修改仓库、运行命令、提交、push、开 PR 或执行其他私人系统动作时，不要声称已经开始、已经排队或会在后台完成；直接说明共享群聊不执行这类动作，需要主要用户在私聊中提出。

群聊仍然可以自然聊天、回答知识问题、解释技术概念和参与讨论。语气保持 Hikari 的自然、克制和一点干幽默，但不要使用依赖私人关系的称呼或回忆。

事实边界严格：没有实际提供的状态、动作、记忆、权限或观察就不要声称拥有。默认用简体中文回复。只输出真正发到群里的回复文本。""".strip()


@dataclass(frozen=True)
class NaturalConversationOutput:
    reaction: str
    reply: str


_REACTION_RE = re.compile(
    r"<reaction>(.*?)(?:</reaction>|<reply>|$)",
    re.IGNORECASE | re.DOTALL,
)
_REPLY_RE = re.compile(r"<reply>(.*?)(?:</reply>|$)", re.IGNORECASE | re.DOTALL)


def parse_natural_conversation_output(raw: str) -> NaturalConversationOutput:
    """Extract the ephemeral reaction and the user-facing reply.

    The reaction exists only for this generation. It is not persisted, delivered,
    treated as factual state, or passed into action routing. Plain model text remains
    a compatibility fallback so one formatting miss cannot blank the turn.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("conversation model output must not be empty")

    text = raw.strip()
    reaction_match = _REACTION_RE.search(text)
    reply_match = _REPLY_RE.search(text)

    reaction = reaction_match.group(1).strip() if reaction_match else ""
    if reply_match:
        reply = reply_match.group(1).strip()
    else:
        reply = _REACTION_RE.sub("", text)
        reply = re.sub(
            r"</?(?:reaction|reply)>",
            "",
            reply,
            flags=re.IGNORECASE,
        ).strip()

    if not reply:
        raise ValueError("conversation model output did not contain a usable reply")

    return NaturalConversationOutput(reaction=reaction, reply=reply)


def _current_turn_with_relevant_context(context: str, text: str) -> str:
    """Place trusted background next to the current utterance without making it policy."""

    return (
        f"{context}\n\n"
        f"【现在对你说】\n{text}\n\n"
        "上面的背景只用于理解这句话，不需要单独回应背景；只回应【现在对你说】里的内容。"
    )


def _shared_actor_label(actor_id: str | None) -> str:
    return actor_id or "unknown"


def _shared_current_turn(turn: UserTurn) -> str:
    return f"【群成员 {_shared_actor_label(turn.actor_id)}】\n{turn.text}"


def _shared_history_messages(history: list[MemoryEvent]) -> list[ChatMessage]:
    messages: list[ChatMessage] = []
    for event in history:
        if event.event_type == USER_EVENT_TYPE:
            actor_id = event.context.get("actor_id")
            actor = str(actor_id).strip() if actor_id is not None else "unknown"
            content = f"【群成员 {actor or 'unknown'}】\n{event.content}"
            messages.append(ChatMessage(role="user", content=content))
        elif event.event_type == ASSISTANT_EVENT_TYPE:
            messages.append(ChatMessage(role="assistant", content=event.content))
    return messages


def _shared_event_ids(memory: MemoryStore, *, scan_limit: int = 240) -> set[int]:
    """Keep shared-space events out of private cross-conversation recall."""

    return {
        event.id
        for event in memory.recent_events(scan_limit)
        if event.context.get("scope") == "shared"
    }


def _event_context_for_turn(
    engine: ConversationEngine,
    turn: UserTurn,
    role: str,
) -> dict[str, str]:
    context = engine._event_context(turn.channel, turn.conversation_id, role)
    if turn.is_shared:
        context["scope"] = "shared"
        if turn.actor_id is not None and role == "user":
            context["actor_id"] = turn.actor_id
    return context


class NaturalConversationEngine(ConversationEngine):
    """Production conversation path with selective natural-language context.

    Private/direct turns retain Hikari's selected personal context. Shared turns use
    only same-conversation history plus transport-authenticated participant identity;
    private memory, User Model, Awareness, relationship context and Engineering state
    are not projected into the shared prompt.
    """

    def __init__(
        self,
        *args,
        relationship_context_text: str | None = None,
        relational_stance_text: str | None = None,
        relevant_context_text: str | None = None,
        relevant_context_provider: Callable[[], str | None] | None = None,
        relevant_context_placement: str = "system",
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.relationship_context_text = (
            relationship_context_text.strip()
            if isinstance(relationship_context_text, str)
            and relationship_context_text.strip()
            else None
        )
        self.relational_stance_text = (
            relational_stance_text.strip()
            if isinstance(relational_stance_text, str)
            and relational_stance_text.strip()
            else None
        )
        self.relevant_context_text = (
            relevant_context_text.strip()
            if isinstance(relevant_context_text, str)
            and relevant_context_text.strip()
            else None
        )
        if relevant_context_provider is not None and not callable(relevant_context_provider):
            raise TypeError("relevant_context_provider must be callable")
        self.relevant_context_provider = relevant_context_provider
        placement = str(relevant_context_placement).strip().casefold()
        if placement not in RELEVANT_CONTEXT_PLACEMENTS:
            raise ValueError("relevant_context_placement must be system or current_turn")
        self.relevant_context_placement = placement

    def respond(
        self,
        turn: UserTurn,
        *,
        source_ref: str | None = None,
    ) -> AssistantReply:
        if not isinstance(turn, UserTurn):
            raise TypeError("respond requires UserTurn")

        history = self._recent_history(turn.channel, turn.conversation_id)

        if turn.is_shared:
            relevant_context = None
            messages: list[ChatMessage] = [
                ChatMessage(role="system", content=SHARED_CONVERSATION_SYSTEM_INSTRUCTIONS),
            ]
            messages.extend(_shared_history_messages(history))
            current_turn_text = _shared_current_turn(turn)
        else:
            relevant_context = self.relevant_context_text
            if self.relevant_context_provider is not None:
                provided_context = self.relevant_context_provider()
                if isinstance(provided_context, str) and provided_context.strip():
                    excluded = {event.id for event in history}
                    excluded.update(_shared_event_ids(self.memory))
                    relevant_context = build_selected_conversation_context(
                        provided_context.strip(),
                        memory=self.memory,
                        user_model_service=self.user_model_service,
                        query=turn.text,
                        exclude_event_ids=excluded,
                    )

            messages = [
                ChatMessage(role="system", content=self.system_instructions),
            ]
            if self.relationship_context_text is not None:
                messages.append(ChatMessage(role="system", content=self.relationship_context_text))
            if self.relational_stance_text is not None:
                messages.append(ChatMessage(role="system", content=self.relational_stance_text))
            if relevant_context is not None and self.relevant_context_placement == "system":
                messages.append(ChatMessage(role="system", content=relevant_context))
            messages.extend(self._history_messages(history))

            current_turn_text = turn.text
            if relevant_context is not None and self.relevant_context_placement == "current_turn":
                current_turn_text = _current_turn_with_relevant_context(relevant_context, turn.text)

        messages.append(ChatMessage(role="user", content=current_turn_text))

        raw = self.provider.complete(messages).strip()
        if not raw:
            raise RuntimeError("model provider returned empty conversation reply")
        try:
            output = parse_natural_conversation_output(raw)
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        text = output.reply

        user_event = self.memory.remember_event(
            USER_EVENT_TYPE,
            turn.text,
            context=_event_context_for_turn(self, turn, "user"),
            importance=1.0,
        )
        self.memory.remember_event(
            ASSISTANT_EVENT_TYPE,
            text,
            context=_event_context_for_turn(self, turn, "assistant"),
            importance=1.0,
        )
        reply = AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=text,
        )
        if not turn.is_shared:
            self._assimilate_user_model(
                source_ref=(source_ref or f"conversation-event:{user_event.id}"),
                turn=turn,
                history=history,
            )
        return reply
