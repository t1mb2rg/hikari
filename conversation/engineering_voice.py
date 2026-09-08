from __future__ import annotations

from dataclasses import dataclass
import logging

from brain.model_reasoner import ChatMessage

from .engine import ConversationEngine
from .natural import NaturalConversationEngine, parse_natural_conversation_output


logger = logging.getLogger(__name__)


_ENGINEERING_VOICE_KINDS = frozenset(
    {
        "accepted",
        "completed",
        "failed",
        "blocked",
        "capability_gap",
        "escalation",
    }
)

_ENGINEERING_VOICE_INSTRUCTIONS = """You are turning trusted Hikari engineering facts into one natural user-facing Jarvis reply.

The facts below are machine truth, not a user message. Use only those facts. Do not invent work, validation, files, commits, branches, permissions, causes, or outcomes that are not supplied.

Presentation belongs to Jarvis:
- Speak naturally as the same assistant already in the conversation.
- Internal execution machinery is part of Hikari, not another actor. Never describe an accepted task as being handed off, delegated, routed, or transferred to another runtime, worker, session, or subsystem.
- For an accepted task, take first-person ownership. Acknowledge the goal and that work has started, but do not narrate worktrees, internal sessions, control-plane plumbing, or project-maintenance authority unless the user explicitly asked about that implementation detail.
- Do not announce internal control-plane fields or mechanically recite status names/capability identifiers unless the identifier itself is genuinely useful to the user.
- Do not say something is completed when the event is only accepted.
- For a completed task, lead with the actual outcome and include concrete evidence only when useful.
- For a failed or blocked task, say clearly what did not complete and why, without pretending it succeeded.
- For a capability gap, distinguish missing implementation from missing per-action permission.
- For an escalation, make clear that the requested effect crosses the standing project mandate and needs a human decision.
- Usually keep this to 1-3 conversational sentences. This is a handoff between familiar collaborators, not a status report.

Return only the final user-facing reply text.""".strip()


