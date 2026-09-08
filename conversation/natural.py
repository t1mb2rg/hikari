from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re

from brain.model_reasoner import ChatMessage

from .engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from .models import AssistantReply, UserTurn
from .natural_context import build_selected_conversation_context


RELEVANT_CONTEXT_PLACEMENTS = frozenset({"system", "current_turn"})


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


class NaturalConversationEngine(ConversationEngine):
    """Production conversation path with selective natural-language context.

    The model sees the persona/system instructions, recent same-conversation turns,
    one small selected natural context when available, and the current user message.
    Durable memory, User Model, runtime facts, and Awareness remain behind the same
    selection boundary rather than being serialized wholesale into every prompt.
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
        relevant_context = self.relevant_context_text
        if self.relevant_context_provider is not None:
            provided_context = self.relevant_context_provider()
            if isinstance(provided_context, str) and provided_context.strip():
                relevant_context = build_selected_conversation_context(
                    provided_context.strip(),
                    memory=self.memory,
                    user_model_service=self.user_model_service,
                    query=turn.text,
                    exclude_event_ids={event.id for event in history},
                )

        messages: list[ChatMessage] = [
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
            context=self._event_context(turn.channel, turn.conversation_id, "user"),
            importance=1.0,
        )
        self.memory.remember_event(
            ASSISTANT_EVENT_TYPE,
            text,
            context=self._event_context(turn.channel, turn.conversation_id, "assistant"),
            importance=1.0,
        )
        reply = AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=text,
        )
        self._assimilate_user_model(
            source_ref=(source_ref or f"conversation-event:{user_event.id}"),
            turn=turn,
            history=history,
        )
        return reply
