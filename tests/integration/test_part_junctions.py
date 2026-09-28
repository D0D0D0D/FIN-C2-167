"""Layer-2 part junction tests for FIN-C2-167 (TESTING.md)."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from typing import get_type_hints

import openpyxl
import pytest
import yaml
from framework.errors import SecurityViolationError
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel

from src.evidence_gate import (
    ProvenanceContract,
    StrictCoveragePolicy,
    find_uncited_numeric_or_comparative_claims,
    validate_provenance,
)
from src.ingest_file_sanitizer import sanitize_file_bytes
from src.ingest_row_pipeline import CheckpointScrubPolicy, RowPipelineHooks, scrub_checkpoint_envelope
from src.llm_draft_stage import DraftStageHooks, LlmDraftStageNode
from src.nodes.ingest_nodes import DisclosureIngestPipeline, normalize_for_retrieval
from src.nodes.invoke_stage_nodes import Stage2ScopedRetrievalNode
from src.nodes.main_node import DisclosureDiffWorkflowGraphNode
from src.nodes.post_process_node import PostProcessNode
from src.nodes.pre_process_node import PreProcessNode
from src.payload_store import PayloadStore
from src.scoped_retrieval import ScopedRetrievalNode, normalize_retrieved_record
from src.schemas.state import DisclosureDiffWorkflowState
from src.services.kb_service import KBService
from src.services.pii_sanitizer import sanitize_disclosure_pii


def test_g1_mode_specs_merge_only_active_mode_fields() -> None:
    node = DisclosureDiffWorkflowGraphNode()
    merged = node.merge_output(
        {"mode": "ingest"},
        {
            "status": "success",
            "ingest_summary": {"written_count": 1},
            "output": {"not": "promoted"},
            "node_history": ["x"],
        },
    )
    assert merged == {"gate_kind": "ingest", "status": "success", "ingest_summary": {"written_count": 1}}


def test_g2_inner_route_uses_disclosure_workflow_state_annotation() -> None:
    from src.graph.domain_workflow_graph import DisclosureDiffWorkflowGraph

    assert get_type_hints(DisclosureDiffWorkflowGraph.route)["state"] is DisclosureDiffWorkflowState
    graph = DisclosureDiffWorkflowGraph(config={})
    assert graph.route_mode({"mode": "invoke", "status": "success"}) == "invoke"
    assert graph.route_mode({"mode": "invoke", "status": "error"}) == "error"
    # routing は hitl_status が設定され **かつ** hitl_feedback が非 None の
    # ときだけ発火する。feedback を伴わない stale な hitl_status は通常の routing を上書きしない。
    approved = {"status": "success", "hitl_status": "approved", "hitl_feedback": {"action": "approve"}}
    rejected = {"status": "success", "hitl_status": "rejected", "hitl_feedback": {"action": "reject"}}
    assert graph.route_after_review(approved) == "proceed"
    assert graph.route_after_review(rejected) == "stop"
    # stale（feedback 無し）は停止させない
    assert graph.route_after_review({"status": "success", "hitl_status": "rejected"}) != "stop"


def test_e1_envelope_missing_required_fields_blocks_and_complete_passes() -> None:
    node = PostProcessNode()
    with pytest.raises(SecurityViolationError):
        node._extra_security_gate_output({"formatted_output": {"mode": "invoke", "gate_kind": "draft"}})
    complete = {
        "mode": "invoke",
        "gate_kind": "draft",
        "facts": [],
        "evidence_sources": [],
        "unreflected_facts": [],
        "inconsistencies": [],
        "review_questions": [],
    }
    assert node._extra_security_gate_output({"formatted_output": complete})["formatted_output"]["facts"] == []


def test_tc04_tc06_context_from_state_and_final_s2_gate_sanitize_payload() -> None:
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    scope = {"company_id": "company", "issuance_id": "issue"}
    ref = store.put("John Smith reviewed john@example.com", scope=scope, session_id="session")
    state = {
        "correlation_id": "correlation",
        "trace_id": "trace",
        "session_id": "session",
        "thread_id": "thread",
        "caller_trust_level": TrustLevel.VERIFIED_EXTERNAL.value,
        "input_context": {"mode": "invoke", **scope, "payload_ref": ref, "session_id": "session"},
    }
    node = PreProcessNode(payload_store=store, trusted_scope=scope, session_id="session")
    gated = node._security_gate_input(state)
    sanitized = store.resolve(gated["payload_ref"], scope=scope, session_id="session")
    assert "John Smith" not in sanitized and "john@example.com" not in sanitized
    result = node.execute(gated)
    assert result["tenant_id"] == "company:issue"


def test_tc07_final_s3_gate_rejects_missing_output_contract() -> None:
    with pytest.raises(SecurityViolationError):
        PostProcessNode()._security_gate_output({"formatted_output": {"mode": "invoke", "gate_kind": "draft"}})


def test_e2_manifest_config_drives_disclosure_s3_pass_and_block() -> None:
    manifest = yaml.safe_load(open("config/agent.yaml", encoding="utf-8"))
    assert manifest["hitl"]["enabled"] is True
    node = PostProcessNode()
    safe = {
        "mode": "invoke",
        "gate_kind": "draft",
        "facts": [],
        "evidence_sources": [],
        "unreflected_facts": [],
        "inconsistencies": [],
        "review_questions": ["根拠を確認してください。"],
    }
    assert node._extra_security_gate_output({"formatted_output": safe})["formatted_output"]["review_questions"]
    unsafe = {**safe, "review_questions": ["開示は適正。"]}
    with pytest.raises(SecurityViolationError):
        node._extra_security_gate_output({"formatted_output": unsafe})


def test_i1_sdk_pii_sanitizer_masks_sdk_findings_and_preserves_japanese_name_gap() -> None:
    clean, _count, ok = sanitize_disclosure_pii(
        "John Smith reviewed the 重要契約 disclosure section; email john@example.com. 田中太郎が承認した。"
    )
    assert ok and "John Smith" not in clean and "john@example.com" not in clean
    assert "重要契約" in clean and "disclosure section" in clean
    # B-5 accepted gap: SDK detect_pii does not identify this Japanese name.
    assert "田中太郎" in clean


def test_i2_normalization_is_shared_by_write_and_retrieval() -> None:
    value = "  Material   Contract\nChange "
    assert normalize_for_retrieval(value) == "material contract change"
    service = KBService(sqlite3.connect(":memory:"))
    service.insert_record(
        "company:issue",
        "issue",
        {"source_record_id": "row", "source_kind": "material_contract", "fact_summary": normalize_for_retrieval(value)},
    )
    node = ScopedRetrievalNode(
        scope_keys=("company_id", "issuance_id"), backend=service, normalize_query=normalize_for_retrieval
    )
    result = node.execute({"company_id": "company", "issuance_id": "issue", "query": value})
    assert result["status"] == "success" and result["retrieved_records"]


def test_h5_query_ranks_related_fact_summary_first_across_multiple_records() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    records = (
        {
            "source_record_id": "unrelated",
            "source_id": "a_unrelated",
            "source_kind": "board_material",
            "disclosure_section": "business",
            "fact_summary": "office relocation timetable",
        },
        {
            "source_record_id": "related",
            "source_id": "z_related",
            "source_kind": "risk_register",
            "disclosure_section": "risk_factors",
            "fact_summary": "liquidity outlook deteriorated",
        },
    )
    for record in records:
        service.insert_record("company:issue", "issue", record)

    result = Stage2ScopedRetrievalNode(service, top_k=2, min_similarity=0.0).execute(
        {
            "company_id": "company",
            "issuance_id": "issue",
            "structured_query": "liquidity deteriorated",
            "retrieved_records": [],
        }
    )
    hits = result["retrieved_records"]

    assert [hit["source_id"] for hit in hits] == ["z_related", "a_unrelated"]
    assert hits[0]["fact_summary"] == "liquidity outlook deteriorated"
    assert hits[0]["score"] > hits[1]["score"]


def test_m1_min_similarity_filters_unrelated_records() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    service.insert_record(
        "company:issue",
        "issue",
        {
            "source_record_id": "related",
            "source_id": "related",
            "source_kind": "risk_register",
            "fact_summary": "liquidity outlook deteriorated",
        },
    )
    service.insert_record(
        "company:issue",
        "issue",
        {
            "source_record_id": "unrelated",
            "source_id": "unrelated",
            "source_kind": "board_material",
            "fact_summary": "office relocation timetable",
        },
    )

    hits = service.search(
        scope={"company_id": "company", "issuance_id": "issue"},
        query="liquidity deteriorated",
        top_k=8,
        min_similarity=0.55,
    )

    assert [hit["source_id"] for hit in hits] == ["related"]


def test_i3_required_fields_reject_with_reason() -> None:
    node = DisclosureIngestPipeline(hooks=None)
    result = node.execute({"rows": [{"source_record_id": "row"}]})
    rejected = result["ingest_summary"]["rejected"]
    assert rejected[0]["reasons"] == ["missing_source_kind", "missing_fact_summary"]


def test_i5_checkpoint_sentinel_absent_from_scrubbed_envelopes_for_all_modes() -> None:
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    scope = {"company_id": "company", "issuance_id": "issue"}
    sentinel = "CONFIDENTIAL_RAW_PROSPECTUS_SENTINEL"
    policy = CheckpointScrubPolicy(scope_keys=frozenset(scope))
    for mode in ("ingest", "invoke", "feedback"):
        envelope = scrub_checkpoint_envelope(
            json.dumps({"mode": mode, "payload": {"body": sentinel}}),
            store,
            session_id=f"session-{mode}",
            trusted_scope=scope,
            policy=policy,
        )
        assert sentinel not in envelope
        assert json.loads(envelope)["payload_ref"]


def test_i6_forged_payload_ref_is_reissued_and_unknown_ref_fails_closed() -> None:
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    scope = {"company_id": "company", "issuance_id": "issue"}
    envelope = json.loads(
        scrub_checkpoint_envelope(
            json.dumps({"mode": "ingest", "payload_ref": "forged", "payload": {"body": "fact"}}),
            store,
            session_id="session",
            trusted_scope=scope,
            policy=CheckpointScrubPolicy(scope_keys=frozenset(scope)),
        )
    )
    assert envelope["payload_ref"] != "forged"
    node = ScopedRetrievalNode(
        scope_keys=("company_id", "issuance_id"), backend=None, normalize_query=normalize_for_retrieval
    )
    result = node.execute({"company_id": "company", "query": "x"})
    assert result["status"] == AgentStatus.ERROR.value
    unknown = PreProcessNode(payload_store=store)._extra_security_gate_input(
        {
            "session_id": "session",
            "input_context": {"mode": "invoke", **scope, "payload_ref": "unknown", "session_id": "session"},
        }
    )
    assert unknown["status"] == AgentStatus.ERROR.value


def test_i7_i8_real_payload_store_resolves_only_constructor_injected_context() -> None:
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    scope = {"company_id": "company", "issuance_id": "issue"}
    rows = [{"source_record_id": "row", "source_kind": "risk_register", "fact_summary": "risk"}]
    ref = store.put(rows, scope=scope, session_id="session")
    written: list[dict] = []
    hooks = RowPipelineHooks(
        sanitize=lambda _state, row: row,
        normalize=lambda _state, row: row,
        write=lambda _state, row: written.append(row) or "row",
    )
    node = DisclosureIngestPipeline(
        payload_store=store,
        trusted_scope=scope,
        session_id="session",
        hooks=hooks,
    )
    result = node.execute(
        {
            "payload_ref": ref,
            "company_id": "forged-company",
            "issuance_id": "forged-issue",
            "session_id": "forged-session",
        }
    )
    assert result["ingest_summary"]["written_count"] == 1 and written == rows

    second = store.put(rows, scope=scope, session_id="session-2")
    unbound = DisclosureIngestPipeline(payload_store=store, hooks=hooks)
    with pytest.raises(ValueError, match="trusted_scope must be passed explicitly"):
        unbound.execute({"payload_ref": second, **scope, "session_id": "session-2"})


def test_f1_csv_xlsx_and_docx_valid_payloads_pass_unchanged() -> None:
    csv_result = sanitize_file_bytes(b"source_record_id,source_kind,fact_summary\nr1,risk_register,contract change\n")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["source_record_id", "source_kind", "fact_summary"])
    sheet.append(["r2", "board_material", "board update"])
    xlsx = io.BytesIO()
    workbook.save(xlsx)
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>board memo</w:t></w:r></w:p></w:body></w:document>',
        )
    assert csv_result.accepted and csv_result.sanitized_rows[0]["fact_summary"] == "contract change"
    assert sanitize_file_bytes(xlsx.getvalue()).accepted
    assert sanitize_file_bytes(docx.getvalue()).accepted


def test_f2_quality_gate_boundary_rejects_replacement_character_payload() -> None:
    accepted = sanitize_file_bytes(b"a,b\nnormal,value\n")
    rejected = sanitize_file_bytes("a,b\n\ufffd,value\n".encode())
    assert accepted.accepted
    assert not rejected.accepted and "text quality rejected" in (rejected.reject_reason or "")


def test_f3_file_rejection_has_explicit_reason_and_row_summary_keeps_reasons() -> None:
    rejected = sanitize_file_bytes(b"%PDF-1.7 forbidden")
    row_result = DisclosureIngestPipeline(hooks=None).execute({"rows": [{}]})
    assert rejected.rejected[0]["reason"] == rejected.reject_reason
    assert row_result["ingest_summary"]["rejected"][0]["reasons"]


def test_r2_scope_missing_fails_closed_and_tenant_b_cannot_read_a() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    service.insert_record(
        "a:issue", "issue", {"source_record_id": "a", "source_kind": "risk_register", "fact_summary": "a"}
    )
    node = ScopedRetrievalNode(
        scope_keys=("company_id", "issuance_id"), backend=service, normalize_query=normalize_for_retrieval
    )
    assert node.execute({"company_id": "a", "query": "a"})["status"] == "error"
    assert node.execute({"company_id": "b", "issuance_id": "issue", "query": "a"})["retrieved_records"] == []


def test_r3_projection_keeps_allowlisted_fin_fields_and_drops_free_text() -> None:
    record = normalize_retrieved_record(
        {
            "source_id": "source_1",
            "source_kind": "risk_register",
            "disclosure_section": "risk_factors",
            "description": "raw body",
            "metadata": {
                "source_id": "source_1",
                "description": "raw body",
                "company_id": "c",
                "issuance_id": "i",
            },
        },
        expected_scope={"company_id": "c", "issuance_id": "i"},
    )
    assert record is not None and record["source_kind"] == "risk_register"
    assert "description" not in record and "description" not in record["metadata"]


def test_r4_empty_hit_degrades_without_error() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    node = ScopedRetrievalNode(
        scope_keys=("company_id", "issuance_id"), backend=service, normalize_query=normalize_for_retrieval
    )
    result = node.execute({"company_id": "c", "issuance_id": "i", "query": "none"})
    assert result["status"] == "success" and result["degradation_reason"] == "no scoped retrieval hits"


def test_v1_complete_evidence_passes_and_missing_fields_block() -> None:
    contract = ProvenanceContract(record_id_keys=("source_id",))
    complete = {
        "status": "success",
        "gate_kind": "draft",
        "cited_source_ids": ["source_1"],
        "retrieved_records": [{"source_id": "source_1", "risk_class": "high"}],
        "evidence_map": [
            {
                "claim_id": "c",
                "claim_text": "risk",
                "source_ids": ["source_1"],
                "asserted_attrs": {"source_1": {"risk_class": "high"}},
            }
        ],
    }
    assert validate_provenance(complete, contract)["gate_kind"] == "draft"
    with pytest.raises(SecurityViolationError):
        validate_provenance({key: value for key, value in complete.items() if key != "evidence_map"}, contract)


def test_v2_v3_strict_paths_cover_all_fin_llm_fields_and_flag_uncited_number() -> None:
    policy = StrictCoveragePolicy(
        free_text_paths=(("draft", "unreflected_facts"), ("draft", "inconsistencies"), ("draft", "review_questions"))
    )
    payload = {"draft": {"unreflected_facts": "売上が10%増加", "inconsistencies": "", "review_questions": ""}}
    flags = find_uncited_numeric_or_comparative_claims(payload, [], policy=policy)
    assert flags and flags[0]["path"] == "draft.unreflected_facts"


class _NonJsonLlm:
    def complete(self, _messages):
        return {"content": "not json"}


def test_l1_l2_fallback_is_provenance_complete_and_non_json_fails_closed() -> None:
    hooks = DraftStageHooks(
        build_prompt=lambda _state: "prompt",
        build_fallback=lambda _state: {"unreflected_facts": [{"source_id": "source_1"}]},
        build_evidence=lambda _state, _draft: (
            [{"claim_id": "c", "claim_text": "fact", "source_ids": ["source_1"], "asserted_attrs": {}}],
            ["source_1"],
        ),
    )
    state = {"retrieved_records": [{"source_id": "source_1"}]}
    fallback = LlmDraftStageNode(config={}, hooks=hooks).execute(state)
    gated = validate_provenance(
        {
            "status": "success",
            "gate_kind": "draft",
            "retrieved_records": state["retrieved_records"],
            "evidence_map": fallback["evidence_map"],
            "cited_source_ids": fallback["cited_source_ids"],
        },
        ProvenanceContract(record_id_keys=("source_id",)),
    )
    assert gated["gate_kind"] == "draft"
    assert LlmDraftStageNode(config={"llm": _NonJsonLlm()}, hooks=hooks).execute(state)["status"] == "error"
