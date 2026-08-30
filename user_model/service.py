from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
import hashlib
import math
import re
import unicodedata

from .models import AssimilationResult, UserFact, UserFactCandidate
from .store import UserModelStore


_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_LATIN_TOKEN_PATTERN = re.compile(r"[a-z0-9_]+")
_CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")


def make_evidence_key(source_ref: str, category: str, fact_key: str) -> str:
    source = _required_text(source_ref, "source_ref", 256)
    material = f"{source}\x1f{category}\x1f{fact_key}".encode("utf-8")
    return f"conversation:{hashlib.sha256(material).hexdigest()}"


class UserModelService:
    """Normalize, validate, assimilate, and deterministically retrieve user facts."""

    def __init__(
        self,
        store: UserModelStore,
        *,
        retrieval_scan_limit: int = 200,
        retrieval_limit: int = 6,
    ) -> None:
        if not isinstance(store, UserModelStore):
            raise TypeError("UserModelService requires UserModelStore")
        if retrieval_scan_limit <= 0 or retrieval_limit <= 0:
            raise ValueError("user model retrieval limits must be positive")
        self.store = store
        self.retrieval_scan_limit = int(retrieval_scan_limit)
        self.retrieval_limit = int(retrieval_limit)

    def assimilate(
        self,
        candidates: Sequence[UserFactCandidate],
        *,
        observed_at: datetime | None = None,
    ) -> list[AssimilationResult]:
        normalized: list[UserFactCandidate] = []
        seen_evidence: set[str] = set()
        for candidate in candidates:
            value = self.normalize_candidate(candidate)
            if value.evidence_key in seen_evidence:
                continue
            seen_evidence.add(value.evidence_key)
            normalized.append(value)
        return self.store.assimilate(normalized, observed_at=observed_at)

    def normalize_candidate(self, candidate: UserFactCandidate) -> UserFactCandidate:
        if not isinstance(candidate, UserFactCandidate):
            raise TypeError("candidate must be UserFactCandidate")
        key = unicodedata.normalize("NFKC", candidate.key).strip().lower()
        if not _KEY_PATTERN.fullmatch(key):
            raise ValueError("candidate key must be stable lowercase snake_case")
        value = _required_text(candidate.value, "value", 240)
        statement = _required_text(candidate.statement, "statement", 500)
        source_ref = _required_text(candidate.source_ref, "source_ref", 256)
        confidence = float(candidate.confidence)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("candidate confidence must be between 0.0 and 1.0")
        expected_evidence = make_evidence_key(
            source_ref,
            candidate.category.value,
            key,
        )
        if candidate.evidence_key != expected_evidence:
            raise ValueError("candidate evidence_key does not match its source and fact key")
        provenance = {
            _required_text(str(name), "provenance key", 80): _required_text(
                str(raw_value), "provenance value", 500
            )
            for name, raw_value in dict(candidate.provenance).items()
        }
        return replace(
            candidate,
            key=key,
            value=_normalize_text(value).casefold(),
            statement=_normalize_text(statement),
            confidence=confidence,
            source_ref=source_ref,
            provenance=provenance,
        )

    def retrieve(self, query: str, *, limit: int | None = None) -> list[UserFact]:
        query_text = _required_text(query, "query", 20_000)
        result_limit = self.retrieval_limit if limit is None else int(limit)
        if result_limit <= 0:
            return []
        bounded_limit = min(result_limit, self.retrieval_limit)
        query_tokens = _tokens(query_text)
        facts = self.store.active_facts(self.retrieval_scan_limit)
        scored: list[tuple[float, float, str, int, UserFact]] = []
        for fact in facts:
            fact_tokens = _tokens(
                " ".join(
                    (
                        fact.category.value,
                        fact.key.replace("_", " "),
                        fact.value,
                        fact.statement,
                    )
                )
            )
            overlap = len(query_tokens.intersection(fact_tokens))
            if overlap == 0:
                continue
            lexical = overlap / max(1, len(query_tokens))
            score = lexical * 4.0 + fact.confidence
            scored.append(
                (score, fact.confidence, fact.last_confirmed_at, fact.id, fact)
            )
        scored.sort(key=lambda item: item[:4], reverse=True)
        return [item[4] for item in scored[:bounded_limit]]

    @staticmethod
    def grounding(facts: Iterable[UserFact]) -> list[dict[str, object]]:
        return [
            {
                "id": fact.id,
                "category": fact.category.value,
                "key": fact.key,
                "statement": fact.statement,
                "confidence": fact.confidence,
                "revision": fact.revision,
                "provenance": "persistent_user_model",
            }
            for fact in facts
        ]


def _required_text(value: str, name: str, max_length: int) -> str:
    text = str(value).strip()
    if not text:
        raise ValueError(f"{name} must not be empty")
    if len(text) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")
    return text


def _normalize_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split())


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).lower()
    tokens = set(_LATIN_TOKEN_PATTERN.findall(normalized))
    cjk = "".join(_CJK_PATTERN.findall(normalized))
    tokens.update(cjk)
    tokens.update(cjk[index : index + 2] for index in range(max(0, len(cjk) - 1)))
    return {token for token in tokens if token}
