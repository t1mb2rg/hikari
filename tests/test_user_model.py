from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from user_model import (
    AssimilationDecision,
    UserFactCandidate,
    UserFactCategory,
    UserFactExtractionError,
    UserFactStatus,
    UserModelService,
    UserModelStore,
    make_evidence_key,
    parse_candidate_output,
)


def _candidate(
    source_ref: str,
    *,
    key: str = "perfume_style_intensity",
    value: str = "低调",
    statement: str = "用户推荐香水时偏好不过分张扬的风格。",
    confidence: float = 0.9,
    category: UserFactCategory = UserFactCategory.PREFERENCE,
) -> UserFactCandidate:
    return UserFactCandidate(
        category=category,
        key=key,
        value=value,
        statement=statement,
        confidence=confidence,
        source_ref=source_ref,
        evidence_key=make_evidence_key(source_ref, category.value, key),
        provenance={"source": "test", "source_ref": source_ref},
    )


def test_user_model_persists_in_independent_database_and_survives_restart(
    tmp_path: Path,
):
    path = tmp_path / "user_model.db"
    first = UserModelService(UserModelStore(path))
    created = first.assimilate([_candidate("qq:1")])[0].fact

    restarted = UserModelService(UserModelStore(path))
    active = restarted.store.active_facts()

    assert path.is_file()
    assert active == [created]
    assert not (tmp_path / "memory.db").exists()


def test_same_evidence_retry_is_idempotently_deduplicated(tmp_path: Path):
    service = UserModelService(UserModelStore(tmp_path / "user_model.db"))
    candidate = _candidate("qq:same")

    first = service.assimilate([candidate])[0]
    retry = service.assimilate([candidate])[0]

    assert first.decision is AssimilationDecision.CREATED
    assert retry.decision is AssimilationDecision.DUPLICATE
    assert retry.fact.id == first.fact.id
    assert len(service.store.audit_history()) == 1
    assert len(service.store.evidence_history()) == 1


def test_compatible_fact_confirms_without_replacing_content(tmp_path: Path):
    service = UserModelService(UserModelStore(tmp_path / "user_model.db"))
    first_time = datetime(2026, 8, 30, tzinfo=timezone.utc)
    second_time = first_time + timedelta(days=1)
    first = service.assimilate(
        [_candidate("qq:1", confidence=0.7)],
        observed_at=first_time,
    )[0].fact
    confirmed = service.assimilate(
        [
            _candidate(
                "qq:2",
                statement="用户喜欢低调的香水。",
                confidence=0.95,
            )
        ],
        observed_at=second_time,
    )[0]

    assert confirmed.decision is AssimilationDecision.CONFIRMED
    assert confirmed.fact.id == first.id
    assert confirmed.fact.statement == first.statement
    assert confirmed.fact.first_seen_at == first.first_seen_at
    assert confirmed.fact.last_confirmed_at == second_time.isoformat()
    assert confirmed.fact.confidence == 0.95
    assert [item.decision for item in service.store.evidence_history()] == [
        AssimilationDecision.CREATED,
        AssimilationDecision.CONFIRMED,
    ]


def test_contradiction_supersedes_or_records_lower_confidence_dispute(
    tmp_path: Path,
):
    service = UserModelService(UserModelStore(tmp_path / "user_model.db"))
    original = service.assimilate([_candidate("qq:1", confidence=0.9)])[0].fact
    revision = service.assimilate(
        [
            _candidate(
                "qq:2",
                value="张扬",
                statement="用户目前想尝试更张扬的香水风格。",
                confidence=0.97,
            )
        ]
    )[0]
    disputed = service.assimilate(
        [
            _candidate(
                "qq:3",
                value="极简",
                statement="用户可能偏好极简香水。",
                confidence=0.4,
            )
        ]
    )[0]

    history = service.store.audit_history(
        category=UserFactCategory.PREFERENCE,
        key="perfume_style_intensity",
    )
    assert revision.decision is AssimilationDecision.SUPERSEDED
    assert revision.fact.revision == 2
    assert revision.fact.supersedes_id == original.id
    assert disputed.decision is AssimilationDecision.DISPUTED
    assert disputed.fact.status is UserFactStatus.DISPUTED
    assert disputed.fact.supersedes_id == revision.fact.id
    assert [fact.status for fact in history] == [
        UserFactStatus.SUPERSEDED,
        UserFactStatus.ACTIVE,
        UserFactStatus.DISPUTED,
    ]
    assert service.store.active_facts() == [revision.fact]


def test_retrieval_is_active_only_relevant_deterministic_and_bounded(tmp_path: Path):
    service = UserModelService(
        UserModelStore(tmp_path / "user_model.db"),
        retrieval_limit=2,
    )
    service.assimilate(
        [
            _candidate(
                "qq:wood",
                key="perfume_scent_family",
                value="木质",
                statement="用户长期偏好木质调香水。",
                confidence=0.98,
            ),
            _candidate("qq:quiet", confidence=0.96),
            _candidate(
                "qq:coffee",
                key="coffee_roast",
                value="浅烘",
                statement="用户喝咖啡偏好浅烘。",
                confidence=0.99,
            ),
        ]
    )
    service.assimilate(
        [
            _candidate(
                "qq:bold",
                value="张扬",
                statement="用户目前想尝试更张扬的香水风格。",
                confidence=0.99,
            )
        ]
    )

    first = service.retrieve("给我推荐点香水", limit=99)
    second = service.retrieve("给我推荐点香水", limit=99)

    assert first == second
    assert len(first) == 2
    assert {fact.key for fact in first} == {
        "perfume_scent_family",
        "perfume_style_intensity",
    }
    assert all(fact.status is UserFactStatus.ACTIVE for fact in first)
    assert all("不过分张扬" not in fact.statement for fact in first)


@pytest.mark.parametrize(
    "raw",
    [
        "```json\n{\"facts\":[]}\n```",
        '{"facts":{},"extra":true}',
        '{"facts":[{"category":"emotion"}]}',
        "not json",
    ],
)
def test_candidate_parser_rejects_malformed_or_out_of_scope_output(raw: str):
    with pytest.raises(UserFactExtractionError):
        parse_candidate_output(raw, source_ref="qq:1", provenance={"source": "test"})


def test_candidate_parser_accepts_only_typed_candidates():
    candidates = parse_candidate_output(
        '{"facts":[{"category":"preference","key":"perfume_scent_family",'
        '"value":"木质","statement":"用户偏好木质调香水。","confidence":0.98}]}',
        source_ref="qq:1",
        provenance={"source": "conversation"},
    )

    assert len(candidates) == 1
    assert candidates[0].category is UserFactCategory.PREFERENCE
    assert candidates[0].evidence_key == make_evidence_key(
        "qq:1",
        "preference",
        "perfume_scent_family",
    )
