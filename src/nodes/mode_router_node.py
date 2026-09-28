"""Restore the JSON GraphNode envelope into the inner state."""

from __future__ import annotations

import json
from typing import ClassVar

from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event


class ModeRouterNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        try:
            envelope = json.loads(state.get("user_input") or "{}")
        except (TypeError, ValueError):
            envelope = {}
        mode = envelope.get("mode")
        if mode not in {"ingest", "invoke", "feedback"} or not envelope.get("tenant_id"):
            emit_trace_event("mode_router_rejected", {"mode": mode}, state)
            return {"status": AgentStatus.ERROR.value, "error_log": ["ModeRouterNode: invalid envelope"]}
        emit_trace_event("mode_router_restored", {"mode": mode}, state)
        return {key: envelope.get(key, "") for key in ("mode", "tenant_id", "company_id", "issuance_id", "session_id", "payload_ref")} | {"status": AgentStatus.SUCCESS.value}
