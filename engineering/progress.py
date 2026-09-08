from __future__ import annotations

from dataclasses import dataclass

from .session import EngineeringSessionState


@dataclass(frozen=True, slots=True)
class EngineeringProgress:
    status: str
    phase: str
    updated_at: float

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "phase": self.phase,
            "updated_at": self.updated_at,
        }


def _phase_from_summary(summary: str) -> str | None:
    text = summary.strip()
    if not text:
        return None
    if "等待 Engineering Worker" in text:
        return "queued"
    if "准备工程工作区" in text or "工程工作区已准备" in text:
        return "preparing"
    if "只读理解项目" in text:
        return "inspecting"
    if "正在维护项目" in text:
        return "editing"
    if "正在运行项目测试" in text:
        return "testing"
    if "自动修复" in text:
        return "repairing"
    if "正在提交工程分支" in text:
        return "committing"
    return None


def describe_engineering_progress(state: EngineeringSessionState) -> EngineeringProgress:
    """Project durable EngineeringSession state into a stable progress phase.

    The phase is derived only from machine-written session state. Conversation
    never invents completion or progress from the natural-language task itself.
    """

    if state.status in {"completed", "failed", "blocked"}:
        phase = state.status
    elif state.status == "pending":
        phase = "queued"
    else:
        phase = _phase_from_summary(state.latest_summary) or "working"
    return EngineeringProgress(
        status=state.status,
        phase=phase,
        updated_at=state.updated_at,
    )
