from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_voice import EngineeringVoiceFacts, EngineeringVoiceRenderer
from conversation.natural import NaturalConversationEngine
from memory.store import MemoryStore


class _RecordingProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = []

    def complete(self, messages):
        self.calls.append(tuple(messages))
        return self.response


class _ExplodingProvider:
    def complete(self, messages):
        raise AssertionError("deterministic engineering voice fallback must not call the model")


class _FailingProvider:
    def complete(self, messages):
        raise RuntimeError("provider unavailable")


def test_production_engineering_voice_renders_trusted_terminal_facts(tmp_path: Path) -> None:
    provider = _RecordingProvider(
        "<reaction>done</reaction><reply>搞定了，先生。README 已同步到当前真实能力。</reply>"
    )
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    renderer = EngineeringVoiceRenderer(engine)

    text = renderer.render(
        EngineeringVoiceFacts(
            kind="completed",
            goal="同步 README 的 M7-07 状态",
            status="completed",
            summary="README 已更新，范围检查通过。",
            changed_files=("README.md",),
            branch="hikari/engineering/example",
        ),
        channel="qq",
        conversation_id="private:42",
    )

    assert text == "搞定了，先生。README 已同步到当前真实能力。"
    assert len(provider.calls) == 1
    prompt = provider.calls[0][-1].content
    assert "event: completed" in prompt
    assert "goal: 同步 README 的 M7-07 状态" in prompt
    assert "changed_files: README.md" in prompt
    assert "branch: hikari/engineering/example" in prompt


def test_production_accepted_voice_hides_internal_delegation_plumbing(tmp_path: Path) -> None:
    provider = _RecordingProvider(
        "<reaction>accepted</reaction><reply>好，我来改。只动 README 里你指定的那部分，完成后告诉你结果。</reply>"
    )
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    renderer = EngineeringVoiceRenderer(engine)

    text = renderer.render(
        EngineeringVoiceFacts(
            kind="accepted",
            goal="只更新 README 的 M7-07 Engineering Runtime 部分",
            status="accepted",
            branch="hikari/engineering/example",
            details=(
                "这个任务在项目维护职责内；已经进入持久工程会话，可在隔离工程分支完成修改、测试和提交。",
            ),
        ),
        channel="qq",
        conversation_id="private:42",
    )

    assert text == "好，我来改。只动 README 里你指定的那部分，完成后告诉你结果。"
    assert len(provider.calls) == 1
    prompt = provider.calls[0][-1].content
    assert "event: accepted" in prompt
    assert "goal: 只更新 README 的 M7-07 Engineering Runtime 部分" in prompt
    assert "accepted_scope:" in prompt
    assert "项目维护职责" not in prompt
    assert "持久工程会话" not in prompt
    assert "隔离工程分支" not in prompt
    assert "hikari/engineering/example" not in prompt


def test_production_accepted_voice_degrades_without_exposing_internal_plumbing(
    tmp_path: Path,
) -> None:
    engine = NaturalConversationEngine(_FailingProvider(), MemoryStore(tmp_path / "memory.db"))
    renderer = EngineeringVoiceRenderer(engine)

    text = renderer.render(
        EngineeringVoiceFacts(
            kind="accepted",
            goal="更新 README",
            details=(
                "这个任务在项目维护职责内；已经进入持久工程会话，可在隔离工程分支完成修改、测试和提交。",
            ),
        ),
        channel="qq",
        conversation_id="private:42",
    )

    assert "我来处理" in text
    assert "项目维护职责" not in text
    assert "工程会话" not in text
    assert "工程分支" not in text


def test_base_engineering_voice_fallback_keeps_authority_decision_model_free(
    tmp_path: Path,
) -> None:
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    renderer = EngineeringVoiceRenderer(engine)

    text = renderer.render(
        EngineeringVoiceFacts(
            kind="capability_gap",
            goal="创建 Draft PR",
            capabilities=("engineering.git.open_or_update_draft_pr",),
        ),
        channel="qq",
        conversation_id="private:42",
    )

    assert "能力缺口" in text
    assert "engineering.git.open_or_update_draft_pr" in text
    assert "逐个动作授权" in text


def test_terminal_failure_voice_never_falls_through_as_success(tmp_path: Path) -> None:
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    renderer = EngineeringVoiceRenderer(engine)

    text = renderer.render(
        EngineeringVoiceFacts(
            kind="failed",
            goal="更新 README",
            status="failed",
            summary="范围检查没有通过。",
        ),
        channel="qq",
        conversation_id="private:42",
    )

    assert "没做完" in text
    assert "范围检查没有通过" in text
    assert "搞定" not in text
