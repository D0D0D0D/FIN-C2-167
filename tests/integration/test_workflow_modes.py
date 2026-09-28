import json
import sqlite3

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import ProspectusDisclosureReviewGraph
from src.payload_store import PayloadStore
from src.services.kb_service import KBService


def _graph(payloads, kb, *, session, llm=None):
    scope = {"company_id": "company", "issuance_id": "bond"}
    graph = ProspectusDisclosureReviewGraph(
        config={
            "llm": llm,
            "payload_store": payloads,
            "kb_service": kb,
            "trusted_scope": scope,
            "session_id": session,
            "memory_enabled": True,
            "hitl": {"enabled": True},
        }
    )
    graph.compile(checkpointer=InMemorySaver())
    return graph


def _invoke(graph, payloads, *, mode, body, session="s", thread="t", allowed=False):
    scope = {"company_id": "company", "issuance_id": "bond"}
    ref = payloads.put(body, scope=scope, session_id=session)
    ctx = InvocationContext(
        session_id=session, thread_id=thread, caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=allowed
    )
    return graph.invoke("{}", ctx=ctx, input_context={"mode": mode, **scope, "payload_ref": ref, "session_id": session})


def test_ingest_then_invoke_is_tenant_scoped_and_returns_five_key_contract() -> None:
    payloads = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    graph = _graph(payloads, kb, session="ingest")
    csv = b"source_record_id,source_kind,fact_summary,disclosure_section\nr1,risk_register,contract change,material_contracts\n"
    ingest = _invoke(graph, payloads, mode="ingest", body=csv, session="ingest", thread="ingest", allowed=False)
    assert ingest["status"] == "success"
    graph = _graph(payloads, kb, session="invoke")
    result = _invoke(
        graph,
        payloads,
        mode="invoke",
        body="business risk_factors material_contracts",
        session="invoke",
        thread="invoke",
        allowed=False,
    )
    assert result["status"] == "success"
    assert set(result["output"]) >= {
        "facts",
        "evidence_sources",
        "unreflected_facts",
        "inconsistencies",
        "review_questions",
    }
    assert any(source.get("source_kind") == "risk_register" for source in result["output"]["evidence_sources"])


def test_hitl_pause_then_resume_reaches_success() -> None:
    payloads = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    graph = _graph(payloads, kb, session="hitl")
    paused = _invoke(graph, payloads, mode="invoke", body="business", session="hitl", thread="hitl", allowed=True)
    assert paused["status"] == "awaiting_human"
    main = graph._nodes["main"]
    inner = main.get_subgraph()
    assert main._checkpointer is not graph._compiled.checkpointer
    assert inner is main.get_subgraph()
    resumed = graph.resume(thread_id="hitl", feedback={"action": "correct", "comment": "John Smith confirmed"})
    assert resumed["status"] == "success"
    assert "John Smith" not in json.dumps(resumed["output"], ensure_ascii=False)
    snapshot = graph._compiled.get_state(graph._thread_config("hitl"))
    assert not snapshot.next


class _ExplodingLlm:
    def complete(self, _messages):
        raise RuntimeError("provider-secret-marker")


def test_graph_invoke_surfaces_llm_error_code_without_provider_text() -> None:
    payloads = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    graph = _graph(payloads, kb, session="llm-error", llm=_ExplodingLlm())

    result = _invoke(
        graph,
        payloads,
        mode="invoke",
        body="business risk_factors material_contracts",
        session="llm-error",
        thread="llm-error",
        allowed=False,
    )

    assert result["status"] == "error"
    assert result["error_code"] == "llm_call_failed"
    assert "provider-secret-marker" not in json.dumps(result, ensure_ascii=False, default=str)
