from __future__ import annotations

from pathlib import Path
import re
import unicodedata

from awareness import (
    ContextCollector,
    ForegroundContextProvider,
    InputActivityContextProvider,
)
from engineering.session import EngineeringSessionStore
from memory.store import MemoryStore
from user_model import UserModelService


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
_FOREGROUND_DIRECT_MARKERS = (
    "前台窗口",
    "当前前台",
    "当前窗口",
    "我现在在干嘛",
    "我正在干嘛",
    "我现在在做什么",
    "我正在做什么",
    "我现在在用什么",
    "我正在用什么",
    "我现在打开",
    "我现在开着",
    "我现在是在",
    "我的屏幕",
    "foreground",
)
_ACTIVITY_DIRECT_MARKERS = (
    "没动电脑",
    "没碰电脑",
    "没操作电脑",
    "多久没动",
    "多久没碰",
    "多久没操作",
    "空闲多久",
    "闲置多久",
    "idle",
)
_TIME_MARKERS = ("现在", "当前", "刚刚", "刚才")
_FOREGROUND_OBJECT_MARKERS = ("窗口", "应用", "程序", "软件", "浏览器", "屏幕", "桌面")
_ACTIVITY_OBJECT_MARKERS = ("键盘", "鼠标", "键鼠", "输入", "操作电脑", "动电脑")


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
    exclude_event_ids: set[int] | None = None,
    scan_limit: int = 240,
    limit: int = 2,
) -> list[str]:
    query_tokens = _memory_tokens(query)
    if not query_tokens:
        return []

    excluded = exclude_event_ids or set()
    scored: list[tuple[int, int, str]] = []
    for event in memory.recent_events(scan_limit):
        if event.id in excluded or event.event_type != "conversation.user":
            continue
        overlap = len(query_tokens.intersection(_memory_tokens(event.content)))
        if overlap <= 0:
            continue
        content = event.content.strip()
        if content:
            scored.append((overlap, event.id, content))

    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [content for _, _, content in scored[:limit]]


def add_recalled_conversation_context(
    context: str,
    *,
    memory: MemoryStore,
    query: str,
    exclude_event_ids: set[int] | None = None,
) -> str:
    recalled = _recalled_user_turns(
        memory,
        query,
        exclude_event_ids=exclude_event_ids,
    )
    if not recalled:
        return context

    lines = [context, "", "过去对话中可能与眼前这句话有关的用户原话："]
    lines.extend(f"- {text}" for text in recalled)
    lines.append("只有确实有助于当前问题时才使用这些过去内容。")
    return "\n".join(lines)


def add_user_model_context(
    context: str,
    *,
    user_model_service: UserModelService | None,
    query: str,
    limit: int = 2,
) -> str:
    """Expose only a few relevant active user facts, without database metadata."""

    if user_model_service is None or limit <= 0:
        return context
    try:
        facts = user_model_service.retrieve(query, limit=limit)
    except Exception:
        return context

    statements = [fact.statement.strip() for fact in facts if fact.statement.strip()]
    if not statements:
        return context

    lines = [context, "", "与当前问题相关的当前有效用户事实："]
    lines.extend(f"- {statement}" for statement in statements[:limit])
    lines.append("只在确实相关时自然使用这些事实，不需要提及其存储方式或内部字段。")
    return "\n".join(lines)


def _foreground_relevant(query: str) -> bool:
    text = unicodedata.normalize("NFKC", query).casefold()
    if any(marker in text for marker in _FOREGROUND_DIRECT_MARKERS):
        return True
    has_personal_anchor = any(marker in text for marker in ("我现在", "我正在", "我刚刚", "我刚才", "电脑现在"))
    return (
        has_personal_anchor
        and any(marker in text for marker in _TIME_MARKERS)
        and any(marker in text for marker in _FOREGROUND_OBJECT_MARKERS)
    )


def _activity_relevant(query: str) -> bool:
    text = unicodedata.normalize("NFKC", query).casefold()
    if any(marker in text for marker in _ACTIVITY_DIRECT_MARKERS):
        return True
    has_personal_anchor = any(marker in text for marker in ("我现在", "我正在", "我刚刚", "我刚才", "电脑现在"))
    return (
        has_personal_anchor
        and any(marker in text for marker in _TIME_MARKERS)
        and any(marker in text for marker in _ACTIVITY_OBJECT_MARKERS)
    )


def _default_conversation_awareness_collector() -> ContextCollector:
    return ContextCollector(
        [
            InputActivityContextProvider(),
            ForegroundContextProvider(),
        ]
    )


def add_awareness_context(
    context: str,
    *,
    query: str,
    awareness_collector: ContextCollector | None = None,
) -> str:
    """Expose raw local activity signals only when the current turn asks for them."""

    wants_foreground = _foreground_relevant(query)
    wants_activity = _activity_relevant(query)
    if not wants_foreground and not wants_activity:
        return context

    requested_provider_names: set[str] = set()
    if wants_foreground:
        requested_provider_names.add("foreground")
    if wants_activity:
        requested_provider_names.add("input_activity")

    collector = awareness_collector or _default_conversation_awareness_collector()
    selected_collector = ContextCollector(
        provider
        for provider in collector.providers
        if provider.name in requested_provider_names
    )
    try:
        snapshot = selected_collector.capture()
    except Exception:
        return context

    facts: list[str] = []
    if wants_foreground:
        foreground = snapshot.providers.get("foreground", {})
        if foreground.get("available") is True:
            title = str(foreground.get("title", "")).strip()
            if title:
                facts.append(f"- 操作系统当前报告的前台窗口标题是“{title}”。")
            else:
                facts.append("- 操作系统当前报告存在前台窗口，但没有可用的窗口标题。")
        elif foreground.get("supported") is False:
            facts.append("- 当前系统不支持前台窗口信号。")
        else:
            facts.append("- 当前没有可用的前台窗口信号。")

    if wants_activity:
        activity = snapshot.providers.get("input_activity", {})
        if activity.get("supported") is True:
            idle_seconds = activity.get("idle_seconds")
            if isinstance(idle_seconds, (int, float)) and not isinstance(idle_seconds, bool):
                facts.append(
                    f"- 系统记录的最近一次本机键盘或鼠标输入距今约 {max(0, round(float(idle_seconds)))} 秒。"
                )
            elif activity.get("recent_input") is True:
                facts.append("- 最近检测到本机键盘或鼠标输入。")
            elif activity.get("recent_input") is False:
                facts.append("- 最近没有检测到本机键盘或鼠标输入。")
        else:
            facts.append("- 当前没有可用的本机输入活动信号。")

    if not facts:
        return context

    lines = [context, "", "与眼前问题直接相关的当前环境信号：", *facts]
    lines.append(
        "这些只是操作系统观测到的信号，不代表用户的意图、专注状态或是否在场。"
    )
    return "\n".join(lines)


def build_selected_conversation_context(
    base_context: str,
    *,
    memory: MemoryStore,
    user_model_service: UserModelService | None,
    query: str,
    exclude_event_ids: set[int] | None = None,
    awareness_collector: ContextCollector | None = None,
) -> str:
    """Select the small model-visible context for one conversation turn."""

    context = add_recalled_conversation_context(
        base_context,
        memory=memory,
        query=query,
        exclude_event_ids=exclude_event_ids,
    )
    context = add_user_model_context(
        context,
        user_model_service=user_model_service,
        query=query,
        limit=2,
    )
    return add_awareness_context(
        context,
        query=query,
        awareness_collector=awareness_collector,
    )


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
