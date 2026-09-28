"""Feedback branch: validate review reflection then update only own tenant."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.payload_store import PayloadStore, PayloadStoreError
from src.services.kb_service import KBNotFoundError, KBService
from src.services.pii_sanitizer import sanitize_value


class FeedbackValidateNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        payload_store: PayloadStore | None = None,
        trusted_scope: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._payload_store = payload_store
        self._trusted_scope = dict(trusted_scope or {})
        self._session_id = session_id

    def execute(self, state: dict) -> dict:
        if self._payload_store is None:
            return _error("FeedbackValidateNode: payload store unavailable", state)
        try:
            payload = self._payload_store.resolve(
                state.get("payload_ref"),
                scope=self._trusted_scope,
                session_id=self._session_id or "",
                consume=True,
            )
        except PayloadStoreError as exc:
            return _error(f"FeedbackValidateNode: {exc}", state)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = {}
        if not isinstance(payload, dict) or not payload.get("source_id") or "accepted" not in payload:
            return _error("FeedbackValidateNode: source_id and accepted are required", state)
        clean, count, ok = sanitize_value(payload)
        emit_trace_event("feedback_validated", {"pii_removed": count}, state)
        if not ok:
            return _error("FeedbackValidateNode: PII sanitization failed", state)
        return {"_feedback_payload": clean, "status": AgentStatus.SUCCESS.value}


class KbUpdateReflectionNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(self, kb_service: KBService | None = None) -> None:
        super().__init__()
        self._kb_service = kb_service

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if self._kb_service is None:
            return _error("KbUpdateReflectionNode: KB service unavailable", state)
        payload = state.get("_feedback_payload", {})
        try:
            result = self._kb_service.update_reflection(
                state.get("tenant_id", ""), str(payload.get("source_id", "")), payload
            )
        except (KBNotFoundError, ValueError, RuntimeError) as exc:
            return _error(f"KbUpdateReflectionNode: {exc}", state)
        emit_trace_event("feedback_kb_updated", {"source_id": result["source_id"]}, state)
        return {"feedback_summary": result, "status": AgentStatus.SUCCESS.value}


def _error(message: str, state: dict) -> dict:
    emit_trace_event("feedback_failed", {"reason": message}, state)
    return {"status": AgentStatus.ERROR.value, "error_log": [message]}
