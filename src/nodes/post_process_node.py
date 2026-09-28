"""Output envelope adaptation and the S-3 disclosure-verdict guard."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.output_envelope import OutputEnvelopeNode
from src.services.disclosure_verdict_gate import load_lexicon, scan_and_sanitize_fields
from src.services.pii_sanitizer import sanitize_value

_PROHIBITED = frozenset(("legal_compliance_verdict", "issuance_approved", "pricing"))


class PostProcessNode(OutputEnvelopeNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    required_fields_by_mode: ClassVar[dict[str, tuple[str, ...]]] = {
        "invoke": (
            "mode",
            "gate_kind",
            "facts",
            "evidence_sources",
            "unreflected_facts",
            "inconsistencies",
            "review_questions",
        ),
        "ingest": ("mode", "gate_kind", "ingest_summary"),
        "feedback": ("mode", "gate_kind", "feedback_summary"),
    }

    def execute(self, state: dict) -> dict:
        # The framework's PB-6 harness deliberately supplies only security
        # bookkeeping. Treat such an incomplete invocation as a normal error
        # envelope rather than allowing an undeclared output path to fail open.
        if not state.get("mode"):
            emit_trace_event("post_process_missing_mode", {}, state)
            return {
                "formatted_output": {
                    "mode": "",
                    "gate_kind": "error",
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["PostProcessNode: mode missing"],
                },
                "status": AgentStatus.ERROR.value,
            }
        return super().execute(state)

    def format_invoke(self, state: dict) -> dict[str, Any]:
        result = state.get("result") if isinstance(state.get("result"), dict) else {}
        return {
            "facts": result.get("facts", []),
            "evidence_sources": result.get("evidence_sources", []),
            "unreflected_facts": result.get("unreflected_facts", []),
            "inconsistencies": result.get("inconsistencies", state.get("inconsistency_flags", [])),
            "review_questions": result.get("review_questions", []),
        }

    def format_ingest(self, state: dict) -> dict[str, Any]:
        return {"ingest_summary": state.get("ingest_summary") or {}}

    def format_feedback(self, state: dict) -> dict[str, Any]:
        return {"feedback_summary": state.get("feedback_summary") or {}}

    def _sanitize_client_echoes(self, formatted: dict[str, Any]) -> dict[str, Any]:
        if _find_prohibited(formatted):
            raise SecurityViolationError("S-3 structural gate: prohibited verdict output field")
        lexicon = load_lexicon()
        verdict_safe, safe, detected = scan_and_sanitize_fields(formatted, lexicon)
        if not safe:
            raise SecurityViolationError("S-3 disclosure-verdict gate: all output text was assertive")
        sanitized, count, resolvable = sanitize_value(verdict_safe)
        if not resolvable:
            raise SecurityViolationError("S-3 PII rescan could not sanitize output")
        emit_trace_event("post_process_s3", {"verdict_categories": detected, "pii_removed": count}, {})
        return sanitized


def _find_prohibited(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _PROHIBITED:
                found.add(key)
            found.update(_find_prohibited(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_find_prohibited(item))
    return found
