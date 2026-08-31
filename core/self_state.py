from __future__ import annotations

from collections.abc import Mapping
import os


DEVELOPMENT_STATE = {
    "milestone": "M7",
    "milestone_name": "Evolution",
    "active_slice": "M7-05",
    "active_slice_name": "Grounded Self State",
    "status": "active",
    "source": "runtime_manifest",
    "note": (
        "This is Hikari's canonical current development state. It must not be "
        "re-inferred from README or historical roadmap prose."
    ),
}


def _runtime_bool(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def describe_self_state(
    environment: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Return machine-grounded facts about Hikari's current implementation state.

    This intentionally describes only facts the running system can safely assert.
    In particular, configured Engineering Runtime support is not equivalent to
    continuous filesystem perception or proof that a worker process is currently alive.
    """

    env = os.environ if environment is None else environment
    engineering_enabled = _runtime_bool(
        env,
        "HIKARI_ENGINEERING_ENABLED",
        default=False,
    )

    return {
        "development": dict(DEVELOPMENT_STATE),
        "engineering": {
            "relationship": "internal_hikari_capability",
            "conversation_read_only_enabled": engineering_enabled,
            "execution_model": "durable_engineering_session_plus_separate_worker_process",
            "result_model": "result_is_persisted_in_hikari_state_then_delivered_through_hikari_outbox",
            "repository_write_enabled": False,
            "direct_filesystem_perception": False,
            "continuous_filesystem_perception": False,
            "instantaneous_filesystem_access_claim": False,
            "worker_liveness": "not_asserted_by_self_state",
        },
        "epistemic_boundaries": {
            "engineering_inspection": (
                "Repository inspection is delegated to Hikari's internal EngineeringSession "
                "and separate Engineering Worker. The conversation model receives the persisted "
                "result after that work completes."
            ),
            "filesystem": (
                "Hikari does not continuously or directly sense the filesystem merely because "
                "Engineering Runtime exists. Do not describe delegated repository inspection as "
                "instantaneous touch, direct perception, or an always-on filesystem sense."
            ),
            "metaphor_vs_fact": (
                "Expressive metaphors may be used as personality, but factual questions about "
                "implementation, authority, sensing, memory, or execution must follow this state."
            ),
        },
    }
