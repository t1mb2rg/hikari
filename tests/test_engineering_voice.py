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

    assert text == "搞定了。README 已更新，范围检查通过。"
    assert provider.calls == []


def test_terminal_voice_cannot_turn_committed_evidence_into_pending_stage(tmp_path: Path):
    provider = _RecordingProvider("修改已移交给后续工程层提交。")
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    result = EngineeringVoiceRenderer(engine).render(EngineeringVoiceFacts(
        kind="completed", goal="修改并提交文件", status="completed", summary="提交已完成：abc123。",
    ), channel="qq", conversation_id="private:42")
    assert "提交已完成：abc123" in result
    assert "后续" not in result
    assert provider.calls == []


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

    assert "我来处理" in text
    assert "只更新 README 的 M7-07 Engineering Runtime 部分" in text
    assert provider.calls == []
    assert "项目维护职责" not in text
    assert "持久工程会话" not in text
    assert "隔离工程分支" not in text
    assert "hikari/engineering/example" not in text
    assert "已经开始" not in text
    assert "已完成" not in text


def test_production_accepted_voice_needs_no_working_model(
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
    assert "更新 README" in text


def test_acceptance_uses_current_durable_goal_without_loading_history_or_model(tmp_path: Path, monkeypatch) -> None:
    provider = _RecordingProvider("stale model response")
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))

    def forbidden_history(*args):
        raise AssertionError("accepted receipt must be immediate without history retrieval")

    monkeypatch.setattr(engine, "_recent_history", forbidden_history)
    facts = EngineeringVoiceFacts(
        kind="accepted",
        goal="修正新的安装示例，只修改 README",
        status="accepted",
        summary="This is not terminal evidence",
        branch="hikari/engineering/private-internal-id",
    )
    text = EngineeringVoiceRenderer(engine).render(facts, channel="qq", conversation_id="private:42")
    assert facts.goal in text
    assert provider.calls == []
    assert "stale model response" not in text
    assert "This is not terminal evidence" not in text
    assert "private-internal-id" not in text
    assert "搞定" not in text and "已经开始" not in text


def test_base_acceptance_keeps_goal_identity_and_legacy_boundary_detail(tmp_path: Path) -> None:
    provider = _RecordingProvider("must remain unused")
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    facts = EngineeringVoiceFacts(kind="accepted", goal="检查命令示例", details=("已经开始一个只读工程会话。",))
    text = EngineeringVoiceRenderer(engine).render(facts, channel="cli", conversation_id="local")
    assert facts.goal in text
    assert facts.details[0] in text
    assert provider.calls == []


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
