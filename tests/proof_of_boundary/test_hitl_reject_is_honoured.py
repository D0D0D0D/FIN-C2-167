"""HITL の reject が実際に出力を止めることを固定する。

由来: 2026-08-13 の独立レビュー（Batch 2 二巡目）。レビュアーの再現 probe をそのまま
回帰テストにしている（実装者が書くと実装の写しになるため）。

欠陥: resume 後に feedback を sanitize するが `HitlStatus.from_feedback()` を呼ばず
`hitl_status` も返していなかった。route は `hitl_status == "rejected"` のときだけ止まるので、
**approve / reject どちらも `status=success, has_output=True, hitl_status=None`** になっていた。
"""
from __future__ import annotations

import sqlite3

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import ProspectusDisclosureReviewGraph
from src.payload_store import PayloadStore
from src.services.kb_service import KBService

SCOPE = {"company_id": "company", "issuance_id": "bond"}


def _run(action: str) -> tuple[dict, dict]:
    payloads = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    graph = ProspectusDisclosureReviewGraph(config={
        "payload_store": payloads, "kb_service": kb,
        "trusted_scope": SCOPE, "session_id": action,
        "memory_enabled": True, "hitl": {"enabled": True},
    })
    graph.compile(checkpointer=InMemorySaver())
    ref = payloads.put("business", scope=SCOPE, session_id=action)
    graph.invoke(
        "{}",
        ctx=InvocationContext(session_id=action, thread_id=action,
                              caller_trust_level=TrustLevel.VERIFIED_EXTERNAL, hitl_allowed=True),
        input_context={"mode": "invoke", **SCOPE, "payload_ref": ref, "session_id": action},
    )
    resumed = graph.resume(thread_id=action, feedback={"action": action})
    snapshot = graph._compiled.get_state(graph._thread_config(action)).values
    return resumed, snapshot


def test_reject_stops_the_output():
    """reject は出力を公開しないこと。"""
    resumed, snapshot = _run("reject")
    assert snapshot.get("hitl_status") == "rejected", (
        f"reject が hitl_status に反映されていない: {snapshot.get('hitl_status')!r}"
    )
    assert not resumed.get("output"), "reject なのに出力が公開された"
    assert "PostProcessNode" not in (snapshot.get("node_history") or []), (
        "reject なのに post_process へ進んでいる"
    )


def test_approve_publishes_the_output():
    """approve は従来どおり出力を公開すること（3 値が同じ処理にならないことの対）。"""
    resumed, snapshot = _run("approve")
    assert snapshot.get("hitl_status") == "approved"
    assert resumed.get("output"), "approve なのに出力が無い"


def test_approve_and_reject_do_not_produce_the_same_result():
    """分岐だけ作って同じ処理になっていないこと。"""
    approved, _ = _run("approve")
    rejected, _ = _run("reject")
    assert bool(approved.get("output")) != bool(rejected.get("output")), (
        "approve と reject が同じ結果になっている"
    )
