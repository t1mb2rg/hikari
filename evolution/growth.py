from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Mapping, Sequence

from engineering.session import EngineeringSessionStore


_GAP_STATUSES = frozenset({"observed", "proposed", "resolved"})
_PROPOSAL_STATUSES = frozenset({"proposed", "resolved", "accepted", "dismissed"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}_{digest}"


@dataclass(frozen=True, slots=True)
class GrowthEvidence:
    evidence_id: str
    kind: str
    source_ref: str
    status: str
    summary: str
    observed_at: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "source_ref": self.source_ref,
            "status": self.status,
            "summary": self.summary,
            "observed_at": self.observed_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GrowthEvidence":
        return cls(
            evidence_id=str(payload.get("evidence_id", "")).strip(),
            kind=str(payload.get("kind", "")).strip(),
            source_ref=str(payload.get("source_ref", "")).strip(),
            status=str(payload.get("status", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            observed_at=str(payload.get("observed_at", "")).strip(),
        )


@dataclass(frozen=True, slots=True)
class CapabilityGap:
    gap_id: str
    capability_key: str
    category: str
    summary: str
    status: str
    first_seen_at: str
    last_seen_at: str
    evidence: tuple[GrowthEvidence, ...]

    @property
    def observation_count(self) -> int:
        return len(self.evidence)

    def to_mapping(self) -> dict[str, object]:
        return {
            "gap_id": self.gap_id,
            "capability_key": self.capability_key,
            "category": self.category,
            "summary": self.summary,
            "status": self.status,
            "first_seen_at": self.first_seen_at,
            "last_seen_at": self.last_seen_at,
            "observation_count": self.observation_count,
            "evidence": [item.to_mapping() for item in self.evidence],
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "CapabilityGap":
        raw_evidence = payload.get("evidence") or []
        if not isinstance(raw_evidence, list):
            raw_evidence = []
        status = str(payload.get("status", "observed")).strip().lower()
        if status not in _GAP_STATUSES:
            status = "observed"
        return cls(
            gap_id=str(payload.get("gap_id", "")).strip(),
            capability_key=str(payload.get("capability_key", "")).strip(),
            category=str(payload.get("category", "")).strip(),
            summary=str(payload.get("summary", "")).strip(),
            status=status,
            first_seen_at=str(payload.get("first_seen_at", "")).strip(),
            last_seen_at=str(payload.get("last_seen_at", "")).strip(),
            evidence=tuple(
                GrowthEvidence.from_mapping(item)
                for item in raw_evidence
                if isinstance(item, Mapping)
            ),
        )


@dataclass(frozen=True, slots=True)
class GrowthProposal:
    proposal_id: str
    gap_id: str
    capability_key: str
    title: str
    user_impact: str
    current_workaround: str
    requested_capability: str
    risk_notes: str
    authority_boundary: str
    status: str
    created_at: str
    updated_at: str

    def to_mapping(self) -> dict[str, str]:
        return {
            "proposal_id": self.proposal_id,
            "gap_id": self.gap_id,
            "capability_key": self.capability_key,
            "title": self.title,
            "user_impact": self.user_impact,
            "current_workaround": self.current_workaround,
            "requested_capability": self.requested_capability,
            "risk_notes": self.risk_notes,
            "authority_boundary": self.authority_boundary,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "GrowthProposal":
        status = str(payload.get("status", "proposed")).strip().lower()
        if status not in _PROPOSAL_STATUSES:
            status = "proposed"
        return cls(
            proposal_id=str(payload.get("proposal_id", "")).strip(),
            gap_id=str(payload.get("gap_id", "")).strip(),
            capability_key=str(payload.get("capability_key", "")).strip(),
            title=str(payload.get("title", "")).strip(),
            user_impact=str(payload.get("user_impact", "")).strip(),
            current_workaround=str(payload.get("current_workaround", "")).strip(),
            requested_capability=str(payload.get("requested_capability", "")).strip(),
            risk_notes=str(payload.get("risk_notes", "")).strip(),
            authority_boundary=str(payload.get("authority_boundary", "")).strip(),
            status=status,
            created_at=str(payload.get("created_at", "")).strip(),
            updated_at=str(payload.get("updated_at", "")).strip(),
        )


class GrowthStateStore:
    """Durable, machine-generated capability-gap and proposal state."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def _load(self) -> tuple[dict[str, CapabilityGap], dict[str, GrowthProposal]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}, {}
        if not isinstance(payload, Mapping) or payload.get("version") != 1:
            return {}, {}
        gaps_raw = payload.get("gaps") or []
        proposals_raw = payload.get("proposals") or []
        gaps: dict[str, CapabilityGap] = {}
        proposals: dict[str, GrowthProposal] = {}
        if isinstance(gaps_raw, list):
            for raw in gaps_raw:
                if not isinstance(raw, Mapping):
                    continue
                gap = CapabilityGap.from_mapping(raw)
                if gap.capability_key:
                    gaps[gap.capability_key] = gap
        if isinstance(proposals_raw, list):
            for raw in proposals_raw:
                if not isinstance(raw, Mapping):
                    continue
                proposal = GrowthProposal.from_mapping(raw)
                if proposal.capability_key:
                    proposals[proposal.capability_key] = proposal
        return gaps, proposals

    def _save(
        self,
        gaps: Mapping[str, CapabilityGap],
        proposals: Mapping[str, GrowthProposal],
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "gaps": [item.to_mapping() for item in gaps.values()],
                    "proposals": [item.to_mapping() for item in proposals.values()],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def observe_gap(
        self,
        *,
        capability_key: str,
        category: str,
        summary: str,
        evidence: GrowthEvidence,
    ) -> CapabilityGap:
        gaps, proposals = self._load()
        now = evidence.observed_at or _now_iso()
        current = gaps.get(capability_key)
        if current is None:
            gap = CapabilityGap(
                gap_id=_stable_id("gap", capability_key),
                capability_key=capability_key,
                category=category,
                summary=summary,
                status="observed",
                first_seen_at=now,
                last_seen_at=now,
                evidence=(evidence,),
            )
        else:
            evidence_by_id = {item.evidence_id: item for item in current.evidence}
            evidence_by_id[evidence.evidence_id] = evidence
            ordered = tuple(evidence_by_id.values())[-12:]
            gap = replace(
                current,
                category=category,
                summary=summary,
                status="observed" if current.status == "resolved" else current.status,
                last_seen_at=now,
                evidence=ordered,
            )
        gaps[capability_key] = gap
        self._save(gaps, proposals)
        return gap

    def resolve_gap(self, capability_key: str) -> None:
        gaps, proposals = self._load()
        gap = gaps.get(capability_key)
        if gap is None or gap.status == "resolved":
            return
        now = _now_iso()
        gaps[capability_key] = replace(gap, status="resolved", last_seen_at=now)
        proposal = proposals.get(capability_key)
        if proposal is not None and proposal.status == "proposed":
            proposals[capability_key] = replace(proposal, status="resolved", updated_at=now)
        self._save(gaps, proposals)

    def ensure_proposal(
        self,
        capability_key: str,
        *,
        title: str,
        user_impact: str,
        current_workaround: str,
        requested_capability: str,
        risk_notes: str,
        authority_boundary: str,
    ) -> GrowthProposal | None:
        gaps, proposals = self._load()
        gap = gaps.get(capability_key)
        if gap is None or gap.status == "resolved":
            return None
        current = proposals.get(capability_key)
        now = _now_iso()
        if current is None:
            proposal = GrowthProposal(
                proposal_id=_stable_id("proposal", capability_key),
                gap_id=gap.gap_id,
                capability_key=capability_key,
                title=title,
                user_impact=user_impact,
                current_workaround=current_workaround,
                requested_capability=requested_capability,
                risk_notes=risk_notes,
                authority_boundary=authority_boundary,
                status="proposed",
                created_at=now,
                updated_at=now,
            )
        elif current.status in {"dismissed", "accepted"}:
            return current
        else:
            proposal = replace(
                current,
                title=title,
                user_impact=user_impact,
                current_workaround=current_workaround,
                requested_capability=requested_capability,
                risk_notes=risk_notes,
                authority_boundary=authority_boundary,
                status="proposed",
                updated_at=now,
            )
        gaps[capability_key] = replace(gap, status="proposed")
        proposals[capability_key] = proposal
        self._save(gaps, proposals)
        return proposal

    def snapshot(self) -> dict[str, object]:
        gaps, proposals = self._load()
        active_gaps = [item for item in gaps.values() if item.status != "resolved"]
        active_proposals = [
            item for item in proposals.values() if item.status == "proposed"
        ]
        return {
            "version": 1,
            "captured_at": _now_iso(),
            "active_gap_count": len(active_gaps),
            "active_proposal_count": len(active_proposals),
            "gaps": [item.to_mapping() for item in active_gaps],
            "proposals": [item.to_mapping() for item in active_proposals],
            "epistemic_rule": (
                "Capability gaps and Growth Proposals shown here are machine-backed by listed evidence. "
                "A proposal is not an implemented capability, permission grant, or autonomous goal."
            ),
        }


class GrowthProposalService:
    """Deterministic first-slice policy for turning grounded evidence into proposals."""

    def __init__(
        self,
        store: GrowthStateStore,
        engineering_store: EngineeringSessionStore,
        *,
        operational_observation_threshold: int = 2,
    ) -> None:
        self.store = store
        self.engineering_store = engineering_store
        self.operational_observation_threshold = max(1, int(operational_observation_threshold))

    def sync(self, operational_state: Mapping[str, object] | None = None) -> dict[str, object]:
        if operational_state is not None:
            self._sync_operational(operational_state)
        self._sync_engineering()
        return self.store.snapshot()

    def _sync_operational(self, snapshot: Mapping[str, object]) -> None:
        components = snapshot.get("components")
        if not isinstance(components, Mapping):
            return
        captured_at = str(snapshot.get("captured_at", "")).strip() or _now_iso()
        for name, raw in components.items():
            if not isinstance(raw, Mapping):
                continue
            component = str(name)
            status = str(raw.get("status", "unknown")).strip().lower()
            observed = raw.get("observed") is True
            capability_key = f"observability:{component}"
            if status == "unknown" and not observed:
                evidence = GrowthEvidence(
                    evidence_id=_stable_id(
                        "evidence",
                        f"operational:{component}:{captured_at}",
                    ),
                    kind="operational_state",
                    source_ref=f"operational:{component}",
                    status="unknown",
                    summary=f"Current {component} state is not trustworthily observable.",
                    observed_at=captured_at,
                )
                gap = self.store.observe_gap(
                    capability_key=capability_key,
                    category="observability",
                    summary=f"Hikari lacks a trustworthy current-state observation for {component}.",
                    evidence=evidence,
                )
                if gap.observation_count >= self.operational_observation_threshold:
                    self.store.ensure_proposal(
                        capability_key,
                        title=f"补齐 {component} 的可信运行状态探针",
                        user_impact=(
                            f"Without a trustworthy probe Hikari cannot reliably tell the user whether {component} is available now."
                        ),
                        current_workaround=(
                            "Keep the state explicit as unknown and require separate manual diagnosis."
                        ),
                        requested_capability=(
                            f"Add a bounded, secret-safe, read-only point-in-time probe for {component}."
                        ),
                        risk_notes=(
                            "Diagnostics only. Do not expose credentials, arbitrary logs, or add control side effects."
                        ),
                        authority_boundary="no_new_execution_authority",
                    )
            else:
                self.store.resolve_gap(capability_key)

        engineering = components.get("engineering")
        if isinstance(engineering, Mapping):
            details = engineering.get("details")
            if isinstance(details, Mapping):
                liveness = str(details.get("worker_liveness", "")).strip().lower()
                capability_key = "observability:engineering_worker"
                if liveness == "unknown":
                    evidence = GrowthEvidence(
                        evidence_id=_stable_id(
                            "evidence",
                            f"operational:engineering_worker:{captured_at}",
                        ),
                        kind="operational_state",
                        source_ref="operational:engineering_worker",
                        status="unknown",
                        summary="Engineering Worker liveness is not trustworthily observable.",
                        observed_at=captured_at,
                    )
                    gap = self.store.observe_gap(
                        capability_key=capability_key,
                        category="observability",
                        summary="Hikari lacks a trustworthy liveness observation for Engineering Worker.",
                        evidence=evidence,
                    )
                    if gap.observation_count >= self.operational_observation_threshold:
                        self.store.ensure_proposal(
                            capability_key,
                            title="补齐 Engineering Worker 存活观测",
                            user_impact=(
                                "Hikari cannot distinguish an idle worker from a missing or dead worker."
                            ),
                            current_workaround="Report worker liveness as unknown.",
                            requested_capability=(
                                "Add a secret-free worker heartbeat checked against freshness and live PID."
                            ),
                            risk_notes="Read-only liveness evidence only; no permission expansion.",
                            authority_boundary="no_new_execution_authority",
                        )
                elif liveness:
                    self.store.resolve_gap(capability_key)

    def _sync_engineering(self) -> None:
        try:
            states = self.engineering_store.list_states()
        except Exception:
            return
        for state in states:
            if state.status not in {"failed", "blocked"} or not state.current_turn_id:
                continue
            try:
                result = self.engineering_store.load_result(
                    state.session_id,
                    state.current_turn_id,
                )
            except Exception:
                continue
            capability_key = (
                "engineering:failure_diagnostics"
                if result.status == "failed"
                else "engineering:block_preflight"
            )
            evidence = GrowthEvidence(
                evidence_id=_stable_id(
                    "evidence",
                    f"engineering:{state.session_id}:{result.turn_id}:{result.status}",
                ),
                kind="engineering_result",
                source_ref=f"engineering:{state.session_id}:{result.turn_id}",
                status=result.status,
                summary=(
                    "A grounded Engineering turn ended in failure."
                    if result.status == "failed"
                    else "A grounded Engineering turn was blocked by a safety or capability boundary."
                ),
                observed_at=datetime.fromtimestamp(
                    result.completed_at or time.time(), timezone.utc
                ).isoformat(),
            )
            if result.status == "failed":
                self.store.observe_gap(
                    capability_key=capability_key,
                    category="engineering_reliability",
                    summary=(
                        "Engineering can fail without a structured failure taxonomy that supports reliable recovery decisions."
                    ),
                    evidence=evidence,
                )
                self.store.ensure_proposal(
                    capability_key,
                    title="增强 Engineering 失败诊断与有界恢复",
                    user_impact=(
                        "A failed engineering task can require manual debugging before Hikari knows whether retrying is useful or safe."
                    ),
                    current_workaround="Return a failure result and require human diagnosis.",
                    requested_capability=(
                        "Add structured Engineering failure reason codes plus bounded recovery guidance."
                    ),
                    risk_notes=(
                        "Do not turn failure recovery into unrestricted retry, write, publish, or authority escalation."
                    ),
                    authority_boundary="existing_turn_authority_only",
                )
            else:
                self.store.observe_gap(
                    capability_key=capability_key,
                    category="engineering_preflight",
                    summary=(
                        "Engineering safety blocks are not yet represented with structured reasons suitable for earlier routing or preflight."
                    ),
                    evidence=evidence,
                )
                self.store.ensure_proposal(
                    capability_key,
                    title="增强 Engineering 阻塞原因结构化与前置检查",
                    user_impact=(
                        "A task may consume an Engineering turn before Hikari can explain which capability or safety boundary blocked it."
                    ),
                    current_workaround="Fail closed after the worker encounters the boundary.",
                    requested_capability=(
                        "Add structured block reason codes and bounded preflight checks before expensive execution."
                    ),
                    risk_notes=(
                        "A detected block must not automatically widen authority or bypass the boundary that caused it."
                    ),
                    authority_boundary="no_automatic_authority_expansion",
                )


def capture_growth_state(
    *,
    operational_state: Mapping[str, object] | None = None,
    state_dir: str | Path | None = None,
) -> dict[str, object]:
    """Synchronize and return Hikari's grounded capability-growth state."""

    if state_dir is None:
        from resident.paths import default_state_dir

        root = default_state_dir()
    else:
        root = Path(state_dir).expanduser().resolve()
    service = GrowthProposalService(
        GrowthStateStore(root / "growth_state.json"),
        EngineeringSessionStore(root / "engineering"),
    )
    try:
        return service.sync(operational_state)
    except Exception:
        return {
            "version": 1,
            "captured_at": _now_iso(),
            "active_gap_count": 0,
            "active_proposal_count": 0,
            "gaps": [],
            "proposals": [],
            "epistemic_rule": (
                "Growth-state synchronization failed. Do not invent capability gaps or proposals from introspection."
            ),
        }
