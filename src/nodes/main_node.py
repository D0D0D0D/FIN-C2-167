"""Cat2 GraphNode boundary and S-3 provenance gate."""

from __future__ import annotations

from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.cat2_graph_skeleton import DomainWorkflowGraphNode, ModeSpec
from src.evidence_gate import ProvenanceContract, validate_provenance


class DisclosureDiffWorkflowGraphNode(DomainWorkflowGraphNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL
    error_strategy: ClassVar[str] = "handle"
    propagate_hitl: ClassVar[bool] = True
    known_failure_codes: ClassVar[tuple[str, ...]] = (
        "llm_call_failed",
        "llm_output_parse_failed",
        "llm_output_contract_failed",
    )
    mode_specs: ClassVar[dict[str, ModeSpec]] = {
        "ingest": ModeSpec(
            "ingest",
            {
                "ingest_summary": "ingest_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
        "feedback": ModeSpec(
            "feedback",
            {
                "feedback_summary": "feedback_summary",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
        "invoke": ModeSpec(
            "draft",
            {
                "result": "output",
                "evidence_map": "evidence_map",
                "cited_source_ids": "cited_source_ids",
                "retrieved_records": "retrieved_records",
                "inconsistency_flags": "inconsistency_flags",
                "missing_data_flags": "missing_data_flags",
                "requires_human_review": "requires_human_review",
                "error_code": "error_code",
                "error_type": "error_type",
                "error_log": "error_log",
            },
        ),
    }

    def __init__(
        self,
        *,
        llm: Any = None,
        kb_service: Any = None,
        payload_store: Any = None,
        top_k: int = 8,
        min_similarity: float = 0.55,
        trusted_scope: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        self._fin_config = {
            "llm": llm,
            "kb_service": kb_service,
            "payload_store": payload_store,
            "top_k": top_k,
            "min_similarity": min_similarity,
            "trusted_scope": dict(trusted_scope or {}),
            "session_id": session_id,
            "memory_enabled": True,
            "hitl": {"enabled": True},
        }
        super().__init__(config=self._fin_config)

    def _build_subgraph(self):
        from src.graph.domain_workflow_graph import DisclosureDiffWorkflowGraph

        return DisclosureDiffWorkflowGraph(config=self._parent_config())

    def _parent_config(self) -> dict[str, Any]:
        return dict(self._fin_config)

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if not state.get("validated_input"):
            emit_trace_event("main_rejected", {"reason": "missing validated_input"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["main: validated_input missing"]}
        emit_trace_event("main_subgraph_invoke", {"mode": state.get("mode", "")}, state)
        return super().execute(state)

    def _extra_security_gate_output(self, result: dict) -> dict:
        if result.get("status") == AgentStatus.ERROR.value:
            return result
        kind = result.get("gate_kind")
        if kind in ("ingest", "feedback"):
            return result
        if kind != "draft":
            raise SecurityViolationError("S-3 provenance gate: gate_kind is absent or unknown")
        contract = ProvenanceContract(
            gate_kind="draft",
            cited_ids_key="cited_source_ids",
            retrieved_records_key="retrieved_records",
            evidence_map_key="evidence_map",
            record_id_keys=("source_id",),
            passthrough_keys=("result", "degradation_reason", "requires_human_review"),
        )
        validated = validate_provenance(result, contract)
        emit_trace_event("provenance_gate_passed", {"cited_count": len(result.get("cited_source_ids", []))}, result)
        return validated


# Compatibility-free explicit implementation name required by docs/02.
MainNode = DisclosureDiffWorkflowGraphNode
