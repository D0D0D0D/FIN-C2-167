# テスト仕様 — FIN-C2-167

本仕様は `parts/TESTING.md` の層2（部品接合部）と層3（横断）を、FIN-C2-167 の実ノード・実設定・実テストへ具体化したものである。層1の部品同梱テストは `tests/parts/` で327件を維持する。表中のテスト参照はすべて実在する pytest 関数であり、テスト名を変更する場合は本書も同時に更新する。

## Framework compliance（TC-01〜TC-08）

| ID | 実装対象・assertion | 対応テスト |
|---|---|---|
| TC-01 | `State` / `DisclosureDiffWorkflowState` が資格情報・Pydantic・dataclass を持たない flat TypedDict である | `tests/proof_of_boundary/test_state_safety.py::TestStateSafety::test_state_file_safety` |
| TC-02 | `PostProcessNode` の欠落 output contract と禁止 verdict field が `SecurityViolationError` になる | `tests/integration/test_part_junctions.py::test_e1_envelope_missing_required_fields_blocks_and_complete_passes` |
| TC-03 | `src/` に Level 0 `agenticstar` / platform import がない | `tests/proof_of_boundary/test_import_isolation.py::TestImportIsolation::test_no_prohibited_imports_in_src` |
| TC-04 | `PreProcessNode` が `InvocationContext.from_state()` を利用して verified company/issuance から tenant_id を解決する | `tests/integration/test_part_junctions.py::test_tc04_tc06_context_from_state_and_final_s2_gate_sanitize_payload` |
| TC-05 | 各 concrete node が実 `__call__` 経路で framework audit を発行し、S-2/S-3 前後の実行順を保つ | `tests/proof_of_boundary/test_pb_invoke_order.py::TestInvokeOrder::test_call_order_for_every_node` |
| TC-06 | FunctionNode の S-2 final gate を経由して、SDK `detect_pii` が検知する Latin name / email / phone をマスクする | `tests/integration/test_part_junctions.py::test_tc04_tc06_context_from_state_and_final_s2_gate_sanitize_payload` |
| TC-07 | FunctionNode の S-3 final gate を経由して、5キー envelope の欠落を拒否する | `tests/integration/test_part_junctions.py::test_tc07_final_s3_gate_rejects_missing_output_contract` |
| TC-08 | `required_trust_level=VERIFIED_EXTERNAL` の実ノード群を `node(state)` で呼び、S-1→S-4→S-2→execute→S-3 順を検証する | `tests/proof_of_boundary/test_pb_invoke_order.py::TestInvokeOrder::test_call_order_for_every_node`。加えて **不足 trust では `execute()` に到達しないこと**を証明 — PB-6 `TestInvokeOrder::test_s1_denial_refuses_execution_before_execute` が `_PrivilegedTrustGateFixture` を不足 trust で `node(state)` 呼びし、`status=error` / `error_log[0]` の `S-1 trust gate denied` / `events == ["s1_denied"]` / **`execute()` 未呼び出し** を assert（正本 docs/03 の TC-08 要求） |

## Proof of boundary（PB-1〜PB-7）

