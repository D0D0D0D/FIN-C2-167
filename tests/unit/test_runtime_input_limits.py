"""Runtime-configured ingest limits are enforced at the S-2 payload boundary."""

from src.nodes.pre_process_node import PreProcessNode
from src.payload_store import PayloadStore


def _gate(raw):
    scope = {"company_id": "company", "issuance_id": "issue"}
    session_id = "session"
    store = PayloadStore(scope_keys=tuple(scope))
    ref = store.put(raw, scope=scope, session_id=session_id)
    node = PreProcessNode(
        payload_store=store,
        max_batch_records=2,
        max_free_text_chars=12,
        trusted_scope=scope,
        session_id=session_id,
    )
    result = node._extra_security_gate_input(
        {
            "session_id": session_id,
            "input_context": {"payload_ref": ref, **scope, "session_id": session_id},
        }
    )
    return result


def test_max_batch_records_rejects_one_over_boundary() -> None:
    accepted = b"source_record_id,source_kind,fact_summary\nr1,risk,a\nr2,risk,b\n"
    rejected = accepted + b"r3,risk,c\n"

    assert _gate(accepted).get("status") != "error"
    result = _gate(rejected)
    assert result["status"] == "error"
    assert result["error_log"] == ["PreProcessNode S-2: batch record limit exceeded"]


def test_max_free_text_chars_rejects_one_over_boundary() -> None:
    assert _gate("x" * 12).get("status") != "error"
    result = _gate("x" * 13)
    assert result["status"] == "error"
    assert result["error_log"] == ["PreProcessNode S-2: free-text limit exceeded"]
