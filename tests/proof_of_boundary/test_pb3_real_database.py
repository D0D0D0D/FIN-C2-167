"""PB-3: the KB SQL contract is exercised against real sqlite3, not a fake."""

import sqlite3

import pytest

from src.services.kb_service import KBNotFoundError, KBService
from src.nodes.ingest_nodes import normalize_for_retrieval
from src.scoped_retrieval import ScopedRetrievalNode


def test_pb3_multi_id_sql_is_bound_and_tenant_scoped() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    a1 = service.insert_record(
        "company-a:issue-a", "issue-a", {"source_record_id": "a1", "source_kind": "risk_register", "fact_summary": "a"}
    )
    a2 = service.insert_record(
        "company-a:issue-a", "issue-a", {"source_record_id": "a2", "source_kind": "board_material", "fact_summary": "a"}
    )
    b1 = service.insert_record(
        "company-b:issue-b", "issue-b", {"source_record_id": "b1", "source_kind": "risk_register", "fact_summary": "b"}
    )
    assert {item["source_id"] for item in service.search_by_ids("company-a:issue-a", [a1, a2, b1])} == {a1, a2}
    assert service.search_by_ids("", [a1]) == []
    with pytest.raises(KBNotFoundError):
        service.update_reflection("company-a:issue-a", b1, {})


def test_r1_real_sqlite_returns_stored_scope_and_one_scoped_hit() -> None:
    service = KBService(sqlite3.connect(":memory:"))
    service.insert_record(
        "company-a:issue-a",
        "issue-a",
        {"source_record_id": "a1", "source_kind": "risk_register", "fact_summary": "contract risk"},
    )
    scope = {"company_id": "company-a", "issuance_id": "issue-a"}
    backend_hits = service.search(scope=scope, query="contract", top_k=8)
    assert backend_hits[0]["metadata"] | scope == backend_hits[0]["metadata"]
    node = ScopedRetrievalNode(
        scope_keys=("company_id", "issuance_id"),
        backend=service,
        normalize_query=normalize_for_retrieval,
    )
    result = node.execute({**scope, "query": "contract"})
    assert result["status"] == "success"
    assert len(result["retrieved_records"]) == 1
    assert result["retrieved_records"][0]["metadata"]["company_id"] == "company-a"