@dataclass(frozen=True)
class EngineeringVoiceFacts:
    """Trusted engineering facts crossing from control/runtime ownership into Jarvis voice."""

    kind: str
    goal: str
    status: str | None = None
    summary: str | None = None
    changed_files: tuple[str, ...] = ()
    branch: str | None = None
    capabilities: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    historical: bool = False

    def __post_init__(self) -> None:
        kind = self.kind.strip().casefold()
        goal = self.goal.strip()
        if kind not in _ENGINEERING_VOICE_KINDS:
            raise ValueError(f"unsupported engineering voice kind: {self.kind!r}")
        if not goal:
            raise ValueError("engineering voice goal must not be empty")
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "goal", goal)
        object.__setattr__(self, "changed_files", tuple(self.changed_files))
        object.__setattr__(self, "capabilities", tuple(self.capabilities))
        object.__setattr__(self, "details", tuple(self.details))

    def prompt(self) -> str:
        lines = [
            "Trusted engineering facts:",
            f"event: {self.kind}",
            f"goal: {self.goal}",
        ]

        # Acceptance is intentionally projected through a narrow voice boundary.
        # Runtime/session/worktree facts may be useful to the control plane, but they
        # are not part of the user-facing ownership story. Hikari accepted the work;
        # the implementation machinery remains internal unless the user asks for it.
        if self.kind == "accepted":
            lines.append(
                "accepted_scope: Hikari has accepted this goal and started work; no terminal outcome exists yet."
            )
            return "\n".join(lines)

        if self.status:
            lines.append(f"status: {self.status}")
        if self.summary:
            lines.append(f"runtime_summary: {self.summary}")
        if self.changed_files:
            lines.append("changed_files: " + ", ".join(self.changed_files))
        if self.branch:
            lines.append(f"branch: {self.branch}")
        if self.capabilities:
            lines.append("capabilities: " + ", ".join(self.capabilities))
        if self.details:
            lines.append("details:")
            lines.extend(f"- {item}" for item in self.details)
        lines.append(f"historical_delivery: {'true' if self.historical else 'false'}")
        return "\n".join(lines)

    def fallback_text(self) -> str:
        """User-facing degraded voice used if production Jarvis rendering is unavailable."""

        if self.kind == "accepted":
            return "我来处理。已经开始了，完成后我把实际结果发回来。"
        if self.kind == "completed":
            prefix = "刚补到一条旧任务结果：" if self.historical else "搞定了。"
            return f"{prefix}{self.summary or self.goal}"
        if self.kind == "failed":
            prefix = "刚补到一条旧任务失败结果：" if self.historical else "这次没做完。"
            return f"{prefix}{self.summary or self.goal}"
        if self.kind == "blocked":
            prefix = "刚补到一条旧任务阻塞结果：" if self.historical else "这一步被当前边界挡住了。"
            return f"{prefix}{self.summary or self.goal}"
        if self.kind == "capability_gap":
            missing = ", ".join(self.capabilities)
            if missing:
                return f"这一步我现在还做不了，缺的是 {missing}。这是能力缺口，不是需要你逐个动作授权。"
            return "这一步我现在还缺实际执行能力，不会假装已经能做。"
        if self.kind == "escalation":
            boundary = ", ".join(self.capabilities)
            if boundary:
                return f"这一步触及当前项目 mandate 之外的影响边界，需要你决定是否扩展这次授权：{boundary}。"
            return "这一步触及当前项目 mandate 之外的影响边界，需要你决定是否扩展这次授权。"
        raise AssertionError(self.kind)

    def compatibility_text(self) -> str:
        """Deterministic legacy/base-engine text kept for routing and boundary tests.

        Production NaturalConversationEngine never uses this path. It exists while the
        pre-release ConversationEngine compatibility surface is still intentionally
        present and keeps those tests focused on deterministic authority decisions.
        """

        if self.kind == "accepted" and self.details:
            return f"我来处理。{self.details[0]}"
        return self.fallback_text()


class EngineeringVoiceRenderer:
    """Use Conversation's provider/persona to phrase trusted engineering facts.

    Rendering deliberately bypasses ConversationEngine.respond(): engineering facts are
    not fake user turns and must not trigger user-model assimilation. Synchronous control
    replies are persisted by the bridge after rendering; proactive terminal delivery
    remains owned by the delivery path.
    """

    def __init__(self, engine: ConversationEngine) -> None:
        if not isinstance(engine, ConversationEngine):
            raise TypeError("EngineeringVoiceRenderer requires ConversationEngine")
        self.engine = engine

    def render(
        self,
        facts: EngineeringVoiceFacts,
        *,
        channel: str,
        conversation_id: str,
    ) -> str:
        if not isinstance(facts, EngineeringVoiceFacts):
            raise TypeError("facts must be EngineeringVoiceFacts")

        # Plain ConversationEngine is used by deterministic routing/boundary tests and
        # legacy compatibility paths. Production Jarvis runs NaturalConversationEngine.
        # Keeping the base engine model-free makes it explicit that the model phrases an
        # already-decided boundary; it never participates in the authority decision.
        if not isinstance(self.engine, NaturalConversationEngine):
            return facts.compatibility_text()

        try:
            history = self.engine._recent_history(channel, conversation_id)
            messages = [
                ChatMessage(role="system", content=self.engine.system_instructions),
                ChatMessage(role="system", content=_ENGINEERING_VOICE_INSTRUCTIONS),
            ]
            messages.extend(self.engine._history_messages(history))
            messages.append(ChatMessage(role="user", content=facts.prompt()))
            raw = self.engine.provider.complete(messages)
            return parse_natural_conversation_output(raw).reply
        except Exception as exc:
            logger.warning(
                "Hikari Engineering voice rendering degraded: %s",
                type(exc).__name__,
            )
            return facts.fallback_text()
