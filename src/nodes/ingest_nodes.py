"""Ingest branch: parse, sanitize, classify and insert structured facts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import yaml
from framework.nodes.function_node import FunctionNode
from framework.schemas.agent_status import AgentStatus
from framework.schemas.trust_level import TrustLevel
from shared.utils.audit_logger import emit_trace_event

from src.ingest_row_pipeline import IngestRowPipelineNode, RowPipelineHooks
from src.payload_store import PayloadStore, PayloadStoreError
from src.services.kb_service import KBService
from src.services.pii_sanitizer import sanitize_value


def normalize_for_retrieval(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


class DisclosureIngestPipeline(IngestRowPipelineNode):
    """FIN adaptation of the copy-in row pipeline's required-field policy."""

    required_fields = ("source_record_id", "source_kind", "fact_summary")
    required_any_fields = ()


class IngestParseNode(FunctionNode):
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
            return _error("IngestParseNode: payload store unavailable", state)
        try:
            payload = self._payload_store.resolve(
                state.get("payload_ref"),
                scope=self._trusted_scope,
                session_id=self._session_id or "",
                consume=True,
            )
        except PayloadStoreError as exc:
            return _error(f"IngestParseNode: {exc}", state)
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        text = payload.get("text", "") if isinstance(payload, dict) else ""
        if not rows and text:
            rows = [_docx_record(text)]
        if not isinstance(rows, list) or not rows:
            return _error("IngestParseNode: no parseable records", state)
        mapped_rows = [_map_ingest_row(row) for row in rows if isinstance(row, dict)]
        emit_trace_event("ingest_parsed", {"record_count": len(mapped_rows)}, state)
        return {"_parsed_records": mapped_rows, "status": AgentStatus.SUCCESS.value}


class IngestSanitizeNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        clean, count, resolvable = sanitize_value(state.get("_parsed_records", []))
        emit_trace_event("ingest_pii_sanitized", {"replacement_count": count}, state)
        if not resolvable:
            return _error("IngestSanitizeNode: unresolvable PII", state)
        return {"_parsed_records": clean, "status": AgentStatus.SUCCESS.value}


class TaxonomyTagNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        taxonomy = _taxonomy()
        flags: list[str] = []
        records = []
        for record in state.get("_parsed_records", []):
            item = dict(record)
            for key in ("source_kind", "disclosure_section", "severity_class"):
                if item.get(key) not in taxonomy.get(key, []):
                    item[key] = "unclassified"
                    flags.append(f"{key}:unclassified")
            records.append(item)
        emit_trace_event("ingest_taxonomy_tagged", {"flag_count": len(flags)}, state)
        return {"_parsed_records": records, "missing_data_flags": flags, "status": AgentStatus.SUCCESS.value}


class KbUpsertNode(FunctionNode):
    required_trust_level: ClassVar[TrustLevel] = TrustLevel.VERIFIED_EXTERNAL

    def __init__(
        self,
        kb_service: KBService | None = None,
        payload_store: PayloadStore | None = None,
        trusted_scope: dict[str, str] | None = None,
        session_id: str | None = None,
    ) -> None:
        super().__init__()
        self._kb_service = kb_service
        self._payload_store = payload_store
        self._trusted_scope = dict(trusted_scope or {})
        self._session_id = session_id

    def execute(self, state: dict) -> dict:
        if state.get("status") == AgentStatus.ERROR.value:
            return {}
        if self._kb_service is None:
            return _error("KbUpsertNode: KB service unavailable", state)

        def sanitize(_state: dict, row: dict) -> dict:
            clean, _count, ok = sanitize_value(row)
            if not ok:
                raise ValueError("unresolvable PII")
            return clean

        def write(current: dict, row: dict) -> str:
            company_id = self._trusted_scope.get("company_id", "")
            issuance_id = self._trusted_scope.get("issuance_id", "")
            return self._kb_service.insert_record(f"{company_id}:{issuance_id}", issuance_id, row)

        pipeline = DisclosureIngestPipeline(
            payload_store=self._payload_store,
            trusted_scope=self._trusted_scope,
            session_id=self._session_id,
            hooks=RowPipelineHooks(sanitize=sanitize, normalize=lambda _s, row: _normalise_record(row), write=write)
        )
        # Rows were resolved from the scoped payload store by IngestParseNode;
        # never hand the consumed reference to the row part a second time.
        result = pipeline.execute({**state, "payload_ref": None, "rows": state.get("_parsed_records", [])})
        emit_trace_event(
            "kb_upsert_complete", {"written": result.get("ingest_summary", {}).get("written_count", 0)}, state
        )
        return result


def _normalise_record(row: dict) -> dict:
    item = dict(row)
    item["fact_summary"] = normalize_for_retrieval(item.get("fact_summary"))
    attrs = item.get("asserted_attrs")
    if isinstance(attrs, str):
        try:
            item["asserted_attrs"] = json.loads(attrs)
        except ValueError:
            item["asserted_attrs"] = {}
    return item


def _docx_record(text: str) -> dict:
    return {
        "source_record_id": "docx_record",
        "source_kind": "board_material",
        "fact_summary": text,
        "disclosure_section": "unclassified",
        "asserted_attrs": {},
    }


def _map_ingest_row(row: dict[str, Any]) -> dict[str, Any]:
    """Apply customer-maintained ingest_mapping.yaml without guessing columns."""
    mapping = _ingest_mapping().get(
        "prior_disclosure" if row.get("source_kind") == "prior_disclosure" else "internal_source", {}
    )
    mapped = dict(row)
    for canonical, source_column in mapping.items():
        if canonical not in mapped and source_column in row:
            mapped[canonical] = row[source_column]
    return mapped


def _error(message: str, state: dict) -> dict:
    emit_trace_event("ingest_failed", {"reason": message}, state)
    return {"status": AgentStatus.ERROR.value, "error_log": [message]}


def _taxonomy() -> dict:
    try:
        with Path("config/taxonomy.yaml").open(encoding="utf-8") as file:
            return yaml.safe_load(file) or {}
    except FileNotFoundError:
        return {}


def _ingest_mapping() -> dict:
    try:
        with Path("config/ingest_mapping.yaml").open(encoding="utf-8") as file:
            loaded = yaml.safe_load(file) or {}
    except FileNotFoundError:
        return {}
    return loaded if isinstance(loaded, dict) else {}
