"""Flat state schemas for FIN-C2-167.

Raw prospectus and internal-document text deliberately never belongs in these
schemas.  It is held behind the scoped, single-use ``payload_ref`` service.
"""

from typing import NotRequired

from framework.schemas.agent_state import AgentState


class State(AgentState):
    """Outer fixed-backbone state (all values are JSON serializable)."""

    mode: str
    tenant_id: str
    validated_input: str
    payload_ref: str
    gate_kind: str
    result: dict
    evidence_map: list[dict]
    cited_source_ids: list[str]
    retrieved_records: list[dict]
    inconsistency_flags: list[dict]
    missing_data_flags: list[str]
    requires_human_review: bool
    degradation_reason: str
    error_code: NotRequired[str]
    error_type: NotRequired[str]
    ingest_summary: dict
    feedback_summary: dict
    formatted_output: dict


class DisclosureDiffWorkflowState(AgentState):
    """Inner graph schema.

    Route and output methods on the inner graph must use this precise type so
    its mode field is not projected away at the GraphNode boundary.
    """

    mode: str
    tenant_id: str
    company_id: str
    issuance_id: str
    session_id: str
    payload_ref: str
    validated_input: str
    gate_kind: str
    _payload: dict
    _parsed_records: list[dict]
    structured_draft: list[dict]
    structured_query: str
    section_gaps: list[str]
    retrieved_records: list[dict]
    internal_sources: list[dict]
    prior_disclosures: list[dict]
    retrieved_source_ids: list[str]
    diff_draft: dict
    evidence_map: list[dict]
    cited_source_ids: list[str]
    review_brief: dict
    inconsistency_flags: list[dict]
    missing_data_flags: list[str]
    requires_human_review: bool
    degradation_reason: str
    error_code: NotRequired[str]
    error_type: NotRequired[str]
    output: dict
    ingest_summary: dict
    feedback_summary: dict
    _feedback_payload: dict
