from __future__ import annotations

from collections.abc import Sequence
import json
from numbers import Real
from typing import Any

from brain import ChatMessage, ChatProvider
from memory import DurableMemory, MemoryCandidate, MemoryKind
from memory.models import parse_memory_kind


LEARNING_CONTEXT_KEY = "_hikari_learning"
ALLOWED_LEARNING_KINDS = frozenset({MemoryKind.USER_MODEL, MemoryKind.SEMANTIC})

REFLECTION_INSTRUCTIONS = """You are Hikari's bounded reflection layer.
Your task is to inspect durable memories and propose at most one conservative, reusable learning.
A learning is not a summary of everything. It should be a stable inference that could improve future understanding.
Use `user_model` only for a durable understanding about the user, and cite at least two distinct memory IDs.
Use `semantic` for a reusable fact or relationship supported by the supplied memories.
Do not infer sensitive personal traits from indirect evidence. Do not invent evidence.
If evidence is weak, contradictory, or merely a one-off preference, return decision `none`.
Return JSON only, with exactly one of these shapes:
{"decision":"none","reason":"..."}
{"decision":"propose","kind":"user_model|semantic","content":"...","confidence":0.0,"evidence_memory_ids":[1,2],"reason":"..."}
Confidence must be between 0 and 1. Evidence IDs must come from the supplied memories."""


class LearningReflectionError(RuntimeError):
    """Raised when a model returns an invalid reflective-learning proposal."""


class LearningReflector:
    """Model-backed reflection that can only propose reviewable memory.

    Reflection never writes to MemoryStore. Accepted candidates continue through
    the existing MemoryReview path so one model response cannot silently rewrite
    Hikari's durable understanding.
    """

    def __init__(self, provider: ChatProvider, *, max_memories: int = 12) -> None:
        if max_memories <= 0:
            raise ValueError("max_memories must be positive")
        self.provider = provider
        self.max_memories = int(max_memories)

    def reflect(self, memories: Sequence[DurableMemory]) -> MemoryCandidate | None:
        selected = list(memories[: self.max_memories])
        if not selected:
            return None

        available_ids = {memory.id for memory in selected}
        payload = {
            "memories": [
                {
                    "id": memory.id,
                    "kind": memory.kind.value,
                    "content": memory.content,
                    "confidence": memory.confidence,
                    "created_at": memory.created_at,
                }
                for memory in selected
            ]
        }
        messages = (
            ChatMessage(role="system", content=REFLECTION_INSTRUCTIONS),
            ChatMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )

        raw = self.provider.complete(messages).strip()
        response = _parse_response(raw)
        decision = response.get("decision")

        if decision == "none":
            reason = response.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise LearningReflectionError("reflection `none` decision requires a reason")
            return None

        if decision != "propose":
            raise LearningReflectionError("reflection decision must be `none` or `propose`")

        kind_value = response.get("kind")
        try:
            kind = parse_memory_kind(kind_value)
        except (TypeError, ValueError) as exc:
            raise LearningReflectionError("invalid reflective learning kind") from exc
        if kind not in ALLOWED_LEARNING_KINDS:
            raise LearningReflectionError(
                "reflective learning kind must be user_model or semantic"
            )

        content = response.get("content")
        reason = response.get("reason")
        if not isinstance(content, str) or not content.strip():
            raise LearningReflectionError("learning content must not be empty")
        if not isinstance(reason, str) or not reason.strip():
            raise LearningReflectionError("learning reason must not be empty")

        confidence_raw = response.get("confidence")
        if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, Real):
            raise LearningReflectionError("learning confidence must be numeric")
        confidence = float(confidence_raw)
        if not 0.0 <= confidence <= 1.0:
            raise LearningReflectionError("learning confidence must be between 0 and 1")

        evidence_raw = response.get("evidence_memory_ids")
        if not isinstance(evidence_raw, list) or not evidence_raw:
            raise LearningReflectionError("learning requires evidence_memory_ids")
        if any(isinstance(item, bool) or not isinstance(item, int) for item in evidence_raw):
            raise LearningReflectionError("evidence_memory_ids must contain integers")

        evidence_ids = tuple(dict.fromkeys(evidence_raw))
        unknown_ids = set(evidence_ids) - available_ids
        if unknown_ids:
            raise LearningReflectionError(
                f"learning cited unknown memory IDs: {', '.join(map(str, sorted(unknown_ids)))}"
            )
        if kind is MemoryKind.USER_MODEL and len(evidence_ids) < 2:
            raise LearningReflectionError(
                "user_model learning requires at least two distinct evidence memories"
            )

        return MemoryCandidate(
            kind=kind,
            content=content.strip(),
            context={
                LEARNING_CONTEXT_KEY: {
                    "method": "model_reflection",
                    "evidence_memory_ids": list(evidence_ids),
                    "reason": reason.strip(),
                }
            },
            confidence=confidence,
            salience=confidence,
            source_event_id=None,
            reason=f"reflective learning proposal: {reason.strip()}",
        )


def _parse_response(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if len(lines) >= 3:
            text = "\n".join(lines[1:-1]).strip()
            if text.startswith("json\n"):
                text = text[5:].lstrip()

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LearningReflectionError("reflection provider returned invalid JSON") from exc

    if not isinstance(value, dict):
        raise LearningReflectionError("reflection response must be a JSON object")
    return value
