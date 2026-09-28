import sqlite3

import pytest
from framework.errors import SecurityViolationError

from src.evidence_gate import ProvenanceContract, validate_provenance
from src.nodes.post_process_node import PostProcessNode
from src.nodes.invoke_stage_nodes import Stage4InconsistencyNode
from src.services.disclosure_verdict_gate import load_lexicon, scan_and_sanitize
from src.services.kb_service import KBNotFoundError, KBService
from src.services.pii_sanitizer import sanitize_disclosure_pii


def test_sdk_detected_pii_is_masked_and_japanese_name_is_accepted_gap() -> None:
    text = "John Smith emailed john@example.com and called 090-1234-5678. 山田太郎が確認した。"
    cleaned, count, ok = sanitize_disclosure_pii(text)
    assert ok and count == 3
    assert "John Smith" not in cleaned and "john@example.com" not in cleaned and "090-1234-5678" not in cleaned
    # B-5: Japanese name detection is intentionally limited to SDK support.
    assert "山田太郎" in cleaned


def test_verdict_sentence_is_replaced_and_all_verdict_text_is_unsafe() -> None:
    lexicon = load_lexicon()
    cleaned, safe, _ = scan_and_sanitize("重要な虚偽記載なし。根拠資料を確認する。", lexicon)
    assert safe and "重要な虚偽記載なし" not in cleaned
    _same, safe, _ = scan_and_sanitize("開示は適正。", lexicon)
    assert not safe


def test_output_schema_has_no_verdict_fields_and_requires_five_keys() -> None:
    node = PostProcessNode()
    state = {
        "mode": "invoke",
        "result": {
            "facts": [],
            "evidence_sources": [],
            "unreflected_facts": [],
            "inconsistencies": [],
            "review_questions": [],
        },
    }
    result = node.execute(state)
    assert set(result["formatted_output"]) >= {
        "facts",
        "evidence_sources",
        "unreflected_facts",
        "inconsistencies",
        "review_questions",
    }
    with pytest.raises(SecurityViolationError):
        node._sanitize_client_echoes({"legal_compliance_verdict": "yes"})


def test_provenance_rejects_unknown_and_misattributed_ids() -> None:
    contract = ProvenanceContract(record_id_keys=("source_id",))
    base = {
        "status": "success",
        "gate_kind": "draft",
        "retrieved_records": [{"source_id": "source_1", "kind": "risk"}],
        "evidence_map": [],
        "cited_source_ids": ["source_1"],
    }
    assert validate_provenance(base, contract)["gate_kind"] == "draft"
    with pytest.raises(SecurityViolationError):
        validate_provenance({**base, "cited_source_ids": ["unknown"]}, contract)
    with pytest.raises(SecurityViolationError):
        validate_provenance(
            {
                **base,
                "evidence_map": [
                    {"claim_id": "c", "source_ids": ["source_1"], "asserted_attrs": {"source_1": {"kind": "other"}}}
                ],
            },
            contract,
        )


def test_kb_sql_in_expansion_tenant_isolation_and_fail_closed_update() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    one = service.insert_record(
        "a:i1", "i1", {"source_record_id": "r1", "source_kind": "risk_register", "fact_summary": "risk"}
    )
    two = service.insert_record(
        "b:i2", "i2", {"source_record_id": "r2", "source_kind": "risk_register", "fact_summary": "other"}
    )
    assert [item["source_id"] for item in service.search_by_ids("a:i1", [one, two])] == [one]
    assert service.search(scope={"company_id": "a", "issuance_id": "i2"}, query="risk", top_k=8) == []
    with pytest.raises(KBNotFoundError):
        service.update_reflection("a:i1", two, {})


def _stage4_state(prior_attrs: dict, draft_attrs: dict) -> dict:
    return {
        "structured_draft": [{"source_id": "draft_1", "section": "risk_factors", "asserted_attrs": draft_attrs}],
        "prior_disclosures": [
            {
                "source_id": "prior_1",
                "disclosure_section": "risk_factors",
                "version": "2025",
                "asserted_attrs": prior_attrs,
            }
        ],
        "internal_sources": [],
        "retrieved_records": [],
        "diff_draft": {"unreflected_facts": [], "review_questions": []},
    }


def test_stage4_flags_different_asserted_attribute_values() -> None:
    result = Stage4InconsistencyNode().execute(
        _stage4_state({"contract_term": "12 months"}, {"contract_term": "18 months"})
    )
    assert result["inconsistency_flags"] == [
        {
            "source_id": "prior_1",
            "section": "risk_factors",
            "reason": "attribute value differs across versions",
            "attr": "contract_term",
            "prior_value": "12 months",
            "draft_value": "18 months",
        }
    ]


def test_stage4_does_not_flag_equal_asserted_attribute_values() -> None:
    result = Stage4InconsistencyNode().execute(
        _stage4_state({"contract_term": "１２ months "}, {"contract_term": "12 months"})
    )
    assert result["inconsistency_flags"] == []


def test_stage4_does_not_treat_one_sided_asserted_attribute_as_conflict() -> None:
    result = Stage4InconsistencyNode().execute(_stage4_state({"contract_term": "12 months"}, {"risk_class": "high"}))
    assert result["inconsistency_flags"] == []
