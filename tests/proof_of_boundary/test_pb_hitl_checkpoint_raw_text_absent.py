"""PB-8: raw prospectus text must not enter either HITL checkpoint."""

from __future__ import annotations

import json

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import ProspectusDisclosureReviewGraph
from src.payload_store import PayloadStore
from src.services.kb_service import KBService

_SENTINEL = "CONFIDENTIAL_RAW_PROSPECTUS_BODY_SENTINEL"


def _serialized(snapshot) -> str:
    return json.dumps(snapshot.values, ensure_ascii=False, default=str)


def _assert_raw_text_absent(snapshot) -> None:
    serialized = _serialized(snapshot)
    assert _SENTINEL not in serialized
    draft = snapshot.values.get("hitl_draft")
    if draft is not None:
        assert _SENTINEL not in json.dumps(draft, ensure_ascii=False, default=str)


def test_pb8_hitl_suspend_and_resume_checkpoints_exclude_raw_payload_text() -> None:
    payload_store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    scope = {"company_id": "company", "issuance_id": "bond"}
    session_id, thread_id = "pb8-session", "pb8-thread"
    graph = ProspectusDisclosureReviewGraph(
        config={
            "payload_store": payload_store,
            "kb_service": KBService(),
            "trusted_scope": scope,
            "session_id": session_id,
            "memory_enabled": True,
            "hitl": {"enabled": True},
        }
    )
    graph.compile(checkpointer=InMemorySaver())
    ref = payload_store.put(f"business {_SENTINEL}", scope=scope, session_id=session_id)
    context = InvocationContext(
        session_id=session_id,
        thread_id=thread_id,
        caller_trust_level=TrustLevel.VERIFIED_EXTERNAL,
        hitl_allowed=True,
    )

    suspended = graph.invoke(
        "{}",
        ctx=context,
        input_context={"mode": "invoke", **scope, "payload_ref": ref, "session_id": session_id},
    )
    assert suspended["status"] == "awaiting_human"

    main = graph._nodes["main"]
    inner = main.get_subgraph()
    assert main._checkpointer is not graph._compiled.checkpointer
    outer_suspended = graph._compiled.get_state(graph._thread_config(thread_id))
    inner_suspended = inner._compiled.get_state(inner._thread_config(thread_id))
    assert outer_suspended and inner_suspended
    _assert_raw_text_absent(outer_suspended)
    _assert_raw_text_absent(inner_suspended)
    assert inner_suspended.values.get("hitl_draft") is not None

    resumed = graph.resume(thread_id=thread_id, feedback={"action": "approve"})
    assert resumed["status"] == "success"
    outer_resumed = graph._compiled.get_state(graph._thread_config(thread_id))
    inner_resumed = inner._compiled.get_state(inner._thread_config(thread_id))
    assert outer_resumed and inner_resumed
    _assert_raw_text_absent(outer_resumed)
    _assert_raw_text_absent(inner_resumed)
