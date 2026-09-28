"""D6 HITL review node with scrub-first resume handling."""

from __future__ import annotations

from typing import ClassVar

from langgraph.types import interrupt
from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.hitl_status import HitlStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.evidence_gate import ProvenanceContract, validate_provenance
from src.nodes.invoke_stage_nodes import _project_draft
from src.services.disclosure_verdict_gate import load_lexicon, scan_and_sanitize_fields
from src.services.pii_sanitizer import sanitize_value


class HitlReviewNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        draft = state.get("hitl_draft") or state.get("diff_draft") or state.get("output") or {}
        if not draft:
            emit_trace_event("hitl_skipped", {"reason": "no draft"}, state)
            return {"status": AgentStatus.SUCCESS.value}
        # Mandatory guard: automated parents must never deadlock on interrupt.
        if not state.get("hitl_allowed", True):
            emit_trace_event("hitl_skipped", {"reason": "parent disallowed"}, state)
            return {"hitl_draft": draft, "status": AgentStatus.SUCCESS.value}
        draft = _gate_hitl_draft(state, draft)
        feedback = interrupt({"draft": draft, "reason": "開示差分レビュー論点の確認"})
        clean_feedback, count, resolvable = sanitize_value(feedback)
        if not resolvable:
            emit_trace_event("hitl_resume_rejected", {}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["HITL feedback PII sanitization failed"]}
        hitl_status = HitlStatus.from_feedback(clean_feedback)
        # The framework routes from hitl_status. Corrected feedback is retained
        # as a sanitized refinement signal; rejected is handled by the graph route.
        refinement = state.get("diff_draft")
        if (
            hitl_status is HitlStatus.CORRECTED
            and isinstance(clean_feedback, dict)
            and isinstance(refinement, dict)
        ):
            questions = list(refinement.get("review_questions", []))
            comment = clean_feedback.get("comment")
            if isinstance(comment, str) and comment:
                questions.append(f"人間レビュー修正: {comment}")
            refinement = {**refinement, "review_questions": questions}
        emit_trace_event(
            "hitl_resumed",
            {"pii_removed": count, "hitl_status": hitl_status.value},
            state,
        )
        delta = {
            "hitl_draft": draft,
            "hitl_feedback": clean_feedback,
            "hitl_status": hitl_status.value,
            "status": AgentStatus.SUCCESS.value,
        }
        if refinement is not None:
            delta["diff_draft"] = refinement
        return delta


def _gate_hitl_draft(state: dict, draft: object) -> dict:
    """Fail closed before an LLM draft crosses the HITL interrupt boundary."""
    projected = _project_draft(draft)
    contract = ProvenanceContract(
        gate_kind="draft",
        cited_ids_key="cited_source_ids",
        retrieved_records_key="retrieved_records",
        evidence_map_key="evidence_map",
        record_id_keys=("source_id",),
    )
    validate_provenance(
        {
            "status": AgentStatus.SUCCESS.value,
            "gate_kind": "draft",
            "cited_source_ids": state.get("cited_source_ids"),
            "retrieved_records": state.get("retrieved_records"),
            "evidence_map": state.get("evidence_map"),
        },
        contract,
    )

    records = state.get("retrieved_records")
    cited = state.get("cited_source_ids")
    if not isinstance(records, list) or not isinstance(cited, list):
        raise SecurityViolationError("HITL draft provenance fields are invalid")
    known_ids = {
        str(record["source_id"])
        for record in records
        if isinstance(record, dict) and isinstance(record.get("source_id"), str) and record["source_id"]
    }
    draft_ids = {
        finding["source_id"]
        for finding in projected["unreflected_facts"]
        if isinstance(finding.get("source_id"), str)
    }
    cited_ids = {source_id for source_id in cited if isinstance(source_id, str)}
    if not draft_ids.issubset(known_ids) or draft_ids != cited_ids:
        raise SecurityViolationError("HITL draft cites unverified source ids")

    _sanitized, _safe, verdict_categories = scan_and_sanitize_fields(projected, load_lexicon())
    if verdict_categories:
        raise SecurityViolationError("HITL draft contains prohibited disclosure verdict text")
    _masked, pii_count, resolvable = sanitize_value(projected)
    if not resolvable or pii_count:
        raise SecurityViolationError("HITL draft contains unsafe personal data")
    emit_trace_event("hitl_draft_gate_passed", {"cited_count": len(cited_ids)}, state)
    return projected