| ID | 境界・assertion | 対応テスト |
|---|---|---|
| PB-1 | BaseNode→AuditLogger。実ノードの node_start/node_complete と domain audit の順序を確認する | `tests/proof_of_boundary/test_pb_invoke_order.py::TestInvokeOrder::test_call_order_for_every_node` |
| PB-2 | State の JSON/msgpack 安全性。資格情報・任意オブジェクトを State 定義に持たない | `tests/proof_of_boundary/test_state_safety.py::TestStateSafety::test_state_file_safety` |
| PB-3 | 実 sqlite3 の KB。保存レコード由来 scope を hit に投影し実 `ScopedRetrievalNode` で1件ヒットする。複数ID bind・tenant 分離も検証する | `tests/proof_of_boundary/test_pb3_real_database.py::test_r1_real_sqlite_returns_stored_scope_and_one_scoped_hit`、`tests/proof_of_boundary/test_pb3_real_database.py::test_pb3_multi_id_sql_is_bound_and_tenant_scoped` |
| PB-4 | import isolation を AST で全 `src/` に適用する | `tests/proof_of_boundary/test_import_isolation.py::TestImportIsolation::test_no_prohibited_imports_in_src` |
| PB-5 | checkpoint envelope に原文センチネルが残らないことを ingest/invoke/feedback の3モードで確認する | `tests/integration/test_part_junctions.py::test_i5_checkpoint_sentinel_absent_from_scrubbed_envelopes_for_all_modes` |
| PB-6 | S-1→S-4 start→S-2→execute→S-3→S-4 complete の invoke 実行順序を全 node で確認する | `tests/proof_of_boundary/test_pb_invoke_order.py::TestInvokeOrder::test_call_order_for_every_node` |
| PB-7 | 正常な HITL draft の interrupt が `GraphInterrupt` として node 境界から伝播し、`hitl_allowed=False` では発火しない。逆方向として断定・PII・未検証 source ID を含む draft は interrupt 前に `status=error` となる | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py::test_pb7_hitl_interrupt_propagates`、`tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py::test_pb7_hitl_allowed_false_skips_interrupt`、`tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py::test_pb7_hitl_draft_is_gated_before_interrupt` |
| PB-8 | 実 InMemorySaver の outer / inner checkpoint を suspend直後・resume後に読み、payload_ref 背後の raw 本文センチネルが存在しないことを確認する。`hitl_draft` の構造化ブリーフ保存は許容する | `tests/proof_of_boundary/test_pb_hitl_checkpoint_raw_text_absent.py::test_pb8_hitl_suspend_and_resume_checkpoints_exclude_raw_payload_text` |

## 層2 — 部品接合部

| 部品 / ID | FIN-C2-167 での接合 assertion | 対応テスト |
|---|---|---|
| cat2-graph-skeleton G-1 | `DisclosureDiffWorkflowGraphNode.mode_specs` の ingest map は ingest の変更キーだけを返し、`node_history` や他モードキーを昇格しない | `tests/integration/test_part_junctions.py::test_g1_mode_specs_merge_only_active_mode_fields` |
| cat2-graph-skeleton G-2 | inner route の注釈が `DisclosureDiffWorkflowState` であり、success/error/HITL rejected の全 route が正しい | `tests/integration/test_part_junctions.py::test_g2_inner_route_uses_disclosure_workflow_state_annotation` |
| cat2-graph-skeleton G-3 | compiled outer graph が initialize→pre→main→post→finalize を通過し、5キー output へ到達する | `tests/integration/test_workflow_modes.py::test_ingest_then_invoke_is_tenant_scoped_and_returns_five_key_contract` |
| cat2-graph-skeleton G-4 | inner LLM 障害は allowlist 済み `error_code` だけを outer `graph.invoke()` の戻り値へ出し、provider 例外本文を出さない。outer `get_output()` を base 実装へ戻す変異で本テストが赤になる | `tests/integration/test_workflow_modes.py::test_graph_invoke_surfaces_llm_error_code_without_provider_text`、`tests/parts/cat2-graph-skeleton/test_graph_node.py::test_on_subgraph_error_projects_allowlisted_code_without_inner_log_text` |
| output-envelope E-1 | invoke の required 5キーは欠落で raise、完備で PASS となる対テスト | `tests/integration/test_part_junctions.py::test_e1_envelope_missing_required_fields_blocks_and_complete_passes` |
| output-envelope E-2 | 実 `config/agent.yaml` の HITL 宣言を読み、FIN verdict gate の PASS/BLOCK 対を確認する | `tests/integration/test_part_junctions.py::test_e2_manifest_config_drives_disclosure_s3_pass_and_block` |
| ingest-row-pipeline I-1 | SDK `detect_pii` の Latin name / email を除去し、開示統制語彙と SDK 未対応の日本語氏名は実挙動どおり保持する | `tests/integration/test_part_junctions.py::test_i1_sdk_pii_sanitizer_masks_sdk_findings_and_preserves_japanese_name_gap` |
| ingest-row-pipeline I-2 | `normalize_for_retrieval` を書込・ScopedRetrieval の双方へ注入し、正規化後レコードを検索できる | `tests/integration/test_part_junctions.py::test_i2_normalization_is_shared_by_write_and_retrieval` |
| ingest-row-pipeline I-3 | `source_record_id` / `source_kind` / `fact_summary` の欠落が理由付き `ingest_summary.rejected[]` になる | `tests/integration/test_part_junctions.py::test_i3_required_fields_reject_with_reason` |
| ingest-row-pipeline I-5 | 3モードの scrubbed checkpoint envelope に機微本文センチネルが含まれない | `tests/integration/test_part_junctions.py::test_i5_checkpoint_sentinel_absent_from_scrubbed_envelopes_for_all_modes` |
| ingest-row-pipeline I-6 | client supplied `payload_ref` は server 発行 ref に置換され、scope 宣言キー欠落は error になる | `tests/integration/test_part_junctions.py::test_i6_forged_payload_ref_is_reissued_and_unknown_ref_fails_closed` |
| ingest-row-pipeline I-7/I-8 | 実 PayloadStore と直接接続し、constructor 注入 scope/session で解決する。state 偽装値は使わず、注入欠落は配線エラー | `tests/integration/test_part_junctions.py::test_i7_i8_real_payload_store_resolves_only_constructor_injected_context` |
| ingest-file-sanitizer F-1 | CSV/XLSX/DOCX の正当 payload が内容スニッフで通過する | `tests/integration/test_part_junctions.py::test_f1_csv_xlsx_and_docx_valid_payloads_pass_unchanged` |
| ingest-file-sanitizer F-2 | replacement-character 比率が品質ゲートを超える CSV を reject し、通常 CSV を通す | `tests/integration/test_part_junctions.py::test_f2_quality_gate_boundary_rejects_replacement_character_payload` |
| ingest-file-sanitizer F-3 | PDF reject の理由が `rejected[]` に残り、行 reject も `ingest_summary.rejected[]` に理由付きで残る | `tests/integration/test_part_junctions.py::test_f3_file_rejection_has_explicit_reason_and_row_summary_keeps_reasons` |
| scoped-retrieval R-1 | 実 sqlite3 KB へ保存したレコードが実 node で1件ヒットする | `tests/proof_of_boundary/test_pb3_real_database.py::test_r1_real_sqlite_returns_stored_scope_and_one_scoped_hit` |
| scoped-retrieval R-2 | `company_id:issuance_id` 区画 A の資料が B に見えず、宣言 scope 欠落は fail-closed | `tests/integration/test_part_junctions.py::test_r2_scope_missing_fails_closed_and_tenant_b_cannot_read_a` |
| scoped-retrieval R-3 | FIN allowlist の `source_kind` / `disclosure_section` は残し、自由文 `description` は落とす | `tests/integration/test_part_junctions.py::test_r3_projection_keeps_allowlisted_fin_fields_and_drops_free_text` |
| scoped-retrieval R-4 | hit 0 は error でなく success + `degradation_reason` となる | `tests/integration/test_part_junctions.py::test_r4_empty_hit_degrades_without_error` |
| scoped-retrieval R-5 | backend hit の全 scope key 必須・不一致 reject を部品同梱テストで固定し、実 KB は request 値でなく保存列から scope を返す | `tests/parts/scoped-retrieval/test_retrieval_node.py::test_hit_with_all_declared_scope_keys_missing_is_rejected`、`tests/proof_of_boundary/test_pb3_real_database.py::test_r1_real_sqlite_returns_stored_scope_and_one_scoped_hit` |
| scoped-retrieval FIN-H5 | 実 SQLite に複数レコードを入れ、`query` に関連する保存済み `fact_summary` が先頭になり、要約本文と score が LLM 向け projection に残る | `tests/integration/test_part_junctions.py::test_h5_query_ranks_related_fact_summary_first_across_multiple_records` |
| evidence-gate V-1 | FIN evidence_map 正常形は PASS、evidence field 欠落は BLOCK となる | `tests/integration/test_part_junctions.py::test_v1_complete_evidence_passes_and_missing_fields_block` |
| evidence-gate V-2 | `unreflected_facts` / `inconsistencies` / `review_questions` の全 LLM 自由文 path を strict policy に列挙する | `tests/integration/test_part_junctions.py::test_v2_v3_strict_paths_cover_all_fin_llm_fields_and_flag_uncited_number` |
| evidence-gate V-3 | 引用なしの数値・比較主張を strict coverage flag が検出する | `tests/integration/test_part_junctions.py::test_v2_v3_strict_paths_cover_all_fin_llm_fields_and_flag_uncited_number` |
| evidence-gate V-4 | 宣言どおりの finding は通り、未知キー・`str` 欄の dict・top-level 語彙違いは typed projection が拒否する | `tests/unit/test_prompt_response_contract.py::test_declared_contract_mechanically_builds_a_response_accepted_by_validator`、`tests/unit/test_prompt_response_contract.py::test_typed_projection_rejects_wrong_types_and_undeclared_keys` |
| llm-draft-stage L-1 | deterministic fallback の evidence_map / cited IDs がそのまま provenance gate を PASS する | `tests/integration/test_part_junctions.py::test_l1_l2_fallback_is_provenance_complete_and_non_json_fails_closed` |
| llm-draft-stage L-2 | injected LLM の非JSON応答は `status=error` となり、生文字列を下流へ流さない | `tests/integration/test_part_junctions.py::test_l1_l2_fallback_is_provenance_complete_and_non_json_fails_closed` |
| llm-draft-stage L-3 | Stage3 の evidence が main S-3 gate と post envelope を通り、5キー output に到達する | `tests/integration/test_workflow_modes.py::test_ingest_then_invoke_is_tenant_scoped_and_returns_five_key_contract` |
| llm-draft-stage L-4/L-5 | provider 障害は fallback にせず `llm_call_failed`。例外本文 marker は Stage3 result と outer `graph.invoke()` result に出ない | `tests/unit/test_prompt_response_contract.py::test_stage3_preserves_stable_llm_error_without_provider_text`、`tests/integration/test_workflow_modes.py::test_graph_invoke_surfaces_llm_error_code_without_provider_text` |
| entry-adapter N-6 | production mode→operation 対応を固定し、standalone token は read のみ、ingest/feedback/resume は 403。token read は interrupt を発火させない | `tests/integration/test_server_security_wiring.py::test_production_mode_mapping_matches_actual_side_effects`、`tests/integration/test_server_security_wiring.py::test_standalone_token_cannot_reach_write_or_resume_endpoints`、`tests/integration/test_server_security_wiring.py::test_hitl_requires_nonempty_middleware_principal_and_non_smoke_mode` |

## 層3 — 横断

| ID | FIN-C2-167 の横断 assertion | 対応テスト |
|---|---|---|
| X-1 | 本番 `config/agent.yaml` を読み込む disclosure S-3 gate の PASS/BLOCK 対を少なくとも1本維持する | `tests/integration/test_part_junctions.py::test_e2_manifest_config_drives_disclosure_s3_pass_and_block` |
| X-2 | 上記 fail-closed テストは欠落 key / verdict 文 / 非JSONという実欠陥入力で赤くなる | `tests/integration/test_part_junctions.py::test_e1_envelope_missing_required_fields_blocks_and_complete_passes`、`tests/integration/test_part_junctions.py::test_l1_l2_fallback_is_provenance_complete_and_non_json_fails_closed` |
| X-3 | `pkgutil.walk_packages` で全 concrete node の trust / S-1〜S-4 実行順を走査する | `tests/proof_of_boundary/test_pb_invoke_order.py::TestInvokeOrder::test_call_order_for_every_node` |
| X-4 | `payload_ref` 意味変更点を偽装 ref・再発行・未解決 ref の組で確認する | `tests/integration/test_part_junctions.py::test_i6_forged_payload_ref_is_reissued_and_unknown_ref_fails_closed` |
| X-5 | PB-7/PB-8、awaiting の session echo、pending の scope/caller/session 所有権、完了後解放、同一 thread の並行 claim が1件だけ成功することを確認する | `tests/proof_of_boundary/test_pb7_hitl_interrupt_propagation.py::test_pb7_hitl_interrupt_propagates`、`tests/proof_of_boundary/test_pb_hitl_checkpoint_raw_text_absent.py::test_pb8_hitl_suspend_and_resume_checkpoints_exclude_raw_payload_text`、`tests/integration/test_workflow_modes.py::test_hitl_pause_then_resume_reaches_success`、`tests/integration/test_server_security_wiring.py::test_middleware_hitl_echoes_server_session_and_binds_pending_owner`、`tests/integration/test_server_security_wiring.py::test_pending_hitl_rejects_wrong_caller_and_wrong_session_without_consuming_claim`、`tests/integration/test_server_security_wiring.py::test_pending_hitl_claim_is_single_use_under_two_concurrent_requests` |
| X-6 / FIN-M1 | 実 runtime config の `max_batch_records` / `max_free_text_chars` / `min_similarity` が request-scoped node へ配線され、上限ちょうどは通過・1超過は拒否、類似度未満の複数レコードは除外される | `tests/unit/test_config_consistency.py::test_runtime_limits_are_wired_into_request_scoped_nodes`、`tests/unit/test_runtime_input_limits.py`、`tests/integration/test_part_junctions.py::test_m1_min_similarity_filters_unrelated_records` |

## ドメイン固有の回帰対象

出力に `legal_compliance_verdict`、`issuance_approved`、`pricing` を追加しない。tenant 解決不能・未知 gate_kind・evidence field 欠落は部分成功にせず error/raise とする。PDF と xlsm は v1 で受理しない。feedback は tenant-scoped rowcount 0 を成功として扱わない。

| 項目 | assertion | 対応テスト |
|---|---|---|
| B-1 Stage4 asserted_attrs 照合 | 同一 section の共通属性キーを NFKC・trim 正規化して決定論比較し、異値だけを版間矛盾フラグにする | `tests/unit/test_disclosure_services.py::test_stage4_flags_different_asserted_attribute_values`、`tests/unit/test_disclosure_services.py::test_stage4_does_not_flag_equal_asserted_attribute_values`、`tests/unit/test_disclosure_services.py::test_stage4_does_not_treat_one_sided_asserted_attribute_as_conflict` |
| B-5 SDK PII 範囲 | PII マスクは `shared.security.pii_detector.detect_pii` の findings のみに基づく。日本語氏名の未検知は既知の受容である | `tests/unit/test_disclosure_services.py::test_sdk_detected_pii_is_masked_and_japanese_name_is_accepted_gap` |

## 実 LLM 1 回転検証

モックだけで固めた実装は実 LLM を繋いだ瞬間に動かないことがあるため、注入シームへ実 LLM クライアントを刺して 1 回 invoke し、**中身のある出力が出ること**を確認している（`gitproject-dev-flow.md` §14-1-bis）。

| 項目 | 内容 |
|---|---|
| 実施日 | 2026-08-20 |
| 使用 LLM | ローカル CLI（Azure OpenAI）を `BaseLLM` 実装として注入 |
| 注入シーム | `config[llm]` |
| 実 LLM 呼び出し | 1 回 |
| 出力 | ['facts', 'evidence_sources', 'unreflected_facts', 'review_questions', 'mode', 'gate_kind', 'missing_data_flags'] が非空。縮退理由なし |

本番の LLM は `AnthropicClient` 等で、これは検証用シーム。テンプレの `src/` は変更していない（一時テストを生成→実行→削除する方式）。CI には入れていない（ローカル CLI と鍵が要るため）。

## 🔴 未解決の所見 — 現実的な日本語ドラフトでは中身が空になる（2026-08-20 実測）

上表の ✅ は **注入シームが生きていること**（LLM に到達し、契約どおりの JSON が返ること）を示す。
ただし `tests/conftest.py` の `case_payload` が使う invoke 本文は
`"business risk_factors material_contracts"` —— すなわち **開示区分の英語 snake_case 識別子そのもの**であり、
`tests/integration/test_workflow_modes.py::test_ingest_then_invoke_is_tenant_scoped_and_returns_five_key_contract`
の本文と同一である。この入力形は Stage 1 / Stage 2 の実装をそのまま満たすため、両ステージの限界が現れない。

同じ経路に、`docs/02_design.md` §109 / §232 が入力契約として定めている
**日本語の目論見書ドラフト本文**（Word 抽出の生テキスト。「第2【事業の状況】」「重要な契約」「事業等のリスク」を含む）を
流したときの実測結果:

| 観測 | 値 |
|---|---|
| `degradation_reason` | `no scoped retrieval hits` |
| LLM に渡った `UNTRUSTED_DATA` | `{"draft": [], "records": []}` |
| LLM 応答 | `{"unreflected_facts":[],"review_questions":[]}` |
| `output` | `facts` / `evidence_sources` / `unreflected_facts` / `inconsistencies` / `review_questions` すべて `[]` |
| `missing_data_flags` | 7 区分すべて（`business` … `use_of_proceeds`） |

最後の行が単なる無出力より重い。ドラフトには「事業の状況」「事業等のリスク」「重要な契約」が
実在するのに、**存在する区分を「欠落」と積極的に主張している**（偽の肯定）。

機序は 2 つで、いずれも決定論ステージ側にある:

1. `src/nodes/invoke_stage_nodes.py` `Stage1DisclosureStructureNode.execute()` —
   区分の有無を `if section.casefold() in lower` で判定しており、本文に
   `business` / `risk_factors` / `material_contracts` という**英語識別子が literal で現れる場合のみ**検出する。
   `docs/02_design.md` §127 は各区分に日本語名（事業の内容 / 事業等のリスク / 重要な契約 …）を割り当てているが、
   `src/` 配下に日本語の見出し語彙は 1 件も存在しない。
2. `src/services/kb_service.py` `_tokens()` / `_query_similarity()` —
   `str.split()` と `re.findall(r"[^\W_]+")` による分かち書き前提のトークン化。
   日本語は空白区切りでないため節がまるごと 1 トークンになり、
   集合積による coverage スコアが実質 0 になる。上の実測では、KB に同主題の日本語レコード
   （「主要顧客との供給契約について価格改定条項を変更する覚書を締結」）を投入しても
   `retrieval.min_similarity: 0.55` を越えず 0 件だった。

2 つは連動している。Stage 1 だけを直しても Stage 2 が 0 件のままなので縮退は解消しない。
Stage 2 の是正（既知パターンでは CJK bigram + 最低一致数）は
`retrieval.min_similarity` のスコア意味論と閾値の再較正を伴うため、**人間判断ゲート項目**として残す。
`tests/integration/test_production_retrieval_ranking.py` が現行スコアラを固定している点も併せて扱うこと。

再現手順: `tests/conftest.py` の `_live_prospectus_draft()` の戻り値と、
`_live_store_kb_and_ref` が投入する KB レコードを下記に差し替えて
`work/scripts/mrb_live_llm_check.py` を実行する
（この所見の採取時は差し替えを検証後に戻しており、コミット済みの fixture は変更していない）。

```python
# _live_prospectus_draft() の戻り値
"第一部【証券情報】第二部【企業情報】第2【事業の状況】\n"
"当社グループは、電子部品の設計・製造・販売を主たる事業としております。"
"当連結会計年度における主要顧客との取引条件の見直しに伴い、"
"重要な契約のうち一部について供給数量および価格改定条項の変更に係る"
"覚書を締結いたしました。事業等のリスクとしては、特定顧客への売上高依存、"
"原材料価格の変動、および海外生産拠点における操業リスクを認識しております。"
"なお、当該契約変更が翌連結会計年度の売上高および利益に与える影響については"
"現在精査中であります。"

