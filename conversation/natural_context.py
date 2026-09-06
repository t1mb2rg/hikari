from __future__ import annotations

from pathlib import Path
import re
import unicodedata

from engineering.session import EngineeringSessionStore
from memory.store import MemoryStore


_ACTIVE_ENGINEERING_STATUSES = {"pending", "running"}
_LATIN_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
_RECALL_STOPWORDS = {
    "我们",
    "你们",
    "他们",
    "现在",
    "之前",
    "当时",
    "这个",
    "那个",
    "觉得",
    "感觉",
    "怎么",
    "为什么",
    "什么",
    "还是",
    "已经",
}


def _current_project_context(repository: str | Path | None) -> str | None:
    root = Path(repository) if repository is not None else Path.cwd()
    path = root / "CURRENT.md"
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def _memory_tokens(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", text).lower()
    tokens = set(_LATIN_TOKEN_PATTERN.findall(normalized))
    cjk = "".join(_CJK_PATTERN.findall(normalized))
    tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {token for token in tokens if token and token not in _RECALL_STOPWORDS}


def _recalled_user_turns(
    memory: MemoryStore,
    query: str,
    *,
    channel: str,
    conversation_id: str,
    scan_limit: int = 240,
    limit: int = 2,
) -> list[str]:
    query_tokens = _memory_tokens(query)
    if not query_tokens:
        return []

    scored: list[tuple[int, int, str]] = []
    for event in memory.recent_events(scan_limit):
        if event.event_type != "conversation.user":
            continue
        if (
            event.context.get("channel") == channel
            and event.context.get("conversation_id") == conversation_id
        ):
            continue
        overlap = len(query_tokens.intersection(_memory_tokens(event.content)))
        if overlap <= 0:
            continue
        scored.append((overlap, event.id, event.content.strip()))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [content for _, _, content in scored[:limit] if content]


def build_resident_natural_context(
    *,
    state_dir: str | Path,
    qq_enabled: bool,
    engineering_enabled: bool,
    repository: str | Path | None = None,
    memory: MemoryStore | None = None,
    query: str | None = None,
    channel: str = "",
    conversation_id: str = "",
) -> str:
    """Turn a few current resident/project/memory facts into compact context."""

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

    if memory is not None and isinstance(query, str) and query.strip():
        recalled = _recalled_user_turns(
            memory,
            query,
            channel=channel,
            conversation_id=conversation_id,
        )
        if recalled:
            lines.extend(["", "过去对话中可能与眼前这句话有关的用户原话："])
            lines.extend(f"- {text}" for text in recalled)
            lines.append("只有确实有助于当前问题时才使用这些过去内容。")

    lines.append(
        "这些只是当前事实；只在与眼前问题相关时自然使用，不需要逐条复述。"
    )
    return "\n".join(lines)
