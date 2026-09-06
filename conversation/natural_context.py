from __future__ import annotations

from pathlib import Path

from engineering.session import EngineeringSessionStore


_ACTIVE_ENGINEERING_STATUSES = {"pending", "running"}


def _current_project_context(repository: str | Path | None) -> str | None:
    root = Path(repository) if repository is not None else Path.cwd()
    path = root / "CURRENT.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def build_resident_natural_context(
    *,
    state_dir: str | Path,
    qq_enabled: bool,
    engineering_enabled: bool,
    repository: str | Path | None = None,
) -> str:
    """Turn a few current resident/project facts into compact model-visible context."""

    lines = [
        "当前可用的系统事实：",
        "- Hikari Resident 当前正在运行，这次对话由它的 Conversation Host 承载。",
    ]

    if qq_enabled:
        lines.append("- QQ Bridge 当前由 Resident 托管。")

    if engineering_enabled:
        states = EngineeringSessionStore(Path(state_dir) / "engineering").list_states()
        active = [
            state for state in states if state.status in _ACTIVE_ENGINEERING_STATUSES
        ]
        if active:
            current = max(active, key=lambda state: state.updated_at)
            fact = f"- 当前有一个 Engineering 任务处于 {current.status} 状态"
            if current.latest_summary.strip():
                fact += f"：{current.latest_summary.strip()}"
            lines.append(fact + "。")
        else:
            lines.append(
                "- Engineering Runtime 已启用，但当前没有 pending 或 running 的 Engineering 任务。"
            )
    else:
        lines.append("- Engineering Runtime 当前未启用。")

    project_context = _current_project_context(repository)
    if project_context:
        lines.extend(["", "当前项目上下文：", project_context])

    lines.append(
        "这些只是当前事实；只在与眼前问题相关时自然使用，不需要逐条复述。"
    )
    return "\n".join(lines)
