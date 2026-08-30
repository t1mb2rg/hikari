from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.model_reasoner import ChatMessage
from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from conversation.receipts import ConversationReceiptStore
from conversation.remote import ConversationRequestProcessor
from memory.store import MemoryStore
from user_model import (
    ModelUserFactExtractor,
    UserFactCandidate,
    UserFactCategory,
    UserModelService,
    UserModelStore,
    make_evidence_key,
)


class QueueProvider:
    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        value = self.replies.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _runtime(
    tmp_path: Path,
    provider: QueueProvider,
) -> tuple[ConversationEngine, UserModelService]:
    service = UserModelService(UserModelStore(tmp_path / "user_model.db"))
    engine = ConversationEngine(
        provider,
        MemoryStore(tmp_path / "memory.db"),
        user_model_service=service,
        user_fact_extractor=ModelUserFactExtractor(provider),
    )
    return engine, service


def test_successful_conversation_assimilates_fact_after_main_reply(tmp_path: Path):
    provider = QueueProvider(
        [
            "记住了。",
            '{"facts":[{"category":"preference","key":"perfume_scent_family",'
            '"value":"木质","statement":"用户长期偏好木质调香水。","confidence":0.98}]}',
        ]
    )
    engine, service = _runtime(tmp_path, provider)

    reply = engine.respond(
        UserTurn("qq", "private:7", "以后推荐香水时，我更喜欢木质。"),
        source_ref="qq:bot:1",
    )

    assert reply.text == "记住了。"
    facts = service.store.active_facts()
    assert len(facts) == 1
    assert facts[0].key == "perfume_scent_family"
    assert facts[0].source_ref == "qq:bot:1"
    assert facts[0].provenance["channel"] == "qq"
    assert len(provider.calls) == 2


def test_failed_main_provider_does_not_extract_or_write_user_model(tmp_path: Path):
    provider = QueueProvider([RuntimeError("temporary outage")])
    engine, service = _runtime(tmp_path, provider)

    with pytest.raises(RuntimeError, match="temporary outage"):
        engine.respond(
            UserTurn("qq", "private:7", "我喜欢木质香水。"),
            source_ref="qq:bot:2",
        )

    assert service.store.audit_history() == []
    assert MemoryStore(tmp_path / "memory.db").recent_events(10) == []
    assert len(provider.calls) == 1


def test_extractor_failure_does_not_fail_successful_conversation(tmp_path: Path):
    provider = QueueProvider(["主回答成功。", "not-json"])
    engine, service = _runtime(tmp_path, provider)

    reply = engine.respond(
        UserTurn("qq", "private:7", "我喜欢木质香水。"),
        source_ref="qq:bot:3",
    )

    assert reply.text == "主回答成功。"
    assert service.store.audit_history() == []
    assert len(MemoryStore(tmp_path / "memory.db").recent_events(10)) == 2


def test_restart_retrieves_persistent_active_fact_into_future_prompt(tmp_path: Path):
    first_provider = QueueProvider(
        [
            "知道了。",
            '{"facts":[{"category":"preference","key":"perfume_scent_family",'
            '"value":"木质","statement":"用户长期偏好木质调香水。","confidence":0.98}]}',
        ]
    )
    first_engine, _ = _runtime(tmp_path, first_provider)
    first_engine.respond(
        UserTurn("qq", "private:7", "以后推荐香水时，我更喜欢木质。"),
        source_ref="qq:bot:4",
    )

    restarted_provider = QueueProvider(["可以。", '{"facts":[]}'])
    restarted_engine, _ = _runtime(tmp_path, restarted_provider)
    restarted_engine.respond(
        UserTurn("qq", "private:7", "给我推荐点香水。"),
        source_ref="qq:bot:5",
    )

    grounding = json.loads(restarted_provider.calls[0][1].content)
    assert grounding["memory_provenance"]["known_user"]["source"] == (
        "persistent_user_model_active_facts"
    )
    assert grounding["known_user"] == [
        {
            "category": "preference",
            "confidence": 0.98,
            "id": 1,
            "key": "perfume_scent_family",
            "provenance": "persistent_user_model",
            "revision": 1,
            "statement": "用户长期偏好木质调香水。",
        }
    ]


class BrokenService:
    def retrieve(self, query: str, *, limit: int):
        raise OSError("db unavailable")

    def grounding(self, facts):
        return []

    def assimilate(self, candidates):
        raise OSError("db unavailable")


class StaticExtractor:
    def extract(self, *, source_ref, current_user_text, recent_history, provenance):
        return [
            UserFactCandidate(
                category=UserFactCategory.PREFERENCE,
                key="perfume_scent_family",
                value="木质",
                statement="用户偏好木质调香水。",
                confidence=0.95,
                source_ref=source_ref,
                evidence_key=make_evidence_key(
                    source_ref,
                    "preference",
                    "perfume_scent_family",
                ),
                provenance=provenance,
            )
        ]


def test_retrieval_and_store_failure_leave_conversation_working(tmp_path: Path):
    provider = QueueProvider(["仍然能回答。"])
    engine = ConversationEngine(
        provider,
        MemoryStore(tmp_path / "memory.db"),
        user_model_service=BrokenService(),  # type: ignore[arg-type]
        user_fact_extractor=StaticExtractor(),  # type: ignore[arg-type]
    )

    reply = engine.respond(
        UserTurn("qq", "private:7", "给我推荐点香水。"),
        source_ref="qq:bot:6",
    )

    assert reply.text == "仍然能回答。"
    assert len(provider.calls) == 1
    grounding = json.loads(provider.calls[0][1].content)
    assert grounding["known_user"] == []
    assert len(MemoryStore(tmp_path / "memory.db").recent_events(10)) == 2


def test_request_retry_does_not_repeat_main_reply_or_user_model_write(tmp_path: Path):
    provider = QueueProvider(
        [
            "收到。",
            '{"facts":[{"category":"preference","key":"perfume_scent_family",'
            '"value":"木质","statement":"用户偏好木质调香水。","confidence":0.98}]}',
        ]
    )
    engine, service = _runtime(tmp_path, provider)
    processor = ConversationRequestProcessor(
        engine,
        ConversationReceiptStore(tmp_path / "receipts.db"),
    )
    turn = UserTurn("qq", "private:7", "我更喜欢木质香水。")

    first, first_duplicate = processor.process("qq:bot:7", turn)
    retry, retry_duplicate = processor.process("qq:bot:7", turn)

    assert first == retry
    assert first_duplicate is False
    assert retry_duplicate is True
    assert len(provider.calls) == 2
    assert len(service.store.audit_history()) == 1
    assert len(service.store.evidence_history()) == 1
