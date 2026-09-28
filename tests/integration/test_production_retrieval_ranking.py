"""本番 Stage1 → Stage2 の配線で関連レコードが上位に来ることを固定する。

由来: 2026-08-13 の独立レビュー（Batch 2 二巡目）。レビュアーの再現 probe をそのまま
回帰テストにしている。

欠陥: `_query_similarity()` 自体は動くが、**本番 Stage1 は入力本文を query に残さず、
検出した固定 section 名だけを渡す**。そのため実配線では query が `risk_factors` になり、
同じ section の関連・無関連レコードが両方 `score=1.0`、ID 順で無関連が先頭に来る。
既存テストは production が生成しない query を Stage2 へ直接注入していたため検出できなかった。

🔴 このテストは **Stage1 の入力から始める**（Stage2 に query を直接渡さない）。
`structured_query` の文字列そのものは固定しない — 配線が変わっても壊れないようにするため。
"""
from __future__ import annotations

import sqlite3

from src.nodes.invoke_stage_nodes import Stage1DisclosureStructureNode, Stage2ScopedRetrievalNode
from src.payload_store import PayloadStore
from src.services.kb_service import KBService

SCOPE = {"company_id": "company", "issuance_id": "issue"}


def test_related_record_outranks_unrelated_one_through_production_wiring():
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    # 同じ section に関連 / 無関連を 1 件ずつ。ID 順では無関連が先に来る配置。
    for record in [
        {"source_record_id": "a", "source_id": "a_unrelated", "source_kind": "risk_register",
         "disclosure_section": "risk_factors", "fact_summary": "office relocation timetable"},
        {"source_record_id": "z", "source_id": "z_related", "source_kind": "risk_register",
         "disclosure_section": "risk_factors", "fact_summary": "liquidity outlook deteriorated"},
    ]:
        kb.insert_record("company:issue", "issue", record)

    ref = store.put("liquidity deteriorated risk_factors", scope=SCOPE, session_id="s")
    stage1 = Stage1DisclosureStructureNode(store, SCOPE, "s").execute({**SCOPE, "payload_ref": ref})
    stage2 = Stage2ScopedRetrievalNode(kb, top_k=2, min_similarity=0.0).execute({**SCOPE, **stage1})

    kb_records = [r for r in stage2.get("retrieved_records", [])
                  if r.get("source_kind") != "prospectus_draft"]
    ids = [r.get("source_id") for r in kb_records]
    assert "z_related" in ids, f"関連レコードが検索されていない: {ids}"
    assert ids.index("z_related") < ids.index("a_unrelated"), (
        f"無関連レコードが関連レコードより上位: {ids} / scores={[r.get('score') for r in kb_records]}"
    )


def test_scores_differentiate_related_from_unrelated():
    """スコアが実際に差をつけていること（全件同点なら順序は運）。"""
    store = PayloadStore(scope_keys=("company_id", "issuance_id"))
    kb = KBService(sqlite3.connect(":memory:"))
    for record in [
        {"source_record_id": "a", "source_id": "a_unrelated", "source_kind": "risk_register",
         "disclosure_section": "risk_factors", "fact_summary": "office relocation timetable"},
        {"source_record_id": "z", "source_id": "z_related", "source_kind": "risk_register",
         "disclosure_section": "risk_factors", "fact_summary": "liquidity outlook deteriorated"},
    ]:
        kb.insert_record("company:issue", "issue", record)
    ref = store.put("liquidity deteriorated risk_factors", scope=SCOPE, session_id="s")
    stage1 = Stage1DisclosureStructureNode(store, SCOPE, "s").execute({**SCOPE, "payload_ref": ref})
    stage2 = Stage2ScopedRetrievalNode(kb, top_k=2, min_similarity=0.0).execute({**SCOPE, **stage1})
    scores = {r.get("source_id"): r.get("score") for r in stage2.get("retrieved_records", [])
              if r.get("source_kind") != "prospectus_draft"}
    assert scores.get("z_related", 0) > scores.get("a_unrelated", 0), (
        f"関連と無関連のスコアが差別化されていない: {scores}"
    )
