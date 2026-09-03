from __future__ import annotations

from dataclasses import dataclass
import re


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
<reply>真正发给对方的话</reply>
"""


@dataclass(frozen=True)
class WhiteboardOutput:
    reaction: str
    reply: str


_REACTION_RE = re.compile(r"<reaction>(.*?)(?:</reaction>|<reply>|$)", re.IGNORECASE | re.DOTALL)
_REPLY_RE = re.compile(r"<reply>(.*?)(?:</reply>|$)", re.IGNORECASE | re.DOTALL)


def parse_whiteboard_output(raw: str) -> WhiteboardOutput:
    """Extract the private reaction and user-facing reply from Whiteboard output.

    The reaction is intentionally ephemeral. Callers may inspect it for diagnostics,
    but only ``reply`` is allowed to enter normal conversation persistence or delivery.
    A plain-text model response is accepted as a compatibility fallback so a formatting
    miss cannot expose an empty reply to the user.
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
        reply = re.sub(r"</?(?:reaction|reply)>", "", reply, flags=re.IGNORECASE).strip()

    if not reply:
        raise ValueError("whiteboard model output did not contain a usable reply")

    return WhiteboardOutput(reaction=reaction, reply=reply)
