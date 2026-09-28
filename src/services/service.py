"""Small domain service facade for callers that need a safe KB query seam."""

from __future__ import annotations

from typing import Any

from src.services.kb_service import KBService


class Service:
    """Expose the retrieval-safe projection, never raw document bodies."""

    def __init__(self, kb_service: KBService | None = None) -> None:
        self._kb_service = kb_service

    async def fetch(self, query: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = context or {}
        scope = {"company_id": context.get("company_id", ""), "issuance_id": context.get("issuance_id", "")}
        if self._kb_service is None or not all(scope.values()):
            return {"records": [], "degradation_reason": "KB service or tenant scope unavailable"}
        return {"records": self._kb_service.search(scope=scope, query=query, top_k=int(context.get("top_k", 8)))}