# KB レコード（kb.insert_record の該当フィールド）
"fact_summary": "主要顧客との供給契約について価格改定条項を変更する覚書を締結。年間売上高への影響は約12億円と試算。",
"asserted_attrs": {"review_focus": "重要な契約"},
```

> なお、この縮退を `live_allowed_degradation_reasons` で許可して緑にしてはならない。
> `no scoped retrieval hits` は KB が空のときの正常な graceful degradation を意図した理由コードだが、
> 上の実測では **KB に該当レコードが存在した上で** 0 件になっている。

### 判定条件の切り分け（検算・2026-08-20）

上記 2 点に加えて **3 点目**がある。いずれも決定論ステージ側で、LLM は関係しない。

| # | 実測 | 出典 |
|---|---|---|
| 1 | `DISCLOSURE_SECTIONS` は英語 snake_case のみ。Stage 1 は `if section.casefold() in lower` の逐語部分一致なので、日本語ドラフトはどの区分にも当たらず**全 7 区分が gap**（＝存在する区分を「無い」と積極的に主張する偽の肯定） | `src/nodes/invoke_stage_nodes.py:23-31, 86-88` |
| 2 | `_tokens()` は `re.findall(r"[^\W_]+", flags=re.UNICODE)`。日本語は空白が無いため 1 節が**単一トークン**へ潰れる（実測: `当社の事業等のリスクおよび重要な契約について記載する` → 要素数 1）。集合積の被覆率が 0 近傍まで落ち `retrieval.min_similarity: 0.55` を割る | `src/services/kb_service.py:283-285` |
| 3 | 🔴 **`DISCLOSURE_SECTIONS`（コード内 7 件）と `config/taxonomy.yaml`（9 件）が不一致** — `related_party`（関連当事者取引）/ `subsequent_events`（後発事象）が**コード側に無い**。ingest は taxonomy.yaml で検証するので両区分のレコードは**取り込めるが、invoke では一度も所見にならない**（`FindingField("section", allowed_values=DISCLOSURE_SECTIONS)` が弾く）。docs/02 は「分類体系は `config/taxonomy.yaml` に外出しし、顧客ごとに追加・改称できる（コード変更不要）」と定めており、**実装がその契約を満たしていない** | `config/taxonomy.yaml` vs `src/nodes/invoke_stage_nodes.py:23-31` |

**日本語入力はスコープ内**である（docs/02「Word（.docx）のテキスト抽出（取締役会資料・目論見書ドラフト本体）… ✅ v1 対象」）。
したがって上記は仕様外入力に対する挙動ではなく、**v1 対象入力に対する欠陥**。

**未修正の理由**: #2 は CJK bigram + 最低一致数への変更が要り、`min_similarity: 0.55` の意味が変わるため
閾値の再較正が要る（人間判断事項）。`tests/integration/test_production_retrieval_ranking.py` が現行スコアラを固定している。
#1 だけ直しても `no scoped retrieval hits` が残るため、部分修正は AG を縮退させたまま所見を曖昧にする。
#3 は再較正と独立に直せるが、#1/#2 と同じ MR で扱うのが自然。
