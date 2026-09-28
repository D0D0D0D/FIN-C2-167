"""Fixtures consumed by ``work/scripts/mrb_live_llm_check.py``."""

from __future__ import annotations

import sqlite3

import pytest

from src.graph.graph import ProspectusDisclosureReviewGraph
from src.payload_store import PayloadStore
from src.services.kb_service import KBService


_LIVE_SCOPE = {"company_id": "company-live", "issuance_id": "bond-live"}
_LIVE_SESSION_ID = "live-1"


def _live_prospectus_draft() -> str:
    """Integration invoke body that crosses the production similarity threshold."""
    return "business risk_factors material_contracts"


@pytest.fixture
def _live_store_kb_and_ref(monkeypatch):
    payload_store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    tenant_id = f"{_LIVE_SCOPE['company_id']}:{_LIVE_SCOPE['issuance_id']}"
    kb.insert_record(
        tenant_id,
        _LIVE_SCOPE["issuance_id"],
        {
            "source_record_id": "r1",
            "source_id": "r1",
            "source_kind": "risk_register",
            "disclosure_section": "material_contracts",
            "fact_summary": "contract change",
            "asserted_attrs": {"review_focus": "material_contracts"},
        },
    )
    ref = payload_store.put(
        _live_prospectus_draft(),
        scope=_LIVE_SCOPE,
        session_id=_LIVE_SESSION_ID,
    )
    original_invoke = ProspectusDisclosureReviewGraph.invoke

    def invoke_with_verified_context(self, user_input, *args, **kwargs):
        kwargs["input_context"] = {
            "mode": "invoke",
            **_LIVE_SCOPE,
            "payload_ref": ref,
            "session_id": _LIVE_SESSION_ID,
        }
        ctx = kwargs.get("ctx")
        if ctx is not None:
            ctx.hitl_allowed = False
        return original_invoke(self, user_input, *args, **kwargs)

    monkeypatch.setattr(ProspectusDisclosureReviewGraph, "invoke", invoke_with_verified_context)
    return payload_store, kb, ref


@pytest.fixture
def agent_config(_live_store_kb_and_ref) -> dict:
    payload_store, kb, _ = _live_store_kb_and_ref
    return {
        "payload_store": payload_store,
        "kb_service": kb,
        "trusted_scope": dict(_LIVE_SCOPE),
        "session_id": _LIVE_SESSION_ID,
        "top_k": 8,
        "memory_enabled": False,
        "hitl": {"enabled": False},
        "ingest": {"max_file_bytes": 10_485_760},
    }


@pytest.fixture
def case_payload(_live_store_kb_and_ref) -> dict:
    _payload_store, _kb, ref = _live_store_kb_and_ref
    return {
        "mode": "invoke",
        **_LIVE_SCOPE,
        "payload_ref": ref,
        "session_id": _LIVE_SESSION_ID,
    }
