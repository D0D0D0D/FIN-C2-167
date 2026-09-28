"""HTTP adapter: authenticated fixed-mode entry points and HITL resume."""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from src.entry_adapter import AuthContext, AuthSource, Operation, authenticate
from src.graph.graph import ProspectusDisclosureReviewGraph
from src.ingest_row_pipeline import CheckpointScrubPolicy, scrub_checkpoint_envelope
from src.payload_store import PayloadStore
from src.services.kb_service import KBService

app = FastAPI(title="FIN-C2-167 Corporate Bond Prospectus Disclosure-Control Diff Review Agent")


def _runtime_config() -> dict[str, Any]:
    try:
        with Path("config/config.yaml").open(encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        return {}


_cfg = _runtime_config()
_payload_store = PayloadStore(
    scope_keys=("company_id", "issuance_id"), ttl_seconds=_cfg.get("payload_store", {}).get("ttl_seconds", 300)
)
_CHECKPOINT_SCRUB_POLICY = CheckpointScrubPolicy(
    safe_keys=frozenset({"mode"}),
    scope_keys=frozenset({"company_id", "issuance_id"}),
    enum_value_keys={"mode": frozenset({"ingest", "invoke", "feedback"})},
)


def wire_services() -> KBService:
    """Server-startup seam; deployments wire their DB/LLM implementations here."""
    return KBService(db=None)


_kb_service = wire_services()
_secrets_provider = secrets_factory(namespace="fin-c2-167", agent_name="ProspectusDisclosureReviewGraph")


@dataclass(frozen=True)
class _PendingInvocation:
    graph: ProspectusDisclosureReviewGraph
    scope: dict[str, str]
    caller_id: str
    session_id: str


class _PendingHitlRegistry:
    """Atomically own only currently suspended request-scoped graph instances."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: dict[str, _PendingInvocation] = {}

    def register(
        self,
        thread_id: str,
        graph: ProspectusDisclosureReviewGraph,
        scope: dict[str, str],
        caller_id: str,
        session_id: str,
    ) -> None:
        with self._lock:
            if thread_id in self._items:
                raise HTTPException(status_code=409, detail="HITL thread is already pending")
            self._items[thread_id] = _PendingInvocation(
                graph=graph,
                scope=dict(scope),
                caller_id=caller_id,
                session_id=session_id,
            )

    def claim(
        self,
        thread_id: str,
        scope: dict[str, str],
        caller_id: str,
        session_id: str,
    ) -> _PendingInvocation:
        with self._lock:
            pending = self._items.get(thread_id)
            if pending is None:
                raise HTTPException(status_code=404, detail="No pending HITL session found")
            if (
                pending.scope != scope
                or pending.caller_id != caller_id
                or pending.session_id != session_id
            ):
                raise HTTPException(status_code=403, detail="Caller does not own this HITL session")
            return self._items.pop(thread_id)

    def contains(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._items


_pending_hitl = _PendingHitlRegistry()


class InvokeRequest(BaseModel):
    input: str = ""
    file_bytes_b64: str | None = None
    session_id: str = ""
    thread_id: str = ""


class IngestRequest(InvokeRequest):
    pass


class FeedbackRequest(BaseModel):
    source_id: str
    accepted: bool
    final_reason: str = ""
    learnable: bool = False
    session_id: str = ""
    thread_id: str = ""


class ResumeRequest(BaseModel):
    thread_id: str
    session_id: str
    feedback: str | dict
    company_id: str
    issuance_id: str


def _stg_smoke_mode() -> bool:
    return os.environ.get("STG_MOCK_MODE", "").casefold() == "true"


def _clean_scope(scope: dict[str, str]) -> None:
    if any(":" in value for value in scope.values()):
        raise HTTPException(status_code=403, detail="company_id/issuance_id must not contain ':'.")


def _context(auth: AuthContext, *, session_id: str, thread_id: str, hitl_allowed: bool) -> InvocationContext:
    return InvocationContext(
        session_id=session_id,
        thread_id=thread_id,
        caller_trust_level=auth.trust,
        caller_id=auth.principal["caller_id"],
        hitl_allowed=hitl_allowed,
    )


def _hitl_allowed(auth: AuthContext) -> bool:
    """Only a resumable middleware principal may open a HITL thread."""
    return (
        auth.auth_source is AuthSource.MIDDLEWARE
        and bool(auth.principal.get("caller_id"))
        and not _stg_smoke_mode()
    )


def _operation_for_mode(mode: str) -> Operation:
    try:
        return {
            "invoke": Operation.READ_INVOKE,
            "ingest": Operation.WRITE_INGEST,
            "feedback": Operation.WRITE_FEEDBACK,
        }[mode]
    except KeyError as exc:
        raise HTTPException(status_code=422, detail="Unsupported mode") from exc


def _build_agent(auth: AuthContext, *, session_id: str) -> ProspectusDisclosureReviewGraph:
    """Build nodes after authentication so trusted scope is request-owned."""
    graph = ProspectusDisclosureReviewGraph(
        config={
            "llm": getattr(app.state, "llm", None),
            "kb_service": _kb_service,
            "payload_store": _payload_store,
            "trusted_scope": auth.scope,
            "session_id": session_id,
            "ingest": _cfg.get("ingest", {}),
            "top_k": _cfg.get("retrieval", {}).get("top_k_internal_sources", 8),
            "min_similarity": _cfg.get("retrieval", {}).get("min_similarity", 0.55),
            "memory_enabled": True,
            "hitl": _cfg.get("hitl", {"enabled": True}),
        }
    )
    graph.compile(checkpointer=InMemorySaver())
    graph.provision_secrets(_secrets_provider)
    return graph


async def _invoke_mode(mode: str, request: Request, raw_payload: Any, session_id: str, thread_id: str):
    auth = authenticate(request, operation=_operation_for_mode(mode))
    # entry-adapter owns authentication and accepts only middleware-stamped
    # company/issuance values; no client header/body value is read.
    scope = dict(auth.scope)
    _clean_scope(scope)
    actual_session = session_id or str(uuid4())
    actual_thread = thread_id or actual_session
    graph = _build_agent(auth, session_id=actual_session)
    if isinstance(raw_payload, str):
        # The scrubber retains the raw text only behind a new scoped ref;
        # bytes remain intact for the S-2 magic-byte file gate.
        ref = json.loads(
            scrub_checkpoint_envelope(
                json.dumps({"mode": mode, "payload": {"body": raw_payload}}, ensure_ascii=False),
                _payload_store,
                session_id=actual_session,
                trusted_scope=scope,
                policy=_CHECKPOINT_SCRUB_POLICY,
            )
        )["payload_ref"]
    else:
        ref = _payload_store.put(raw_payload, scope=scope, session_id=actual_session)
    envelope = json.dumps(
        {
            "mode": mode,
            "company_id": scope["company_id"],
            "issuance_id": scope["issuance_id"],
            "payload_ref": ref,
            "session_id": actual_session,
        },
        ensure_ascii=False,
    )
    try:
        with bound_secrets(graph._secrets_provider):
            result = graph.invoke(
                envelope,
                ctx=_context(
                    auth,
                    session_id=actual_session,
                    thread_id=actual_thread,
                    hitl_allowed=_hitl_allowed(auth),
                ),
                input_context={"mode": mode, **scope, "payload_ref": ref, "session_id": actual_session},
            )
        if result.get("status") == "awaiting_human":
            _pending_hitl.register(
                actual_thread,
                graph,
                scope,
                auth.principal["caller_id"],
                actual_session,
            )
            result = {**result, "session_id": actual_session}
        return result
    finally:
        _payload_store.delete(ref, scope=scope, session_id=actual_session)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    # Malformed base64 must be a client error (400), consistent with /ingest —
    # never an unhandled 500.
    if req.file_bytes_b64:
        try:
            raw = base64.b64decode(req.file_bytes_b64, validate=True)
        except (base64.binascii.Error, ValueError) as exc:
            raise HTTPException(status_code=400, detail="file_bytes_b64 is invalid") from exc
    else:
        raw = req.input
    return await _invoke_mode("invoke", request, raw, req.session_id, req.thread_id)


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    if not req.file_bytes_b64:
        raise HTTPException(status_code=400, detail="ingest requires a CSV, XLSX, or DOCX payload")
    try:
        raw = base64.b64decode(req.file_bytes_b64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="file_bytes_b64 is invalid") from exc
    return await _invoke_mode("ingest", request, raw, req.session_id, req.thread_id)


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    return await _invoke_mode(
        "feedback",
        request,
        {
            "source_id": req.source_id,
            "accepted": req.accepted,
            "final_reason": req.final_reason,
            "learnable": req.learnable,
        },
        req.session_id,
        req.thread_id,
    )


def _authorize_resume(
    graph: ProspectusDisclosureReviewGraph,
    thread_id: str,
    scope: dict[str, str],
    session_id: str,
) -> None:
    _clean_scope(scope)
    graph._ensure_compiled()
    snapshot = graph._compiled.get_state(graph._thread_config(thread_id))
    if not snapshot or not snapshot.values or not snapshot.next:
        raise HTTPException(status_code=404, detail="No HITL checkpoint found")
    if snapshot.values.get("tenant_id") != f"{scope['company_id']}:{scope['issuance_id']}":
        raise HTTPException(status_code=403, detail="Caller does not own this HITL session")
    if snapshot.values.get("session_id") != session_id:
        raise HTTPException(status_code=403, detail="Caller does not own this HITL session")


@app.post("/resume")
async def resume(req: ResumeRequest, request: Request):
    auth = authenticate(request, operation=Operation.RESUME)
    verified_scope = dict(auth.scope)
    if verified_scope != {"company_id": req.company_id, "issuance_id": req.issuance_id}:
        raise HTTPException(status_code=403, detail="Resume scope does not match verified caller scope")
    pending = _pending_hitl.claim(
        req.thread_id,
        verified_scope,
        auth.principal["caller_id"],
        req.session_id,
    )
    try:
        _authorize_resume(pending.graph, req.thread_id, verified_scope, pending.session_id)
        with bound_secrets(pending.graph._secrets_provider):
            result = pending.graph.resume(thread_id=req.thread_id, feedback=req.feedback)
    except Exception:
        if _checkpoint_is_pending(pending.graph, req.thread_id):
            _pending_hitl.register(
                req.thread_id,
                pending.graph,
                verified_scope,
                pending.caller_id,
                pending.session_id,
            )
        raise
    if result.get("status") == "awaiting_human":
        _pending_hitl.register(
            req.thread_id,
            pending.graph,
            verified_scope,
            pending.caller_id,
            pending.session_id,
        )
        result = {**result, "session_id": pending.session_id}
    return result


def _checkpoint_is_pending(graph: ProspectusDisclosureReviewGraph, thread_id: str) -> bool:
    graph._ensure_compiled()
    snapshot = graph._compiled.get_state(graph._thread_config(thread_id))
    return bool(snapshot and snapshot.values and snapshot.next)


@app.get("/health")
def health():
    return {"status": "ok", "agent": "ProspectusDisclosureReviewGraph"}
