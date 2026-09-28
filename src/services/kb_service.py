"""Tenant-scoped SQLite-compatible KB adapter for disclosure control data."""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping


class KBNotFoundError(LookupError):
    """A tenant-scoped feedback update matched no record."""


class KBService:
    """Store only sanitized, structured facts and safe retrieval projections."""

    def __init__(self, db: sqlite3.Connection | None = None) -> None:
        self._db = db
        if db is not None:
            self.ensure_schema()

    def ensure_schema(self) -> None:
        if self._db is None:
            return
        self._db.executescript("""
        CREATE TABLE IF NOT EXISTS bond_issuances (
          issuance_id TEXT NOT NULL, tenant_id TEXT NOT NULL, source_record_id TEXT NOT NULL,
          filing_type TEXT, draft_version TEXT, ingested_at TEXT NOT NULL,
          PRIMARY KEY (tenant_id, source_record_id)
        );
        CREATE TABLE IF NOT EXISTS internal_source_registry (
          source_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, issuance_id TEXT NOT NULL,
          source_record_id TEXT NOT NULL, source_kind TEXT NOT NULL, disclosure_section TEXT,
          fact_summary TEXT NOT NULL, asserted_attrs TEXT NOT NULL, provenance TEXT,
          quality_flag TEXT, source TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS prior_disclosures (
          disclosure_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, issuance_id TEXT NOT NULL,
          disclosure_section TEXT NOT NULL, version TEXT NOT NULL, asserted_attrs TEXT NOT NULL,
          disclosed_on TEXT
        );
        """)
        self._db.commit()

    def insert_record(self, tenant_id: str, issuance_id: str, record: Mapping[str, Any]) -> str:
        if not tenant_id or not issuance_id:
            raise ValueError("KBService.insert_record: tenant_id and issuance_id are required")
        if self._db is None:
            raise RuntimeError("KBService.insert_record: database is not wired")
        source_record_id = str(record.get("source_record_id") or "")
        source_kind = str(record.get("source_kind") or "")
        fact_summary = str(record.get("fact_summary") or "")
        if not source_record_id or not source_kind or not fact_summary:
            raise ValueError("KBService.insert_record: source_record_id, source_kind and fact_summary are required")
        attrs = record.get("asserted_attrs") if isinstance(record.get("asserted_attrs"), dict) else {}
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "INSERT OR REPLACE INTO bond_issuances VALUES (?, ?, ?, ?, ?, ?)",
            (
                issuance_id,
                tenant_id,
                source_record_id,
                record.get("filing_type", ""),
                record.get("draft_version", ""),
                now,
            ),
        )
        if source_kind == "prior_disclosure":
            source_id = str(record.get("disclosure_id") or f"disclosure_{uuid.uuid4().hex[:12]}")
            self._db.execute(
                "INSERT OR REPLACE INTO prior_disclosures VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    tenant_id,
                    issuance_id,
                    record.get("disclosure_section") or "unclassified",
                    record.get("version") or "unknown",
                    json.dumps(attrs, ensure_ascii=False),
                    record.get("disclosed_on", ""),
                ),
            )
        else:
            source_id = str(record.get("source_id") or f"source_{uuid.uuid4().hex[:12]}")
            self._db.execute(
                "INSERT OR REPLACE INTO internal_source_registry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    source_id,
                    tenant_id,
                    issuance_id,
                    source_record_id,
                    source_kind,
                    record.get("disclosure_section") or "unclassified",
                    fact_summary,
                    json.dumps(attrs, ensure_ascii=False),
                    record.get("provenance", ""),
                    record.get("quality_flag", "low"),
                    record.get("source", "ingest"),
                ),
            )
        self._db.commit()
        return source_id

    def search(
        self,
        *,
        scope: Mapping[str, str],
        query: str,
        top_k: int,
        min_similarity: float = 0.0,
    ) -> list[dict[str, Any]]:
        company_id, issuance_id = scope.get("company_id", ""), scope.get("issuance_id", "")
        if not company_id or not issuance_id or self._db is None:
            return []
        normalized_query = str(query or "").strip()
        if not normalized_query:
            return []
        threshold = _similarity_threshold(min_similarity)
        limit = max(1, int(top_k))
        tenant_id = f"{company_id}:{issuance_id}"
        rows = self._db.execute(
            "SELECT source_id, source_kind, disclosure_section, fact_summary, asserted_attrs, provenance, quality_flag, "
            "tenant_id, issuance_id "
            "FROM internal_source_registry WHERE tenant_id=? AND issuance_id=?",
            (tenant_id, issuance_id),
        ).fetchall()
        prior = self._db.execute(
            "SELECT disclosure_id, disclosure_section, version, asserted_attrs, tenant_id, issuance_id "
            "FROM prior_disclosures "
            "WHERE tenant_id=? AND issuance_id=?",
            (tenant_id, issuance_id),
        ).fetchall()
        records: list[dict[str, Any]] = []
        for (
            source_id,
            source_kind,
            section,
            fact_summary,
            attrs_text,
            provenance,
            quality,
            stored_tenant,
            stored_issuance,
        ) in rows:
            attrs = _json_object(attrs_text)
            stored_scope = _stored_scope(stored_tenant, stored_issuance)
            score = _query_similarity(normalized_query, fact_summary, source_kind, section, attrs)
            if score < threshold:
                continue
            record = {
                "source_id": source_id,
                "source_kind": source_kind,
                "disclosure_section": section,
                "fact_summary": fact_summary,
                "provenance": provenance,
                "quality_flag": quality,
                "score": score,
                "asserted_attrs": attrs,
                "metadata": {
                    "source_id": source_id,
                    "source_kind": source_kind,
                    "disclosure_section": section,
                    **stored_scope,
                },
            }
            record.update({key: value for key, value in attrs.items() if isinstance(value, (str, int, float, bool))})
            records.append(record)
        for source_id, section, version, attrs_text, stored_tenant, stored_issuance in prior:
            attrs = _json_object(attrs_text)
            stored_scope = _stored_scope(stored_tenant, stored_issuance)
            score = _query_similarity(normalized_query, section, version, attrs)
            if score < threshold:
                continue
            record = {
                "source_id": source_id,
                "source_kind": "prior_disclosure",
                "disclosure_section": section,
                "version": version,
                "score": score,
                "asserted_attrs": attrs,
                "metadata": {
                    "source_id": source_id,
                    "source_kind": "prior_disclosure",
                    "disclosure_section": section,
                    "version": version,
                    **stored_scope,
                },
            }
            record.update({key: value for key, value in attrs.items() if isinstance(value, (str, int, float, bool))})
            records.append(record)
        records.sort(key=lambda record: (-float(record["score"]), str(record["source_id"])))
        return records[:limit]

    def search_by_ids(self, tenant_id: str, source_ids: list[str]) -> list[dict[str, Any]]:
        """Safe multiple-id query: one bound placeholder per id, never IN (?) for a list."""
        if not tenant_id or not source_ids or self._db is None:
            return []
        ids = list(dict.fromkeys(source_ids))
        if len(ids) > 200:
            raise ValueError("KBService.search_by_ids: too many source ids")
        placeholders = ", ".join("?" for _ in ids)
        rows = self._db.execute(
            f"SELECT source_id, source_kind, disclosure_section, asserted_attrs FROM internal_source_registry "
            f"WHERE tenant_id=? AND source_id IN ({placeholders})",
            [tenant_id, *ids],
        ).fetchall()
        return [
            {
                "source_id": row[0],
                "source_kind": row[1],
                "disclosure_section": row[2],
                "asserted_attrs": _json_object(row[3]),
            }
            for row in rows
        ]

    def update_reflection(self, tenant_id: str, source_id: str, reflection: Mapping[str, Any]) -> dict[str, Any]:
        if not tenant_id or not source_id:
            raise ValueError("KBService.update_reflection: tenant_id and source_id are required")
        if self._db is None:
            raise RuntimeError("KBService.update_reflection: database is not wired")
        rowcount = self._db.execute(
            "UPDATE internal_source_registry SET source='feedback' WHERE tenant_id=? AND source_id=?",
            (tenant_id, source_id),
        ).rowcount
        self._db.commit()
        if type(rowcount) is not int or rowcount < 1:  # noqa: E721 - bool must not count as a written row
            raise KBNotFoundError("KBService.update_reflection: no record in this tenant")
        return {"source_id": source_id, "recorded": True, "rows_updated": rowcount}


