"""Outer S-2 channel gate, mode validation and fail-closed tenant binding."""

from __future__ import annotations

import json
from typing import Any, ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_state import AgentState
from framework.schemas.agent_status import AgentStatus
from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.ingest_file_sanitizer import sanitize_file_bytes
from src.payload_store import PayloadStore, PayloadStoreError
from src.services.pii_sanitizer import sanitize_value

_MODES = frozenset(("ingest", "invoke", "feedback"))


class PreProcessNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        *,
        payload_store: PayloadStore | None = None,
        max_file_bytes: int = 10_485_760,
        max_batch_records: int = 500,
        max_free_text_chars: int = 20_000,
        trusted_scope: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store
        self._max_file_bytes = max_file_bytes
        self._max_batch_records = _positive_limit(max_batch_records)
        self._max_free_text_chars = _positive_limit(max_free_text_chars)
        self._trusted_scope = dict(trusted_scope or {})
        self._session_id = session_id

    def _extra_security_gate_input(self, state: AgentState) -> AgentState:
        """S-2 looks at the submitted payload, never at ``state['mode']``."""
        context = state.get("input_context") or {}
        if not isinstance(context, dict) or self._payload_store is None:
            return state
        ref = context.get("payload_ref") or state.get("payload_ref")
        if not ref:
            return state
        scope = {key: context.get(key, "") for key in ("company_id", "issuance_id")}
        session_id = str(state.get("session_id") or context.get("session_id") or "")
        if scope != self._trusted_scope or session_id != self._session_id:
            return {
                **state,
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode S-2: request context does not match authenticated context"],
            }
        try:
            raw = self._payload_store.resolve(ref, scope=scope, session_id=session_id, consume=True)
            if isinstance(raw, bytes):
                sanitized = sanitize_file_bytes(
                    raw, allowed_channels=("csv", "xlsx", "docx"), max_file_bytes=self._max_file_bytes
                )
                if not sanitized.accepted:
                    return {
                        **state,
                        "status": AgentStatus.ERROR.value,
                        "error_log": [f"PreProcessNode S-2: {sanitized.reject_reason}"],
                    }
                payload: dict[str, Any] = {"rows": sanitized.sanitized_rows, "text": sanitized.sanitized_text or ""}
            else:
                payload, _count, resolvable = sanitize_value(raw)
                if not resolvable:
                    return {
                        **state,
                        "status": AgentStatus.ERROR.value,
                        "error_log": ["PreProcessNode S-2: PII could not be sanitized (fail closed)"],
                    }
            rows = payload.get("rows") if isinstance(payload, dict) else None
            if isinstance(rows, list) and len(rows) > self._max_batch_records:
                return {
                    **state,
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["PreProcessNode S-2: batch record limit exceeded"],
                }
            if _contains_oversized_text(payload, self._max_free_text_chars):
                return {
                    **state,
                    "status": AgentStatus.ERROR.value,
                    "error_log": ["PreProcessNode S-2: free-text limit exceeded"],
                }
            sanitized_ref = self._payload_store.put(payload, scope=scope, session_id=session_id)
        except PayloadStoreError as exc:
            return {**state, "status": AgentStatus.ERROR.value, "error_log": [f"PreProcessNode S-2: {exc}"]}
        new_context = dict(context)
        new_context["payload_ref"] = sanitized_ref
        return {**state, "input_context": new_context, "payload_ref": sanitized_ref}

    def execute(self, state: AgentState) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            emit_trace_event("pre_process_rejected", {"reason": "s2"}, state)
            return {}
        context = state.get("input_context") or {}
        if not isinstance(context, dict):
            context = {}
        mode = context.get("mode")
        company_id = self._trusted_scope.get("company_id")
        issuance_id = self._trusted_scope.get("issuance_id")
        if mode not in _MODES:
            emit_trace_event("pre_process_rejected", {"reason": "mode"}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["PreProcessNode: mode is not adapter-declared"]}
        if (
            context.get("company_id") != company_id
            or context.get("issuance_id") != issuance_id
            or str(state.get("session_id") or context.get("session_id") or "") != self._session_id
        ):
            emit_trace_event("tenant_resolution_failed", {"reason": "context mismatch"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: request context does not match authenticated context"],
            }
        # Resolve context to retain the framework's correlation/audit plumbing;
        # scope values themselves originate only in the authenticated adapter.
        InvocationContext.from_state(state)
        if not _clean_id(company_id) or not _clean_id(issuance_id):
            emit_trace_event("tenant_resolution_failed", {"reason": "invalid scope"}, state)
            return {
                "status": AgentStatus.ERROR.value,
                "error_log": ["PreProcessNode: company_id/issuance_id unresolved (fail closed)"],
            }
        tenant_id = f"{company_id}:{issuance_id}"
        payload_ref = str(context.get("payload_ref") or state.get("payload_ref") or "")
        envelope = {
            "mode": mode,
            "tenant_id": tenant_id,
            "company_id": company_id,
            "issuance_id": issuance_id,
            "session_id": state.get("session_id", ""),
            "payload_ref": payload_ref,
        }
        emit_trace_event("pre_process_validated", {"mode": mode, "tenant_id": tenant_id}, state)
        return {
            "mode": mode,
            "tenant_id": tenant_id,
            "payload_ref": payload_ref,
            "validated_input": json.dumps(envelope, ensure_ascii=False, default=str),
            "status": AgentStatus.SUCCESS.value,
        }


def _clean_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and value == value.strip()
        and ":" not in value
        and "\n" not in value
    )


def _positive_limit(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("PreProcessNode: input limits must be positive integers")
    return value


def _contains_oversized_text(value: object, limit: int) -> bool:
    if isinstance(value, str):
        return len(value) > limit
    if isinstance(value, list):
        return any(_contains_oversized_text(item, limit) for item in value)
    if isinstance(value, dict):
        return any(_contains_oversized_text(item, limit) for item in value.values())
    return False
