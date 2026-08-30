from __future__ import annotations

from collections.abc import Mapping, Sequence
import json

from brain.model_reasoner import ChatMessage, ChatProvider

from .models import UserFactCandidate, UserFactCategory
from .service import make_evidence_key


MAX_CANDIDATES_PER_TURN = 4
MAX_EXTRACTION_TURN_CHARS = 8_000
MAX_EXTRACTION_HISTORY_CHARS = 2_000

EXTRACTION_SYSTEM_INSTRUCTIONS = """You extract only durable user-model fact candidates from the CURRENT user message.
You do not answer the user and you never write storage. Return exactly one JSON object with exactly one key named `facts`.
`facts` must be an array of at most 4 objects. Every object must contain exactly: `category`, `key`, `value`, `statement`, `confidence`.
Allowed categories: preference, goal, project, habit, capability, relationship.
`key` must be a stable lowercase English snake_case conceptual slot. Reuse the same key when the current message revises an earlier fact.
`value` must be a compact normalized value. `statement` must be a concise Simplified Chinese statement that preserves the user's meaning and subject.
`confidence` must be a JSON number from 0 to 1 and should be >= 0.8 only for explicit user claims.
Persist only explicit, durable, future-useful information about this user. Ignore ordinary small talk, transient mood, one-off state, questions, assistant claims, pasted logs/transcripts, and facts about third parties.
Recent history may resolve ellipsis or the subject of a revision, but every candidate must be asserted or revised by the CURRENT user message.
If nothing qualifies, return {"facts":[]}.
Examples:
CURRENT: 以后给我推荐香水，我更喜欢木质，而且别太张扬。
OUTPUT: {"facts":[{"category":"preference","key":"perfume_scent_family","value":"木质","statement":"用户长期偏好木质调香水。","confidence":0.98},{"category":"preference","key":"perfume_style_intensity","value":"低调","statement":"用户推荐香水时偏好不过分张扬的风格。","confidence":0.97}]}
RECENT: 用户偏好不过分张扬的香水。 CURRENT: 最近我反而想试试张扬一点的。
OUTPUT: {"facts":[{"category":"preference","key":"perfume_style_intensity","value":"张扬","statement":"用户目前想尝试更张扬的香水风格。","confidence":0.97}]}"""


class UserFactExtractionError(ValueError):
    pass


class ModelUserFactExtractor:
    """Strict model-to-candidate boundary; the model receives no store authority."""

    def __init__(self, provider: ChatProvider) -> None:
        if not isinstance(provider, ChatProvider):
            raise TypeError("ModelUserFactExtractor requires a ChatProvider")
        self.provider = provider

    def extract(
        self,
        *,
        source_ref: str,
        current_user_text: str,
        recent_history: Sequence[Mapping[str, str]],
        provenance: Mapping[str, str],
    ) -> list[UserFactCandidate]:
        payload = {
            "recent_history": [
                {
                    "role": str(item.get("role", ""))[:32],
                    "content": str(item.get("content", ""))[
                        :MAX_EXTRACTION_HISTORY_CHARS
                    ],
                }
                for item in list(recent_history)[-6:]
            ],
            "current_user_message": current_user_text[:MAX_EXTRACTION_TURN_CHARS],
        }
        messages = (
            ChatMessage(role="system", content=EXTRACTION_SYSTEM_INSTRUCTIONS),
            ChatMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        raw = self.provider.complete(messages)
        return parse_candidate_output(
            raw,
            source_ref=source_ref,
            provenance=provenance,
        )


def parse_candidate_output(
    raw: str,
    *,
    source_ref: str,
    provenance: Mapping[str, str],
) -> list[UserFactCandidate]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise UserFactExtractionError("extractor output must be strict JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"facts"}:
        raise UserFactExtractionError("extractor output must contain only facts")
    facts = payload["facts"]
    if not isinstance(facts, list):
        raise UserFactExtractionError("extractor facts must be an array")
    if len(facts) > MAX_CANDIDATES_PER_TURN:
        raise UserFactExtractionError("extractor returned too many facts")

    candidates: list[UserFactCandidate] = []
    required = {"category", "key", "value", "statement", "confidence"}
    for item in facts:
        if not isinstance(item, dict) or set(item) != required:
            raise UserFactExtractionError("each extracted fact must match the strict schema")
        if any(not isinstance(item[name], str) for name in ("category", "key", "value", "statement")):
            raise UserFactExtractionError("fact text fields must be strings")
        confidence = item["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise UserFactExtractionError("fact confidence must be a number")
        try:
            category = UserFactCategory(item["category"])
        except ValueError as exc:
            raise UserFactExtractionError("fact category is not supported") from exc
        key = item["key"].strip().lower()
        candidates.append(
            UserFactCandidate(
                category=category,
                key=key,
                value=item["value"],
                statement=item["statement"],
                confidence=float(confidence),
                source_ref=source_ref,
                evidence_key=make_evidence_key(source_ref, category.value, key),
                provenance=dict(provenance),
            )
        )
    return candidates
