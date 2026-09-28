"""Deterministic stage 1/2/4 and injected LLM draft stage for invoke."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any, ClassVar

from framework.errors import SecurityViolationError
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.evidence_gate import FindingField, project_typed_finding, project_typed_findings
from src.payload_store import PayloadStore, PayloadStoreError
from src.scoped_retrieval import ScopedRetrievalNode
from src.services.kb_service import KBService
from src.nodes.ingest_nodes import normalize_for_retrieval

DISCLOSURE_SECTIONS = (
    "business",
    "risk_factors",
    "mdna",
    "financial_statements",
    "material_contracts",
    "litigation",
    "use_of_proceeds",
)

MAX_UNREFLECTED_FACTS = 50
MAX_REVIEW_QUESTIONS = 50
MAX_LLM_TEXT_CHARS = 2_000
MAX_RETRIEVAL_QUERY_CHARS = 4_000

DRAFT_RESPONSE_FIELDS = (
    FindingField("unreflected_facts", list, required=True, item_type=dict),
    FindingField("review_questions", list, required=True, item_type=str),
)
UNREFLECTED_FACT_FIELDS = (
    FindingField("source_id", str, required=True),
    FindingField("candidate", str, required=True),
    FindingField("section", str, allowed_values=DISCLOSURE_SECTIONS),
)


class Stage1DisclosureStructureNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        payload_store: PayloadStore | None = None,
        trusted_scope: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store
        self._trusted_scope = dict(trusted_scope or {})
        self._session_id = session_id

    def execute(self, state: dict) -> dict:
        if self._payload_store is None:
            return _error(
                "Stage1: payload store unavailable",
                state,
                error_code="stage1_payload_store_unavailable",
            )
        try:
            payload = self._payload_store.resolve(
                state.get("payload_ref"),
                scope=self._trusted_scope,
                session_id=self._session_id or "",
                consume=True,
            )
        except PayloadStoreError:
            return _error("Stage1: payload ref unresolvable", state, error_code="stage1_payload_ref_unresolvable")
        text = payload.get("text", "") if isinstance(payload, dict) else str(payload or "")
        if not text and isinstance(payload, dict):
            text = json.dumps(payload, ensure_ascii=False)
        lower = text.casefold()
        facts = [
            {"source_id": f"draft_{index}", "section": section, "text": section}
            for index, section in enumerate(DISCLOSURE_SECTIONS)
            if section.casefold() in lower
        ]
        gaps = [section for section in DISCLOSURE_SECTIONS if section.casefold() not in lower]
        structured_query = _retrieval_query(text)
        # Stage-1 source ids are valid provenance records too, but contain no raw text.
        draft_records = [
            {"source_id": fact["source_id"], "source_kind": "prospectus_draft", "section": fact["section"]}
            for fact in facts
        ]
        emit_trace_event("stage1_disclosure_structure", {"fact_count": len(facts), "gap_count": len(gaps)}, state)
        return {
            "structured_draft": facts,
            "section_gaps": gaps,
            "retrieved_records": draft_records,
            "structured_query": structured_query,
            "status": AgentStatus.SUCCESS.value,
        }


class Stage2ScopedRetrievalNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        kb_service: KBService | None = None,
        top_k: int = 8,
        min_similarity: float = 0.55,
    ) -> None:
        super().__init__()
        self._kb_service = kb_service
        self._top_k = top_k
        self._min_similarity = min_similarity

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if self._kb_service is None:
            emit_trace_event("stage2_degraded", {"reason": "kb unavailable"}, state)
            return {
                "internal_sources": [],
                "prior_disclosures": [],
                "retrieved_records": state.get("retrieved_records", []),
                "status": AgentStatus.SUCCESS.value,
                "degradation_reason": "KB service not configured",
            }
        retrieval = ScopedRetrievalNode(
            scope_keys=("company_id", "issuance_id"),
            backend=_ConfiguredRetrievalBackend(self._kb_service, self._min_similarity),
            normalize_query=normalize_for_retrieval,
            top_k=self._top_k,
        )
        structured_query = state.get("structured_query") or _structured_query_from_draft(
            state.get("structured_draft", [])
        )
        result = retrieval.execute({**state, "query": structured_query})
        records = [*state.get("retrieved_records", []), *result.get("retrieved_records", [])]
        prior = [record for record in records if record.get("source_kind") == "prior_disclosure"]
        internal = [
            record for record in records if record.get("source_kind") not in {"prior_disclosure", "prospectus_draft"}
        ]
        emit_trace_event("stage2_scoped_retrieval", {"record_count": len(records)}, state)
        return {
            **result,
            "internal_sources": internal,
            "prior_disclosures": prior,
            "retrieved_records": records,
            "retrieved_source_ids": [record.get("source_id", "") for record in records],
        }


class _ConfiguredRetrievalBackend:
    """Apply the runtime similarity threshold without changing the part protocol."""

    def __init__(self, service: KBService, min_similarity: float) -> None:
        self._service = service
        self._min_similarity = min_similarity

    def search(self, *, scope: dict[str, str], query: str, top_k: int) -> list[dict[str, Any]]:
        return self._service.search(
            scope=scope,
            query=query,
            top_k=top_k,
            min_similarity=self._min_similarity,
        )


class Stage3DraftNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, llm: Any = None) -> None:
        super().__init__()
        self._llm = llm

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        node = LlmDraftStageNode(
            config={"llm": self._llm},
            hooks=DraftStageHooks(build_prompt=_prompt, build_fallback=_fallback, build_evidence=_evidence),
        )
        result = node.execute(state)
        emit_trace_event("stage3_diff_draft", {"cited_count": len(result.get("cited_source_ids", []))}, state)
        if result.get("status") == AgentStatus.ERROR.value:
            error_code = result.get("error_code") or "llm_draft_failed"
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": error_code,
                "error_type": result.get("error_type") or "LlmDraftStageError",
                "error_log": [f"Stage3DraftNode: {error_code}"],
            }
        try:
            draft = _project_draft(result.get("draft"))
        except SecurityViolationError:
            return {
                "status": AgentStatus.ERROR.value,
                "error_code": "llm_output_contract_failed",
                "error_type": "SecurityViolationError",
                "error_log": ["Stage3DraftNode: llm_output_contract_failed"],
            }
        return {
            "diff_draft": draft,
            "evidence_map": result.get("evidence_map", []),
            "cited_source_ids": result.get("cited_source_ids", []),
            "status": result.get("status"),
            **({"degradation_reason": result["degradation_reason"]} if result.get("degradation_reason") else {}),
        }


class Stage4InconsistencyNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        flags = []
        draft_sections = {item.get("section") for item in state.get("structured_draft", [])}
        for record in state.get("prior_disclosures", []):
            section = record.get("disclosure_section") or record.get("metadata", {}).get("disclosure_section")
            if section and section not in draft_sections:
                flags.append(
                    {
                        "source_id": record.get("source_id"),
                        "section": section,
                        "reason": "prior section absent in draft",
                    }
                )
        draft_attrs = _draft_attrs_by_section(state)
        historical_records = [*state.get("prior_disclosures", []), *state.get("internal_sources", [])]
        for record in historical_records:
            if not isinstance(record, dict):
                continue
            section = record.get("disclosure_section") or record.get("metadata", {}).get("disclosure_section")
            current_attrs = draft_attrs.get(section)
            historical_attrs = _asserted_attrs(record)
            if not section or not current_attrs or not historical_attrs:
                continue
            for key in sorted(set(current_attrs) & set(historical_attrs)):
                prior_value = historical_attrs[key]
                draft_value = current_attrs[key]
                if _normalise_attr_value(prior_value) != _normalise_attr_value(draft_value):
                    flags.append(
                        {
                            "source_id": record.get("source_id"),
                            "section": section,
                            "reason": "attribute value differs across versions",
                            "attr": key,
                            "prior_value": prior_value,
                            "draft_value": draft_value,
                        }
                    )
        draft = state.get("diff_draft") if isinstance(state.get("diff_draft"), dict) else {}
        output = {
            "facts": state.get("structured_draft", []),
            "evidence_sources": _safe_sources(state.get("retrieved_records", [])),
            "unreflected_facts": draft.get("unreflected_facts", []),
            "inconsistencies": flags,
            "review_questions": draft.get("review_questions", []),
        }
        missing = list(state.get("section_gaps", []))
        emit_trace_event("stage4_inconsistency_flags", {"flag_count": len(flags)}, state)
        return {
            "output": output,
            "inconsistency_flags": flags,
            "missing_data_flags": missing,
            "requires_human_review": bool(missing),
            "gate_kind": "draft",
            "status": AgentStatus.SUCCESS.value,
        }


def _prompt(state: dict) -> str:
    # Untrusted material is JSON-delimited data, not instructions.
    return (
        "社内資料と目論見書ドラフトを照合し、法令適合を結論づけず JSON を返してください。\n"
        "RESPONSE_CONTRACT:\n"
        + json.dumps(_response_contract(), ensure_ascii=False, sort_keys=True)
        + "\nUNTRUSTED_DATA:\n"
        + json.dumps(
            {"draft": state.get("structured_draft", []), "records": state.get("retrieved_records", [])},
            ensure_ascii=False,
            default=str,
        )
    )


def _fallback(state: dict) -> dict:
    records = [
        record for record in state.get("retrieved_records", []) if record.get("source_kind") != "prospectus_draft"
    ]
    return {
        "unreflected_facts": [
            {"source_id": record.get("source_id"), "candidate": "社内資料との反映要否を確認"} for record in records
        ],
        "review_questions": ["社内資料に記載された事実が当版ドラフトへ反映されているか確認してください。"],
    }


def _evidence(state: dict, draft: dict) -> tuple[list[dict], list[str]]:
    projected = _project_draft(draft)
    evidence = [
        {
            "claim_id": f"claim_{index}",
            "claim_text": str(item),
            "source_ids": [item.get("source_id")],
            "asserted_attrs": {},
        }
        for index, item in enumerate(projected["unreflected_facts"])
        if item.get("source_id")
    ]
    cited = list(dict.fromkeys(str(item["source_id"]) for item in projected["unreflected_facts"]))
    return evidence, cited


def _project_draft(value: object) -> dict[str, Any]:
    """Validate and project the one-level LLM response before customer output."""
    draft = project_typed_finding(value, DRAFT_RESPONSE_FIELDS)
    findings = project_typed_findings(draft["unreflected_facts"], UNREFLECTED_FACT_FIELDS)
    questions = draft["review_questions"]
    if len(findings) > MAX_UNREFLECTED_FACTS or len(questions) > MAX_REVIEW_QUESTIONS:
        raise SecurityViolationError("S-3 draft projection: response exceeds declared item limits")
    for finding in findings:
        if not _clean_llm_text(finding["source_id"]) or not _clean_llm_text(finding["candidate"]):
            raise SecurityViolationError("S-3 draft projection: required string is empty or oversized")
    if any(not _clean_llm_text(question) for question in questions):
        raise SecurityViolationError("S-3 draft projection: review question is empty or oversized")
    return {"unreflected_facts": findings, "review_questions": list(questions)}


def _response_contract() -> dict[str, Any]:
    """Generate the prompt contract from the same declarations used by validation."""
    contract = _object_contract(DRAFT_RESPONSE_FIELDS)
    finding_contract = _object_contract(UNREFLECTED_FACT_FIELDS)
    contract["properties"]["unreflected_facts"].update(
        {"items": finding_contract, "maxItems": MAX_UNREFLECTED_FACTS}
    )
    contract["properties"]["review_questions"].update(
        {"items": {"type": "string", "maxLength": MAX_LLM_TEXT_CHARS}, "maxItems": MAX_REVIEW_QUESTIONS}
    )
    for name in ("source_id", "candidate"):
        finding_contract["properties"][name]["maxLength"] = MAX_LLM_TEXT_CHARS
    contract["rules"] = [
        "Return exactly the declared keys; do not add fields.",
        "source_id must be copied from UNTRUSTED_DATA.records.",
        "review_questions must be questions, not legal-compliance conclusions.",
    ]
    return contract


def _object_contract(fields: tuple[FindingField, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {field.name: _field_contract(field) for field in fields},
        "required": [field.name for field in fields if field.required],
    }


def _field_contract(field: FindingField) -> dict[str, Any]:
    contract: dict[str, Any] = {"type": _type_label(field.expected_type)}
    if field.item_type is not None:
        contract["items"] = {"type": _type_label(field.item_type)}
    if field.allowed_values:
        contract["enum"] = list(field.allowed_values)
    return contract


def _type_label(value: type | tuple[type, ...]) -> str:
    values = value if isinstance(value, tuple) else (value,)
    names = {str: "string", list: "array", dict: "object", int: "integer", float: "number", bool: "boolean"}
    return "|".join(names.get(item, item.__name__) for item in values)


def _clean_llm_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= MAX_LLM_TEXT_CHARS


def _safe_sources(records: list[dict]) -> list[dict]:
    keys = ("source_id", "source_kind", "disclosure_section", "version", "asserted_attrs")
    return [{key: record[key] for key in keys if key in record} for record in records]


def _structured_query_from_draft(structured_draft: object) -> str:
    if not isinstance(structured_draft, list):
        return ""
    return " ".join(str(item.get("section", "")) for item in structured_draft if isinstance(item, dict)).strip()


def _retrieval_query(text: object) -> str:
    """Return a bounded token projection of the sanitized business text."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).casefold().replace("_", " ")
    tokens = re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)
    projected: list[str] = []
    seen: set[str] = set()
    size = 0
    for token in tokens:
        if token in seen:
            continue
        added = len(token) + bool(projected)
        if size + added > MAX_RETRIEVAL_QUERY_CHARS:
            break
        projected.append(token)
        seen.add(token)
        size += added
    return " ".join(projected)


def _draft_attrs_by_section(state: dict) -> dict[str, dict]:
    """Collect current-draft asserted attributes without using the LLM."""
    by_section: dict[str, dict] = {}
    for record in [*state.get("structured_draft", []), *state.get("retrieved_records", [])]:
        if not isinstance(record, dict):
            continue
        if record.get("source_kind") not in (None, "prospectus_draft") and "section" not in record:
            continue
        section = record.get("section") or record.get("disclosure_section")
        attrs = _asserted_attrs(record)
        if section and attrs:
            by_section[str(section)] = attrs
    return by_section


def _asserted_attrs(record: dict) -> dict:
    attrs = record.get("asserted_attrs")
    return dict(attrs) if isinstance(attrs, dict) else {}


def _normalise_attr_value(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def _error(message: str, state: dict, *, error_code: str = "invoke_stage_failed") -> dict:
    emit_trace_event("invoke_stage_failed", {"error_code": error_code}, state)
    return {
        "status": AgentStatus.ERROR.value,
        "error_code": error_code,
        "error_type": "InvokeStageError",
        "error_log": [f"InvokeStageNode: {error_code}"],
    }