def _json_object(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _stored_scope(tenant_id: object, issuance_id: object) -> dict[str, str]:
    """Project scope from persisted columns, never from the search request."""
    tenant = str(tenant_id or "")
    stored_issuance = str(issuance_id or "")
    company, separator, tenant_issuance = tenant.partition(":")
    if not separator or not company or not stored_issuance or tenant_issuance != stored_issuance:
        return {}
    return {"company_id": company, "issuance_id": stored_issuance}


def _similarity_threshold(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("KBService.search: min_similarity must be a number between zero and one")
    try:
        threshold = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("KBService.search: min_similarity must be a number between zero and one") from exc
    if not 0.0 <= threshold <= 1.0:
        raise ValueError("KBService.search: min_similarity must be a number between zero and one")
    return threshold


def _query_similarity(query: str, *values: object) -> float:
    searchable = _tokens(" ".join(_flatten_search_values(value) for value in values))
    if not searchable:
        return 0.0
    fragments = query.split()
    groups = [_tokens(fragment) for fragment in fragments] if any("_" in fragment for fragment in fragments) else [_tokens(query)]
    scores = [len(group & searchable) / len(group) for group in groups if group]
    return round(max(scores, default=0.0), 6)


def _flatten_search_values(value: object) -> str:
    if isinstance(value, Mapping):
        return " ".join(_flatten_search_values(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return " ".join(_flatten_search_values(item) for item in value)
    return str(value or "")


def _tokens(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKC", value).casefold().replace("_", " ")
    return {token for token in re.findall(r"[^\W_]+", normalized, flags=re.UNICODE) if token}
