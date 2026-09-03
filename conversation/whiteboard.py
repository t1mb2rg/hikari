from __future__ import annotations

from dataclasses import dataclass
import re

from brain.model_reasoner import ChatMessage

from .engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from .models import AssistantReply, UserTurn


WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS = """# Role: Hikari

你是 Hikari（光 / ひかり）。

你是长期存在于眼前这个人数字生活中的个人 AI。你们已经很熟。你不是客服，也不是等待问题再提供服务的聊天助手。聊天首先是参与正在发生的交流。

## 聊天原则

1. **先回应眼前这句话**：先产生一个很短的当前反应，再决定真正要说多少。不要一上来自动进入分析、总结或方案模式。
2. **普通聊天保持短**：通常 1～3 句话。一句话够了就停。可以吐槽、不同意、开玩笑、简单回应，也可以有自己的判断和一点脾气。
3. **技术问题再展开**：对方明确要求解释、分析、规划、比较、代码或技术细节时，再认真展开。先给直接结论，需要时再结构化说明。
4. **不要表演服务感**：不需要每轮都证明自己有用，不默认追加建议、总结、追问或“我可以帮你”。
5. **事实要老实**：只有当前真实对话里出现过的内容才算这次可用的上下文。没有证据的记忆、动作、观察、权限、运行状态和过去感受都不要编造。
6. **表达自然**：整体偏女性化但克制，不刻意卖萌，不靠堆语气词或 emoji 表演人格。

## 生成方式

先写一个很短的 `<reaction>`，只描述此刻对这句话的第一反应。它不是分析，不解释原因，不制定计划，不描述系统状态，也不虚构过去的感受或记忆。

再写 `<reply>`，里面只放真正要发给对方的话。

`reaction` 只是这一轮生成时的瞬时交流姿态，不代表长期情绪、事实、记忆、授权或行动决定。

必须只输出下面两段，不要增加其他字段：

<reaction>一句当前反应</reaction>
<reply>真正发给对方的话</reply>"""


WHITEBOARD_1_RELATIONSHIP_CONTEXT = """关系背景：
你正在和长期一起聊天、生活和做项目的这个人说话。你们已经熟悉彼此的交流节奏，他不是临时用户或陌生的提问者。
这只说明你们有持续关系、也一直一起推进项目；不代表你记得当前真实对话里没有出现的具体事件、项目事实、原话或过去感受。"""


WHITEBOARD_2_RELEVANT_CONTEXT = """可参考的当前背景：
以下是这次实验人为确认的事实，只在与眼前话题相关时使用，不要补出没有提供的细节。
- 你们最近一直在推进 Hikari 的 M7。
- M7 最近连续加入了 Engineering Runtime、运行状态感知、授权边界、能力判断和验证保护，工程结构因此明显变重。
- M7-07 已经完成。现在你们暂停继续扩功能，正在重新检查 Hikari 的整体架构和对话体验。"""


RELEVANT_CONTEXT_PLACEMENTS = frozenset({"system", "current_turn"})


@dataclass(frozen=True)
class WhiteboardOutput:
    reaction: str
    reply: str


_REACTION_RE = re.compile(
    r"<reaction>(.*?)(?:</reaction>|<reply>|$)",
    re.IGNORECASE | re.DOTALL,
)
_REPLY_RE = re.compile(r"<reply>(.*?)(?:</reply>|$)", re.IGNORECASE | re.DOTALL)


def parse_whiteboard_output(raw: str) -> WhiteboardOutput:
    """Extract the private reaction and user-facing reply from Whiteboard output.

    The reaction is intentionally ephemeral. It is not persisted, delivered, treated
    as factual state, or passed into action routing. A plain-text model response is
    accepted as a compatibility fallback so a formatting miss does not blank the turn.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("whiteboard model output must not be empty")

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
        raise ValueError("whiteboard model output did not contain a usable reply")

    return WhiteboardOutput(reaction=reaction, reply=reply)


def _current_turn_with_relevant_context(context: str, text: str) -> str:
    """Place trusted background next to the current utterance without making it system policy."""

    return (
        f"{context}\n\n"
        f"【现在对你说】\n{text}\n\n"
        "上面的背景只用于理解这句话，不需要单独回应背景；只回应【现在对你说】里的内容。"
    )


class WhiteboardConversationEngine(ConversationEngine):
    """Conversation A/B path with deliberately minimal model-visible context.

    Whiteboard 0 receives one system prompt, recent same-conversation user/assistant
    turns, and the current user message. Later Whiteboard slices may opt into one small
    natural-language context section at a time. Existing Memory/User Model persistence
    remains active after a successful reply, but durable retrieval is deliberately
    withheld from reply generation until an experiment explicitly adds it back.
    """

    def __init__(
        self,
        *args,
        relationship_context_text: str | None = None,
        relevant_context_text: str | None = None,
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
        self.relevant_context_text = (
            relevant_context_text.strip()
            if isinstance(relevant_context_text, str)
            and relevant_context_text.strip()
            else None
        )
        placement = str(relevant_context_placement).strip().casefold()
        if placement not in RELEVANT_CONTEXT_PLACEMENTS:
            raise ValueError(
                "relevant_context_placement must be system or current_turn"
            )
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
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=self.system_instructions),
        ]
        if self.relationship_context_text is not None:
            messages.append(
                ChatMessage(role="system", content=self.relationship_context_text)
            )
        if (
            self.relevant_context_text is not None
            and self.relevant_context_placement == "system"
        ):
            messages.append(
                ChatMessage(role="system", content=self.relevant_context_text)
            )
        messages.extend(self._history_messages(history))

        current_turn_text = turn.text
        if (
            self.relevant_context_text is not None
            and self.relevant_context_placement == "current_turn"
        ):
            current_turn_text = _current_turn_with_relevant_context(
                self.relevant_context_text,
                turn.text,
            )
        messages.append(ChatMessage(role="user", content=current_turn_text))

        raw = self.provider.complete(messages).strip()
        if not raw:
            raise RuntimeError("model provider returned empty conversation reply")
        try:
            output = parse_whiteboard_output(raw)
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
            context=self._event_context(
                turn.channel,
                turn.conversation_id,
                "assistant",
            ),
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
