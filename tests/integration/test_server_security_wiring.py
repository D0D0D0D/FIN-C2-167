"""Production adapter wiring: operation policy, request scope, and HITL ownership."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server
from src.entry_adapter import AuthContext, AuthSource, Operation


def _middleware_auth(scope: dict[str, str]) -> AuthContext:
    return AuthContext(
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope=scope,
        principal={"caller_id": "middleware-reviewer"},
        auth_source=AuthSource.MIDDLEWARE,
    )


def test_production_mode_mapping_matches_actual_side_effects() -> None:
    assert server._operation_for_mode("invoke") is Operation.READ_INVOKE
    assert server._operation_for_mode("ingest") is Operation.WRITE_INGEST
    assert server._operation_for_mode("feedback") is Operation.WRITE_FEEDBACK


def test_graph_is_constructed_after_auth_with_request_owned_context() -> None:
    one = _middleware_auth({"company_id": "company-a", "issuance_id": "issue-a"})
    two = _middleware_auth({"company_id": "company-b", "issuance_id": "issue-b"})
    graph_one = server._build_agent(one, session_id="session-a")
    graph_two = server._build_agent(two, session_id="session-b")
    assert graph_one is not graph_two
    assert graph_one._nodes["main"]._fin_config["trusted_scope"] == one.scope
    assert graph_two._nodes["main"]._fin_config["trusted_scope"] == two.scope


def test_hitl_requires_nonempty_middleware_principal_and_non_smoke_mode(monkeypatch) -> None:
    scope = {"company_id": "company", "issuance_id": "issue"}
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    assert server._hitl_allowed(_middleware_auth(scope))
    no_principal = AuthContext(
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope=scope,
        principal={"caller_id": ""},
        auth_source=AuthSource.MIDDLEWARE,
    )
    token = AuthContext(
        trust=TrustLevel.VERIFIED_EXTERNAL,
        scope=scope,
        principal={"caller_id": "invoke-token:standalone"},
        auth_source=AuthSource.TOKEN,
    )
    assert not server._hitl_allowed(no_principal)
    assert not server._hitl_allowed(token)
    monkeypatch.setenv("STG_MOCK_MODE", "true")
    assert not server._hitl_allowed(_middleware_auth(scope))


def test_standalone_token_cannot_reach_write_or_resume_endpoints(monkeypatch) -> None:
    monkeypatch.setenv("INVOKE_AUTH_TOKEN", "mock-entry-token")
    monkeypatch.setenv("STG_DEFAULT_COMPANY_ID", "company")
    monkeypatch.setenv("STG_DEFAULT_ISSUANCE_ID", "issue")
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    headers = {"authorization": "Bearer mock-entry-token"}
    with TestClient(server.app, raise_server_exceptions=False) as client:
        read = client.post("/invoke", headers=headers, json={"input": "business"})
        ingest = client.post("/ingest", headers=headers, json={"file_bytes_b64": "YSwK"})
        feedback = client.post(
            "/feedback",
            headers=headers,
            json={"source_id": "source_1", "accepted": True},
        )
        resume = client.post(
            "/resume",
            headers=headers,
            json={
                "thread_id": "thread",
                "session_id": "session",
                "feedback": "approve",
                "company_id": "company",
                "issuance_id": "issue",
            },
        )
    assert read.status_code == 200
    assert read.json()["status"] == "success"
    assert ingest.status_code == feedback.status_code == resume.status_code == 403


@pytest.mark.asyncio
async def test_middleware_hitl_echoes_server_session_and_binds_pending_owner(monkeypatch) -> None:
    scope = {"company_id": "company", "issuance_id": "issue"}
    auth = _middleware_auth(scope)
    monkeypatch.delenv("STG_MOCK_MODE", raising=False)
    monkeypatch.setattr(server, "authenticate", lambda _request, *, operation: auth)
    result = await server._invoke_mode("invoke", object(), "business", "", "owned-thread")
    assert result["status"] == "awaiting_human"
    assert isinstance(result["session_id"], str) and result["session_id"]
    pending = server._pending_hitl.claim(
        "owned-thread",
        scope,
        "middleware-reviewer",
        result["session_id"],
    )
    assert pending.caller_id == "middleware-reviewer"
    assert pending.session_id == result["session_id"]
    server._authorize_resume(pending.graph, "owned-thread", scope, pending.session_id)


def test_pending_hitl_claim_is_single_use_under_two_concurrent_requests() -> None:
    registry = server._PendingHitlRegistry()
    scope = {"company_id": "company", "issuance_id": "issue"}
    graph = object()
    registry.register("thread", graph, scope, "caller", "session")  # type: ignore[arg-type]

    def claim() -> str:
        try:
            return "claimed" if registry.claim("thread", scope, "caller", "session").graph is graph else "wrong"
        except HTTPException as exc:
            return f"http-{exc.status_code}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: claim(), range(2)))
    assert sorted(outcomes) == ["claimed", "http-404"]
    assert not registry.contains("thread")


def test_pending_hitl_rejects_wrong_caller_and_wrong_session_without_consuming_claim() -> None:
    registry = server._PendingHitlRegistry()
    scope = {"company_id": "company", "issuance_id": "issue"}
    registry.register("thread", object(), scope, "caller-a", "session-a")  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as wrong_caller:
        registry.claim("thread", scope, "caller-b", "session-a")
    assert wrong_caller.value.status_code == 403 and registry.contains("thread")
    with pytest.raises(HTTPException) as wrong_session:
        registry.claim("thread", scope, "caller-a", "session-b")
    assert wrong_session.value.status_code == 403 and registry.contains("thread")
    registry.claim("thread", scope, "caller-a", "session-a")
    with pytest.raises(HTTPException) as completed:
        registry.claim("thread", scope, "caller-a", "session-a")
    assert completed.value.status_code == 404


def test_pending_thread_cannot_be_overwritten_by_another_invocation() -> None:
    registry = server._PendingHitlRegistry()
    scope = {"company_id": "company", "issuance_id": "issue"}
    registry.register("thread", object(), scope, "caller", "session")  # type: ignore[arg-type]
    with pytest.raises(HTTPException) as caught:
        registry.register("thread", object(), scope, "caller", "session")  # type: ignore[arg-type]
    assert caught.value.status_code == 409


def test_unknown_mode_is_rejected_before_authentication() -> None:
    with pytest.raises(HTTPException) as caught:
        server._operation_for_mode("delete")
    assert caught.value.status_code == 422
