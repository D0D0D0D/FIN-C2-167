# FIN-C2-167 — Design Specification

> Corporate Bond Prospectus Disclosure-Control Diff Review Agent（社債目論見書 開示統制差分レビュー・蓄積型）

---

## Position in AgentCore Architecture

- **Agent Class**: `ProspectusDisclosureReviewGraph`（`src/graph/graph.py`）
- **L1 Base**: **AgentBaseGraph**（L1 直継承。`AutonomousBaseGraph` ではない — 自律ループを持たない固定パイプライン）
- **Three-Layer Separation**:
  - **State**: flat TypedDict composition（`AgentState` を継承。プリミティブと JSON 直列化可能な型のみ）
  - **Node**: L1 inheritance（Template Method: `execute(self, state) -> dict` のみ override）
  - **Graph**: composition（`register_nodes()` で 3 スロットを埋める。`add_edges()` は override しない）

> **旧 Level 2 base 継承モデルからの移行注記**: 本テンプレートは旧 "Level 2" 基底エージェント
> （RAGAgent / DocGenerationAgent 等）を継承しない。**L1 Base**（`AgentBaseGraph`）を直接継承し、
> ドメインの複雑さは `main` スロットの `GraphNode` が包む内側 workflow graph に閉じ込める。

## Architecture Overview

### Node Configuration

| Node | Responsibility | Input State | Output State | Inherits/Overrides |
|------|---------------|-------------|--------------|-------------------|
| `initialize` | schema_version / session_id / trust_level | — | — | `InitializeNode`（framework 既定） |
| `pre_process` | モードルーティング・入力検証・**テナント fail-closed**・**S-2 入力チャネルゲート** | `user_input` | `mode` / `tenant_id` / `validated_input` / `payload_ref` | `PreProcessNode(FunctionNode)` |
| `main` | 内側 workflow graph（ingest / invoke 4 stage / feedback）の実行・**S-3 Provenance 検証** | `validated_input` | `result` / `evidence_map` / `gate_kind` ほか | `DisclosureDiffWorkflowGraphNode(GraphNode)` |
| `post_process` | 出力整形・**S-3 開示断定ガード（第 2 層）**・機微情報再スキャン・縮退理由の畳み込み | `result` | `formatted_output` | `PostProcessNode(FunctionNode)` |
| `finalize` | response_metadata / total_time_ms | — | — | `FinalizeNode`（framework 既定） |

### 実装時のクラス名整合（MR-B）

| ファイル | クラス |
|---|---|
| `src/graph/graph.py` | `ProspectusDisclosureReviewGraph(AgentBaseGraph)` |
| `src/graph/domain_workflow_graph.py` | `DisclosureDiffWorkflowGraph(BaseGraph)`（内側） |
| `src/nodes/main_node.py` | `DisclosureDiffWorkflowGraphNode(GraphNode)` |
| `src/nodes/pre_process_node.py` | `PreProcessNode(FunctionNode)` |
| `src/nodes/post_process_node.py` | `PostProcessNode(FunctionNode)` |

`config/agent.yaml` の `class:` は `ProspectusDisclosureReviewGraph`、`base_type:` は `AgentBaseGraph`。

> **AgentRegistry エントリポイント整合**: `config/agent.yaml` の `module: src.graph` /
> `class: ProspectusDisclosureReviewGraph` と、`src/graph/graph.py` が実際に export するクラス名・
> `src/api/server.py` の import は**常に一致していなければならない**。MR-A 時点で seed の `Graph` を
> `ProspectusDisclosureReviewGraph` へリネーム済み（`src/api/server.py` の import も同時更新）。
> 内側 graph・ノードの実装は MR-B。

### AGENTIC STAR Marketplace entry adapter

リポジトリ直下の `cli.py` は `ProspectusDisclosureReviewGraph` を継承する
`MarketplaceGraph` を公開する。Marketplace runner の入力は素の文字列1本であり、adapter がその文字列を
TTL・single-use の `PayloadStore` に退避して、外側 Graph が必要とする `mode=invoke`、`payload_ref`、
`session_id`、scope の envelope を構築する。入力文字列を JSON として mode 判定しないため、文字列中に
`ingest` / `feedback` が記載されていても KB 書込経路には入らない。Marketplace は read-only invoke 専用である。

runner が渡す `input_context` はマージせず破棄する。特に `conversation_history` は S-2 の検査対象外なので、
State / checkpoint に入れない。scope は `src.entry_adapter.SCOPE_KEYS` と
`STG_DEFAULT_SCOPE_ENVS` を唯一の宣言としてデータ駆動で組み立てる。全キーが環境変数に存在し、値が
非空で空白文字と `:` を含まないことを Graph 構築時に検証する。さらに `provision_secrets()` でも scope と
Azure OpenAI の必須3値を再検証するため、runner の `compile()` → `provision_secrets()` 順でも最初の invoke
より前に fail closed となる。

Marketplace の `InvocationContext` は runner の caller/trace 属性を維持しつつ、
`caller_trust_level=VERIFIED_EXTERNAL` と `hitl_allowed=False` に置換する。Marketplace には resume channel が
なく `AWAITING_HUMAN` を失敗として扱うため、`hitl.enabled: true` のドメイン設計は維持したまま interrupt
だけを入口で抑止する。

compile 前には資格情報を持たない `_AzureMarketplaceLLM` を注入する。Azure client の import、secret provider
からの接続値解決、client 生成はいずれも `complete()` / `stream()` の呼出しごとに行い、wrapper instance に
API key、endpoint、deployment、client を保持しない。LLM が生成できるのは Stage 3 の未反映候補とレビュー
質問に限られる。Stage 1 の事実構造化、Stage 2 の tenant-scoped retrieval、Stage 4 の版間矛盾フラグ、
S-3 provenance / 開示断定ガードは既存の決定論的境界をそのまま通る。

### Data Flow — 外側 backbone（固定・変更しない）

```
START → initialize → pre_process → main → {route} → post_process → finalize → END
                                    ↓ (RETRY, max 3)
                                 pre_process
```

`add_edges()` / `route()` は override しない（framework 既定）。`route()` は `main` の後に
`state["status"]`（`AgentStatus` の**値**）を読む。HITL 時は `hitl_status` と `hitl_feedback` の
**両方**が揃ったときのみ HITL ルーティングが発火する（フレームワークの HITL ルーティング契約）。

---

## 3 つの動作モード

| Mode | エンドポイント | trust | 処理 |
|---|---|---|---|
| `ingest` | `/ingest` | `VERIFIED_EXTERNAL`（DB 書込） | 社内リスク台帳・取締役会資料・財務予測前提・重要契約/訴訟記録・過年度開示を構造化 → サニタイズ → KB へ INSERT |
| `invoke` | `/invoke` | `VERIFIED_EXTERNAL` | 新規目論見書ドラフト → 4 stage パイプライン → 差分レビューブリーフ + 根拠マップ |
| `feedback` | `/feedback` | `VERIFIED_EXTERNAL`（DB 書込） | 発行体・主幹事が確定・編集したレビュー論点（採否・確定理由・学習可否）を KB へ反映 |

**全モードが同期 request-response**（非同期バッチジョブは持たない）。モードは `pre_process.execute()` 内で
`validated_input` から確定し、**業務ロジックの分岐にのみ使う。セキュリティ判定には使わない**（後述 S-2）。

### Data Flow — 内側 workflow graph（`src/graph/domain_workflow_graph.py`）

```
                      ┌── mode == "ingest"   → parse → sanitize → taxonomy_tag → kb_upsert ─────┐
START → mode_router ──┼── mode == "invoke"   → stage1 → stage2 → stage3 → [hitl_review] → stage4 ┼→ END
                      └── mode == "feedback" → validate → kb_update_reflection ─────────────────┘
                                                     ↑
                                        status == error → END（fail fast）
```

内側 graph の `route()` / `get_output()` は**自スキーマ（`DisclosureDiffWorkflowState`）で型注釈する**。
`AgentState` で注釈すると射影でカスタムフィールド（`mode` 等）が消え、全分岐が既定経路へ潰れる（Business Travel Hotel & Plan Recommendation Agent 実測）。

### invoke の 4 ステージ

| Stage | 種別 | 処理 | 出力 |
|---|---|---|---|
| **Stage 1** | 決定論（LLM 不使用） | 目論見書ドラフトの必須開示セクション（有価証券届出書の記載事項）の有無・形式を検証し、ドラフトの記載要素を構造化。**全要素に `source_id`（ドラフト内 span 参照）を付与**。加えて、S-2 でサニタイズ済みの業務本文を NFKC・casefold・token 化し、重複と句読点・レイアウトを除いた上限 4,000 文字の検索投影を作る。開示十分性の結論は出さない | `structured_draft`（`source_id` 付き要素の配列） / `section_gaps` / `structured_query`（bounded token projection） |
| **Stage 2** | 検索（決定論・**テナント内限定**） | 同一テナント（発行体 × 発行案件）の社内一次資料（社内リスク台帳・取締役会資料・財務予測前提・重要契約/訴訟）と過年度開示を KB 照合。Stage 1 の業務本文由来 `query` と保存済み `fact_summary` / 開示区分の token coverage を計算して関連順に並べ、`retrieval.min_similarity` 未満を除外する。固定 section 名だけを query にすると同一区分の全資料が同点になるため、section は本文中の他の業務語彙と同じ coverage の一部として扱う。**結論ラベルは継承しない**（サニタイズ済み `fact_summary` を含む retrieval-safe projection のみ） | `internal_sources` / `prior_disclosures` / `retrieved_source_ids` |
| **Stage 3** | LLM | ドラフトを社内一次資料に**当てはめ**、①未反映の重要事実 ②版間矛盾 ③要説明論点 を抽出。**事実と推論を分離**。法令適合の結論は生成しない | `diff_draft` / `evidence_map` / `cited_source_ids` |
| **Stage 4** | LLM + 決定論 | 差分レビューブリーフの統合（未反映候補・矛盾候補・レビュー質問）＋**版間矛盾フラグ（決定論算出）**＋根拠マップ統合＋欠落データフラグ | `review_brief` / `inconsistency_flags` / `missing_data_flags` |

> **版間矛盾フラグは決定論で算出する**（LLM に判定させない）。Stage 2 が返した過年度開示の
> 構造化属性（財務前提・重要契約条件・リスク記述の版）と当版ドラフトの対応要素を機械照合して
> 矛盾候補を導く。LLM が担うのはその事実の**言語化**のみ。開示統制に直結する判断を生成に委ねない。

---

## 🔴 v1 の入力スコープ

| 入力形式 | v1 | 理由 |
|---|---|---|
| **CSV / Excel（.xlsx）の構造化エクスポート**（社内リスク台帳・取締役会議事の構造化データ・財務予測表） | ✅ **v1 対象** | 社内一次資料は台帳・スプレッドシート様式が実在。構造化された状態で受け取るのが最も欠落・誤読が少ない |
| **Word（.docx）のテキスト抽出**（取締役会資料・目論見書ドラフト本体） | ✅ **v1 対象** | 目論見書ドラフト・取締役会資料は Word 様式が一般的。`.docx` は ZIP + XML でテキスト抽出は決定論で可能（マクロは実行しない・埋め込みオブジェクトは読まない） |
| **PDF（テキストレイヤあり）** | ⚠️ **v1.1 候補（要ユーザー判断）** | 抽出自体は可能だが `/JavaScript` `/OpenAction` 等の攻撃面を持ち込む（入力チャネルのサニタイゼーション方針）。受理するなら S-2 で拒否が必須 |
| **PDF（スキャン画像）・紙** | 🚫 **スコープ外** | OCR が必要。OCR 誤読が財務数値・契約条件を壊すと開示統制リスクに直結する。**受け付けない＝攻撃面も誤読リスクも構造的に持たない** |

> **設計原則**: 受理しない形式は攻撃面を持たない。v1 は「顧客が保有する構造化データ・Word 文書」に絞る。
> KB がゼロでも必須開示セクションテンプレート準拠のドラフト骨子＋`source_id` 引用は動作する（グレースフルデグレード）。

---

## Design 入場条件 #1 — 開示統制分類体系と KB スキーマ

### 分類体系（初期セット）

再利用の軸は **開示区分 × 資料種別 × 差分種別 × 重要度**。これは**同一テナント（自社の当該発行案件）内の
検索インデックス**であって、ある発行体の非公開事実を無関係な発行体へ移植する一般化ではない。

| 軸 | 初期値（`config/taxonomy.yaml`） |
|---|---|
| `disclosure_section` | `business`（事業の内容） / `risk_factors`（事業等のリスク） / `mdna`（財政状態・経営成績の分析） / `financial_statements`（経理の状況） / `related_party`（関連当事者取引） / `material_contracts`（重要な契約） / `litigation`（訴訟・係争） / `subsequent_events`（後発事象） / `use_of_proceeds`（手取金の使途） |
| `source_kind` | `risk_register`（社内リスク台帳） / `board_material`（取締役会資料） / `financial_forecast`（財務予測前提） / `material_contract`（重要契約） / `litigation_record`（訴訟記録） / `prior_disclosure`（過年度開示） |
| `diff_kind` | `unreflected`（未反映） / `inconsistency`（版間矛盾） / `needs_explanation`（要説明） |
| `severity_class` | `material`（重要事実相当） / `significant`（重要） / `minor`（軽微） |

分類体系は `config/taxonomy.yaml` に外出しし、**顧客（発行体）ごとに追加・改称できる**（コード変更不要）。
`schema_owner` = 顧客開示統制責任部門。未知の値は `unclassified` に落として `missing_data_flags` に載せる
（推測で埋めない）。

### KB スキーマ（サニタイズ後構造化データのみ）

#### `bond_issuances`

| Field | Type | 備考 |
|---|---|---|
| `issuance_id` | str | KB 内 ID（テナント内一意） |
| `tenant_id` | str | **`{company_id}:{issuance_id}`**（発行体 × 発行案件） |
| `source_record_id` | str | **元システムのレコード ID（非改ざん参照）** — 正本との整合追跡用。本エージェントは顧客システムを書き換えない |
| `filing_type` | str | 有価証券届出書 / 発行登録追補書類 等の区分 |
| `draft_version` | str | ドラフトの版（版間矛盾検出のキー） |
| `ingested_at` | datetime | |

#### `internal_source_registry`

| Field | Type | 備考 |
|---|---|---|
| `source_id` / `tenant_id` | str | |
| `issuance_id` | str | FK → `bond_issuances` |
| `source_kind` | str | 分類体系（上表） |
| `disclosure_section` | str | 対応する開示区分（想起用） |
| `fact_summary` | str | **サニタイズ済み**の事実要約（担当者氏名 → 役割表記・生の機微財務値は要約のみ） |
| `asserted_attrs` | dict | 照合用の構造化属性（例: 契約条件区分・訴訟ステータス区分・財務前提の版） |
| `provenance` | str | 資料の出所・版・確認日 |
| `quality_flag` | str | 記録品質（`high` / `low` — 低品質は検索重みを下げる） |
| `source` | str | `ingest` / `feedback` |

#### `prior_disclosures`

| Field | Type | 備考 |
|---|---|---|
| `disclosure_id` / `tenant_id` | str | |
| `issuance_id` | str | FK → `bond_issuances` |
| `disclosure_section` | str | 開示区分 |
| `version` | str | 過年度開示の版 |
| `asserted_attrs` | dict | 版間矛盾照合用の構造化属性（財務前提・契約条件・リスク記述の要旨区分） |
| `disclosed_on` | date | 開示日 |

> **原文ファイル（目論見書ドラフト・取締役会資料本体）は保存しない。** 原本は顧客システムにある（正本）。
> 本エージェントの KB は**正本性を持たない派生コピー**であり、サニタイズ後の構造化データのみを持つ。

---

## Design 入場条件 #2 — `/ingest` の最小データ要件とパース方式

### 最小データ要件

| 記録種別 | 必須フィールド | 欠落時 |
|---|---|---|
| 社内一次資料 | 資料 ID（`source_record_id`）・資料種別・事実要約 | **reject**（この 3 つが無ければ KB レコードとして成立しない） |
| 社内一次資料（推奨） | 対応開示区分・出所/版・照合用属性 | `unclassified` + `missing_data_flags` に記録して INSERT（検索精度は落ちるが蓄積は進む） |
| 過年度開示 | 開示 ID・開示区分・版 | **reject** |

### パース方式（`src/services/record_parser.py`）

| 形式 | 方式 |
|---|---|
| CSV | `csv` 標準ライブラリ。**列マッピングは `config/ingest_mapping.yaml`** で顧客ごとに定義。数式インジェクション中和は S-2 |
| Excel（.xlsx） | `openpyxl`（`read_only=True`・**数式は評価せず文字列として読む**）。マクロ有効ブック（.xlsm）は **reject** |
| Word（.docx） | ZIP + `word/document.xml` のテキスト抽出（**`defusedxml`** でパース）。マクロ・埋め込みオブジェクト・外部参照は読まない |
| PDF | ⚠️ v1.1 候補（🔴 判断項目）。受理する場合は `/JavaScript` `/JS` `/OpenAction` を含むファイルを **reject** |

**LLM は構造化の補助にのみ使う**（自由記述からの分類推定）。必須フィールドの抽出は決定論で行い、
LLM が失敗しても必須フィールドが取れていれば INSERT は成立する。

---

## Design 入場条件 #4 — 差分レビュー報告テンプレート（`config/report_templates/`）

`disclosure_diff_review.yaml` — 差分レビューブリーフの必須構成要素:

| セクション | 内容 | 生成方針 |
|---|---|---|
| 1. 対象ドラフトの識別 | 発行案件・届出書区分・ドラフト版 | Stage 1（決定論） |
| 2. 必須開示セクションの有無 | 開示区分ごとの記載有無・形式 | Stage 1（決定論・断定しない） |
| 3. 未反映の重要事実（`unreflected_facts`） | 社内資料にあり、ドラフトに反映されていない候補 | Stage 3（LLM・`source_id` 引用必須） |
| 4. 版間矛盾（`inconsistencies`） | 過年度開示・社内資料と当版ドラフトの矛盾候補 | Stage 3 + Stage 4（決定論算出） |
| 5. 要説明論点（`review_questions`） | 説明が不足しており人間確認を要する論点 | Stage 3（**質問として提示・断定しない**） |
| 6. 根拠マップ | 記述 → `source_id` / 社内資料 ID の対応表 | Stage 3/4（必須構造） |

**本テンプレートには開示の法令適合判定・発行承認・価格決定に相当するフィールドを一切定義しない**（S-3 第 1 層）。

出力契約（固定フィールド）: `facts` / `evidence_sources`（社内資料リンク） / `unreflected_facts` /
`inconsistencies` / `review_questions`。`legal_compliance_verdict` / `issuance_approved` / `pricing` は持たない。

---

## State Definition

`src/schemas/state.py` — `AgentState` を継承した flat TypedDict。

| Field | Type | Purpose | Required |
|-------|------|---------|----------|
| `mode` | `str` | `ingest` / `invoke` / `feedback` | ✅ |
| `tenant_id` | `str` | `{company_id}:{issuance_id}` — **fail-closed で解決** | ✅ |
| `validated_input` | `str` | 内側 graph への JSON 直列化入力 | ✅ |
| `payload_ref` | `str` | **生の資料本文/ドラフト本文への不透明参照**（本文は State に載せない） | ✅ |
| `structured_query` | `str` | Stage 1 がサニタイズ済み業務本文から作る、上限 4,000 文字の正規化 token 投影（原文の句読点・レイアウト・重複を保持しない） | invoke |
| `gate_kind` | `str` | **S-3 の経路宣言**（`ingest` / `draft` / `feedback`） | ✅ |
| `result` | `dict` | 差分レビューブリーフ | invoke |
| `evidence_map` | `list[dict]` | 記述 → 根拠 の対応表（S-3 の照合対象） | invoke |
| `cited_source_ids` | `list[str]` | LLM が引用した ID 集合 | invoke |
| `retrieved_records` | `list[dict]` | Stage 1/2 が返した**実レコード**（`source_id` + 照合用属性。**S-3 照合の正本**）。生本文は載せない | invoke |
| `inconsistency_flags` | `list[dict]` | 版間矛盾フラグ（決定論算出） | invoke |
| `missing_data_flags` | `list[str]` | 欠落データ | — |
| `requires_human_review` | `bool` | 根拠対応が不完全な主張がある場合 `true` | — |
| `degradation_reason` | `str` | 縮退理由（KB ゼロ・LLM 不在等） | — |
| `formatted_output` | `dict` | post_process の最終出力（下記の出力契約 5 キーを内包する named schema） | — |

### 出力契約（`formatted_output` / `result` 内の固定 named schema）

State には生の中間フィールド（`result` / `evidence_map` / `retrieved_records` / `inconsistency_flags` 等）を
flat に持つが、**呼び出し元に返す最終出力（`formatted_output`）は以下の 5 キーを必須構造として持つ**。
Stage 4 / post_process がこの named schema に組み立てる（`legal_compliance_verdict` / `issuance_approved` /
`pricing` に相当するキーは**定義しない** — S-3 第 1 層の構造制約）。

| 出力キー | 由来（内部フィールド → 出力） | 内容 |
|---|---|---|
| `facts` | `structured_draft`（Stage 1 の `source_id` 付き構造化要素） | ドラフトから抽出した事実要素 |
| `evidence_sources` | `retrieved_records` / `cited_source_ids`（Provenance ゲートの正本） | 各論点に紐づく社内資料リンク（`source_id` + 照合用属性） |
| `unreflected_facts` | Stage 3 の未反映抽出（社内資料にありドラフトに未反映の候補） | 未反映の重要事実候補 |
| `inconsistencies` | `inconsistency_flags`（Stage 4 の決定論算出） | 過年度開示・社内資料と当版ドラフトの版間矛盾候補 |
| `review_questions` | Stage 3 の要説明論点 | 説明不足で人間確認を要するレビュー質問 |

> `evidence_map`（claim → source 対応）は S-3 Provenance ゲートの機械照合用の内部構造であり、
> `evidence_sources` はそれを人間向けに整形した出力キーである（両者は責務が異なる）。

**State Constraints（必須）:**
- flat TypedDict のみ（プリミティブ + JSON 直列化可能な型）。ADR-005 の禁止型は使わない
- **JWT / API キー / 認証情報を State に入れない**（checkpoint は DB に永続化される）
- `InvocationContext` は **`InvocationContext.from_state(state)`** で取得する。**直接構築禁止**
- **資料・ドラフトの生テキストを State に置かない** — `payload_ref`（TTL 付き transient store の
  不透明参照）で間接化する（SubcontractorQuotationDisputeHistory `TransientPayloadStore` パターン）。実体解決は service 層 / API アダプタのみ
- Stage 2 の意味的類似検索には例外として `structured_query` を渡すが、これは S-2 後の本文から作る
  **上限付き token 投影**であり、原文本文・句読点・レイアウトを checkpoint に複製しない
- `status` は必ず **`AgentStatus.<X>.value`**（文字列）で返す（bare Enum を返さない）
- **HITL 系フィールド（7 種）は `AgentState` から継承する。本テンプレートで再宣言しない**
  （フレームワークの HITL 状態契約）。書き込んでよいのは **`hitl_draft` のみ**（冪等性ガード。`hitl_count` /
  `hitl_allowed` は framework / Gateway 専用）

---

## Framework Utilization

- [x] `InvocationContext`（`from_state()` 経由。correlation_id / session_id / permissions / **tenant 解決の入力**）
- [x] `SecurityViolationError`
- [x] S-2: `_extra_security_gate_input()` — 入力チャネルのサニタイズ
- [x] S-3: `_extra_security_gate_output()` — **Provenance 検証（GraphNode）** + **開示断定ガード（PostProcessNode）**
- [x] S-4: `emit_trace_event()`（`shared.utils.audit_logger`）— 各 `execute()` 内で最低 1 回のドメインイベント
- [x] `ctx.secrets.require()` — DB 接続情報等（`os.environ` は使わない）

---

## S-1 設計 — Trust Level

| Node | `required_trust_level` | 根拠 |
|---|---|---|
| `PreProcessNode` | `VERIFIED_EXTERNAL` | 外部から機微な社内資料・ドラフトを受け取る |
| `DisclosureDiffWorkflowGraphNode`（main） | `VERIFIED_EXTERNAL` | LLM を呼び、**`ingest` / `feedback` では KB に書き込む** |
| `PostProcessNode` | `VERIFIED_EXTERNAL` | 差分レビューブリーフを出力する |

- 全 `FunctionNode` サブクラスは **class body で `required_trust_level` を明示宣言**する
  （暗黙の `ANONYMOUS` 継承は不可 — `gate-trust-level-check`）
- **`GraphNode` は既定が `ANONYMOUS`** のため明示宣言が必須（SubcontractorQuotationDisputeHistory で実バグ検出）
- 有効値は `ANONYMOUS` / `VERIFIED_EXTERNAL` / `INTERNAL` の 3 つのみ（他の値は import 時 AttributeError）

---

## S-2 設計 — 入力チャネル・サニタイゼーション

入力チャネル・パターンの **strict** 区分に該当する。
本エージェントは**ファイルアップロードチャネル**（社内資料エクスポート・ドラフト文書）を持つ。

### 🔴 実行順制約 — チャネル判定を `mode` に依存させてはならない

framework の実行順は以下であり、`mode` を書くのは `execute()` である:

```
S-1 → S-4 node_start → S-2 (_extra_security_gate_input) → execute() → S-3 → S-4 node_complete
                        ↑ ゲートはここで走る               ↑ mode はここで初めて書かれる
```

> 🔴 **したがって「S-2 で `mode == "ingest"` なら XX を検査する」という設計は成立しない。**
> ゲート実行時点で `mode` は state に存在せず、分岐が偽になって**検査が丸ごとスキップされる**
> （SoilInvestigationNeighbouringCaseReferenceAgent で実機確認済みの fail-open 実バグ）。

### 正しい設計 — 入力の中身から判別する（`mode` 非依存・fail closed・allowlist）

`PreProcessNode._extra_security_gate_input()` は **state の `mode` を読まない**。生の入力そのものを見て判定する。
共有サニタイザは `src/services/sanitizer.py` に集約する。

| チャネル | 脅威 | 対策 |
|---|---|---|
| **CSV** | 数式インジェクション（`=` `+` `-` `@` 始まりのセル） | 該当セルを `'` でプレフィックスして中和 |
| **Excel（.xlsx）** | 同上 + マクロ | `openpyxl` は数式を評価しない設定で読む。**.xlsm（マクロ有効）は reject** |
| **Word（.docx）** | XML 外部実体（XXE）・埋め込みオブジェクト | **`defusedxml`**（`forbid_entities=True` / `forbid_external=True`）。マクロ・埋め込みオブジェクトは読まない |
| **PDF**（受理する場合） | `/JavaScript` `/JS` `/OpenAction` | 検出したら **reject**（除去ではなく拒否 — 資料の完全性を毀損しないため） |
| **未知の形式** | — | 🚫 **allowlist で reject**（素通りさせない） |
| **巨大ペイロード** | checkpoint 肥大・DoS | `PreProcessNode` が `config/config.yaml` の `max_file_bytes` / `max_batch_records` を S-2 境界で実適用し、超過を fail closed で拒否する |
| **自由記述フィールド** | プロンプトインジェクション（資料の自由記述が Stage 3 の LLM プロンプトに入る） | untrusted として扱い、**指示文とデータを分離**。`PreProcessNode` が全 payload 文字列 leaf に `max_free_text_chars` を適用する |

**形式判定は入力の中身（マジックバイト / MIME スニッフィング）から行う**。
クライアント申告の `Content-Type` やファイル拡張子は信用しない。

### 機微データサニタイズ（非公開財務情報・個人特定情報への配慮）

社内資料には**担当者氏名・取締役氏名・未公表の生の財務数値・M&A 検討情報**が含まれ得る。これらを **KB に生で残さない**。

- SDK が検知する氏名・連絡先はマスクする。SDK が検知しない日本語氏名は現実装では自動置換しないため、
  upstream の構造化 export で役割表記へ変換してから投入する（残余リスクはテストで固定する）
- 生の未公表財務数値は**要約・区分レベル**に落とす（照合には `asserted_attrs` の構造化区分を用いる）
- **解決不能なら reject**（fail closed）
- ⚠️ **SDK の `pii_detector` は日本語氏名を検知しない**（CorporateGroupTripPlannerAgent 実測）。現実装の
  `src/services/pii_sanitizer.py` は SDK finding のマスクを **① S-2 入力時 ② KB 書込直前
  ③ S-3 出力再スキャン ④ HITL resume 応答**の 4 点で適用するが、日本語氏名の補完検出は行わない。
  したがって upstream の役割表記化を導入条件とし、未処理の日本語氏名を含む入力を許容する点を残余リスクとする

`FunctionNode._security_gate_input()` は `@final` — **override 禁止**。拡張は `_extra_security_gate_input()` フックのみ。

---

## S-3 設計 — 二重ゲート（**critical**）

### 第 1 層 — Provenance ID 機械検証（`DisclosureDiffWorkflowGraphNode._extra_security_gate_output()`）

**LLM が引用した社内資料 ID・過年度開示 ID・`source_id` が実在し、かつ主張が実レコードと整合するかを機械照合する。**

#### 実装位置は **subgraph 境界**（`PostProcessNode` に置いてはならない）

`GraphNode.merge_output()` が outer state へ書き戻すキーしか post_process には届かない。
照合の**正本である `retrieved_records`（Stage 1/2 の実レコード）**を outer state に昇格させない限り、
post_process では検証できない。

`BaseNode.__call__()` は `_security_gate_output()` を**無条件に実行**し、`GraphNode._security_gate_output()` は
既定スキャンこそ no-op だが **`_extra_security_gate_output()` へ委譲する**（framework 1.0.0 実機検証済み）。

#### ✅ 正しい実装 — 経路を明示宣言（`gate_kind`）し、宣言が無ければ error に倒す

`merge_output()` が `mode` から `gate_kind`（`ingest` / `feedback` / `draft`）を明示宣言する。
`draft` 経路では `cited_source_ids` / `retrieved_records` / `evidence_map` の**欠落を「検査不要」と解釈せず fail closed で raise**する。

| 検証 | 不一致時の挙動 |
|---|---|
| 引用 ID が Stage 1/2 出力に**実在しない**（捏造） | 🔴 **fail-closed `raise`** |
| 引用 ID は実在するが、主張が言う**属性が実レコードと不一致**（＝「実在するが無関係な `source_id`」） | 🔴 **fail-closed `raise`**（誤帰属） |
| 財務数値・契約条件・版が引用元レコードの該当フィールドと不一致 | 🔴 **fail-closed `raise`** |
| ブリーフ本文の主張に `evidence_map` エントリが**存在しない**（根拠なしの記述） | 🟡 `requires_human_review = true` を付与し、**HITL レビューへ強制ルーティング** |

`evidence_map` は claim → source の対応を必須構造として持つ（`claim_id` / `claim_text` / `source_ids` / `asserted_attrs`）。

> 🔴 **照合の正本は ID の集合ではなくレコード実体である。** `retrieved_records`（Stage 1/2 が返した
> 実レコードの構造化配列・サニタイズ済み照合用属性のみ）を昇格し、ゲートはそれを唯一の正本として照合する。
> S-3 のためだけに KB を再フェッチしない。生の資料本文は載せない。

> **役割分担の明文化**: Provenance 検証の守備範囲は「捏造 ID・誤帰属の機械遮断」である。
> **差分解釈そのものの正しさは検証できない** — それは根拠マップ経由の人間（発行体・主幹事・弁護士）レビューが担保する。

### 第 2 層 — 開示断定ガード（構造 → lexicon の順）

| 層 | 実装 | 内容 |
|---|---|---|
| **層 1（構造）** | **出力スキーマ** | 出力欄は「**社内資料の事実＋未反映候補＋矛盾候補＋要説明論点**」のみ。**法令適合判定・発行承認・価格決定に相当するフィールドを出力スキーマに一切定義しない**。禁止フィールド名の不在を schema テストで assert する |
| **層 2（lexicon）** | `PostProcessNode._extra_security_gate_output()` + `src/services/disclosure_verdict_gate.py` | 自由文中の断定表現（「重要な虚偽記載なし」「開示は適正」「発行可能」「投資判断上問題ない」等）を lexicon で検知。**NFKC + casefold 正規化 → 文単位分割 → 該当文を非断定形へ実置換**。**全文が該当する場合は `AgentStatus.ERROR.value`**（fail closed） |

- lexicon は `config/disclosure_verdict_lexicon.yaml`。**`schema_owner` = 顧客開示統制責任部門**・版管理付き
- 「テンプレに追記するだけ」は過去レビューの Critical 指摘で捕まった前例がある — **文単位の実置換を実装する**

`FunctionNode._security_gate_output()` は `@final` — **override 禁止**。

---

## テナント統制（fail-closed・検索段まで貫通）

| 箇所 | 挙動 |
|---|---|
| `PreProcessNode.execute()` | `InvocationContext.from_state(state)` から `company_id` / `issuance_id` を解決 → `tenant_id`。**解決不能なら `status = AgentStatus.ERROR.value`**（fail closed） |
| Stage 2 KB 検索 | 全クエリに `tenant_id` フィルタを**必ず**適用。**既定テナントへのフォールバック禁止・クロステナント学習なし** |
| `ingest` / `feedback` の INSERT/UPDATE | `tenant_id` 必須。他テナント（他発行体・他案件）の行は書き換えられない |

PB テストで **2 テナント分の KB をシードし、テナント A のクエリがテナント B の資料を一切返さないこと**を assert する。

---

## Composition Pattern

- **Pattern**: **GraphNode（subgraph）** — `main` スロットに `DisclosureDiffWorkflowGraphNode` を配置
- **Composition target**: `src/graph/domain_workflow_graph.py` の `DisclosureDiffWorkflowGraph`（内側・`BaseGraph` 継承）
- **Error propagation strategy**: **`propagate`**（既定）— 内側のエラーは再送出し fail fast。
  開示統制レビューは**誤った部分結果を返すより失敗させるべき**領域である
- **`propagate_hitl`**: **`True`** — HITL は内側 workflow graph の draft レビューで発火するため、
  外側の呼び出し元（レビュー担当）まで伝播させる必要がある

### GraphNode の 3 つの `@abstractmethod`

| Method | 実装 |
|---|---|
| `get_subgraph()` | **`__init__` でキャッシュした内側 graph を返す**。毎 `execute()` で新規生成すると HITL resume が無限ループする（BusinessDiningAndEntertainmentPlannerAgent 実測） |
| `extract_input(state)` | `state["validated_input"]`（`mode` / `tenant_id` / `payload_ref` を含めて JSON 直列化した**文字列**） |
| `merge_output(state, sub_result)` | **実行されたモードのフィールドだけを返す**（`None` 埋め禁止）。`gate_kind` を明示宣言（上記 S-3） |

`error_strategy` / `propagate_hitl` / `required_trust_level` は **class body で明示宣言**する。
`InvocationContext` を `GraphNode` 内で手動構築しない（context 伝播は `GraphNode.execute()` が自動で行う）。

---

## 実装方針 — 共通部品適用（parts/ ライブラリ）

本テンプレートの実装は、写経ではなく **共通部品ライブラリ `1000AG/parts/`（正本: `aiagentasset/wamurocommonparts`）
の部品適用（`apply_part.py`）+ adaptation_points 編集**で行う。部品は GMP Manufacturing Deviation & CAPA Report Drafting Agent 等の released 実装から
抽出され、同梱テスト・過去レビュー由来の回帰テスト付きで、全部品が現行 wheel `agenticstar-agentcore==1.0.0` で緑
（`check_sdk_impact` PASS 確認済 2026-07-23）。骨格は無改変で適用し、adaptation_points のみをドメインに合わせる。

### 部品指定表（設計時に確定）

| 部品 | v | FIN-C2-167 での役割 | 主な adaptation_points |
|---|---|---|---|
| `cat2-graph-skeleton` | 0.1.6 | `main` slot GraphNode = `DisclosureDiffWorkflowGraphNode`（3モード骨格・inner/outer checkpointer 分離・並行 cold-start 保護・HITL propagate・safe error_code projection） | `mode_specs` / `_parent_config` / `error_strategy=handle` / `known_failure_codes` / `propagate_hitl=True` |
| `output-envelope` | 0.1.2 | `post_process` = `PostProcessNode`（出力契約 5 キーの named schema 整形・gate_kind 宣言・degradation 畳み込み） | `format_*` / `required_fields_by_mode` / `_sanitize_client_echoes` |
| `ingest-file-sanitizer` | 0.1.1 | S-2 ファイルチャネル allowlist・マジックバイト判定・テキスト品質ゲート | `allowed_channels`={csv,xlsx,docx} / `max_file_bytes` |
| `ingest-row-pipeline` | 0.3.1 | `/ingest` 行パイプライン + 全 scalar checkpoint scrub。`trusted_scope`/`session_id` は認証後にコンストラクタ注入 | required fields / sanitize・normalize・write hooks / `CheckpointScrubPolicy` |
| `scoped-retrieval` | 0.3.0 | Stage 2 = fail-closed scope 検索。各 hit は保存レコード由来 `company_id`/`issuance_id` を必須投影 | `scope_keys` / 実 SQLite backend / safe allowlist |
| `evidence-gate` | 0.3.1 | Provenance 検証 + LLM finding の型付き allowlist 射影 | `ProvenanceContract` / `FindingField` / `project_typed_finding(s)` |
| `llm-draft-stage` | 0.2.0 | Stage 3。LLM 未設定だけ deterministic fallback、provider 障害は安定 `error_code` で fail-closed | `config['llm']` / prompt・fallback・evidence hooks |
| `payload-store` | 0.4.0 | scope/session 束縛 store。digit-free ref、`resolve(..., consume=True)` のみ | `scope_keys`=(company_id,issuance_id) / `ttl_seconds` |
| `entry-adapter` | 0.4.0 | operation-aware 認証。token/STG default は read-only、write/resume は middleware identity 必須 | `SCOPE_KEYS` / mode→`Operation` mapping / `AuthContext` |

> **役割分担**: 部品選定は本表で確定済み。
> **実装**: adaptation_points のみ編集・骨格無改変／**検証**: push 前敵対レビュー + 実機検証。
> 写経忠実性は `parts/tools/verify_part.py`（🔴SUSPECT=骨格変更疑い→即差し戻し）で機械照合する。

> **部品でカバーされない glue（テンプレ側で実装）**: `src/schemas/state.py`（State/DisclosureDiffWorkflowState）・
> `src/graph/graph.py`（外側 register_nodes）・`src/graph/domain_workflow_graph.py`（内側 3分岐 graph・route を
> **自State型注釈**）・KB スキーマ SQL（`bond_issuances`/`internal_source_registry`/`prior_disclosures`。部品には SQL を入れない）・
> S-3 第2層 開示断定 lexicon（`disclosure_verdict_gate.py` + `config/disclosure_verdict_lexicon.yaml`）・
> 日本語 PII 補完サニタイザ（`pii_sanitizer.py`。SDK `pii_detector` の日本語欠落を補完）・config/report_templates・taxonomy。

> **テスト方針**: 部品同梱テスト（層1・弱体化禁止・緑維持）＋ `parts/TESTING.md` の接合部テスト項目（層2・
> cat2-graph-skeleton の G-1〜3 / output-envelope の E-1〜3 / ingest-row-pipeline の I-1〜6 等）を
> `docs/03_test_spec.md` に引き写して具体化＋横断（層3）。PB-3 は**実 DB（sqlite3）**で KB SQL を検証。

## HITL 設計（D6 `interrupt()`）

### 設定（`config/config.yaml` = ランタイム / `config/agent.yaml` = 静的マニフェスト）

```yaml
# config/config.yaml — ランタイムパラメータ
memory_enabled: true      # MANDATORY — checkpointer が無いと interrupt() の pause/resume が成立しない
hitl:
  enabled: true
  max_hitl: 12            # 安全弁（壁打ち回数の上限であって、対話を縛る目的ではない）
```

`config/agent.yaml` にも **トップレベル `hitl.enabled: true`** を宣言する（PB-7 スタブと HITL レビューが読む）。
`max_hitl` 等の**チューナブルは `config.yaml` 側のみ**に置き、二重管理しない。
2 ファイルの `hitl.enabled` 一致は `tests/unit/test_config_consistency.py` で assert する（ドリフト検知・MR-B）。

### 挙動

- **発火点**: 内側 workflow graph の `hitl_review` ノード（Stage 3 の後）。差分レビュー論点のドラフトを
  レビュー担当に提示し、修正指示を受けて refine するループ
- 🔴 **発火前ゲート**: `interrupt()` に渡す前に、Stage 3 の型付き closed projection、開示断定 lexicon、
  PII 再スキャン、`retrieved_records` を正本とする provenance 照合をすべて通す。断定文・PII・未検証
  `source_id` のいずれかを含む draft は HITL 応答へ公開せず `status=error` にする
- 🔴 **`interrupt()` は必ず `state.get("hitl_allowed", True)` でガードする**。
  無条件 `interrupt()` は **blocking defect**。`hitl_allowed=False`（親 `GraphNode` からの自動実行）のときは人間レビューを飛ばす
- **`hitl_draft`** に生成済みドラフトを保存し、resume 時の LLM 再実行を防ぐ（冪等性ガード）
- **`HitlStatus` の 3 分岐をすべて処理する**:

| `HitlStatus` | route 先 | 本テンプレートの処理 |
|---|---|---|
| `APPROVED` | `post_process` | ドラフト確定 → S-3 ゲート → 出力 |
| `CORRECTED` | `post_process` | **人間の修正を反映して refine** してから確定（無視すると修正が黙って捨てられる） |
| `REJECTED` | `finalize` | post_process をスキップし、ドラフト破棄を明示して終了 |

- **resume 応答は無加工で state に入れない** — PII/機微サニタイザを**先に**通す（scrub-first。CorporateGroupTripPlannerAgent 実測）
- 🔴 **inner / outer の checkpointer は別インスタンス**にする（BusinessDiningAndEntertainmentPlannerAgent 実測）。inner は
  `DisclosureDiffWorkflowGraphNode.__init__` で専用 saver を新規生成し、**`compile(checkpointer=...)` を明示的に呼ぶ**
- **`hitl_count` はテンプレートから書かない**（framework 専用フィールド）
- **最終文責は発行体・主幹事・弁護士**（開示の法令適合判定は自動化しない）

### API 認可境界（先行テンプレートの認可境界設計を継承）

- 🔴 **`hitl_allowed` は caller が指定できてはならない（F-01）**。`src/api/server.py` の `InvokeRequest` は
  `hitl_allowed` フィールドを**持たない**。値は認証済み `AuthContext` から決め、非空 `caller_id` を持つ
  `AuthSource.MIDDLEWARE` かつ非 STG smoke の場合だけ true とする。standalone token / STG default の read は
  `/resume` 権限を持たないため interrupt を発火させず、回収不能 thread を作らない
- 🔴 **`/resume` は `authenticate(..., operation=Operation.RESUME)` + 所有権検証を必ず通る（F-02）**。
  request-scoped graph は認証後に構築し、suspend 中だけ lock 付き pending registry が保持する。pending ownership は
  scope + middleware `caller_id` + server が確定した `session_id` の厳密一致で束縛する。awaiting 応答は
  `session_id` を返し、`ResumeRequest` はその echo を必須とする。
  claim は single-use なので同一 thread の並行 resume は 1 件だけ通り、成功後は registry を解放して checkpoint の
  pending 状態も空であることを確認する。`ResumeRequest` の scope と認証済み scope、checkpoint の `tenant_id` を突合する。
  **`company_id`/`issuance_id` に `:` を含む値は 403 で拒否**（`f"{company_id}:{issuance_id}"` の曖昧結合の悪用経路を閉じる）
- 現行 template の `InMemorySaver` と pending registry は process-local である。複数 worker 本番構成では、sticky routing
  または scope 所有権付き durable checkpoint/pending store へ置換するまで resume の可用性を保証しない

---

## Design 入場条件 #5 — 派生 KB のデータ統治契約

| 項目 | 契約 |
|---|---|
| **正本性** | 記録の正本は**顧客システム**（社内リスク台帳・取締役会資料の原本）。本エージェントの KB は検索支援用の**正本性を持たない派生コピー**。原文ファイルは保持しない |
| **許可フィールド** | 上記 KB スキーマに列挙したフィールドのみ |
| **禁止フィールド** | 担当者/取締役氏名・個人特定自由記述・生の未公表財務数値・原文ファイル本体・認証情報。**法令適合判定/発行承認フィールド**も持たない |
| **テナントキー** | `tenant_id = {company_id}:{issuance_id}`。全テーブルに必須・全クエリでフィルタ |
| **元システムへの参照** | `source_record_id` を**非改ざん参照**として保持（本エージェントは顧客システムを書き換えない） |
| **保持期間・訂正・削除** | 顧客開示統制責任部門が指定。KB レコードは `source_record_id` 単位で削除可能な API を持つ |
| **監査** | `ingest` / `feedback` の全書込で `emit_trace_event()`（誰が・どのテナントに・どのレコードを） |
| **導入前提（正直な明記）** | 本エージェントは開示書類を直接生成・提出するシステムではないが、**顧客の内部統制評価の対象になり得る**。「バリデーション不要」とは主張しない |

---

## LLM 注入シーム

- `src/graph/graph.py` が `config.get("llm")` で LLM クライアントを取り出し、ノードへ**コンストラクタ注入**する
- entry adapter の認証後に request-scoped graph を構築し、同じ `AuthContext.scope` と server-owned `session_id` を
  pre-process / payload resolver / ingest pipeline へコンストラクタ注入する。共有 node へ state から後付けしない
- **テンプレート内にモデル ID（`claude-...` 等）や具体クライアント実装を書かない**
- LLM 不在時は **deterministic fallback**（テンプレート準拠のブリーフ骨子＋`source_id` 引用のみ）で
  `status = AgentStatus.SUCCESS.value` + `degradation_reason` を返す（KB ゼロ時も同様のグレースフルデグレード）
- provider/auth/config/runtime 障害は fallback にせず `status=error` + 安定 `error_code` とし、例外本文を state/trace に残さない
- Stage 3 の受理キー・型・section enum・件数/文字数上限を `FindingField` 等のモジュール定数に置き、同じ宣言から
  `RESPONSE_CONTRACT` を生成して prompt に埋め込む。未知キー・型違いは顧客出力前に fail-closed で拒否する

## Import Isolation Confirmation

- [x] Level 0（`agenticstar` SDK）を import しない
- [x] import 先は `framework/` と `shared/` のみ
- [x] 他テンプレートを import しない（正本パターンは**写経**する）

## Test Design（`docs/03_test_spec.md` で詳細化）

| 種別 | 主要項目 |
|---|---|
| TC | TC-01〜TC-08。**TC-08 は `node(state)` で呼ぶ**（S-1 バイパスの偽緑を防ぐ） |
| PB-1〜PB-6 | 全テンプレ必須 |
| **PB-7** | **HITL 有効のため必須**（auto-waive されない）。`interrupt()` → `GraphInterrupt` 伝播 / `hitl_allowed=False` ガードで interrupt が発火しないこと |
| PB（ドメイン） | ① 機微/PII 混入入力（CSV/Excel/Word）→ 役割表記置換 or reject ② **KB 全レコードに氏名・生財務数値パターンが存在しない** ③ State/checkpoint に生テキスト非格納 ④ テナント検索分離（A のクエリが B を返さない・解決不能は ERROR） ⑤ **出力スキーマに法令適合判定/発行承認フィールドが存在しない** ⑥ 断定表現の lexicon 検知・文単位置換 ⑦ **Provenance: 捏造 ID → raise** ⑧ **属性不一致（実在するが無関係な `source_id`）→ raise** ⑨ **`gate_kind` 欠落・引用フィールド欠落 → raise**（fail closed） ⑩ invoke → feedback の E2E 配線 |
| unit | `test_config_consistency.py` — `agent.yaml` と `config.yaml` の `hitl.enabled` 一致（ドリフト検知） |
| PB-3 | **実 DB（sqlite3）で KB の SQL を実行**（`IN` の複数 id プレースホルダ展開・テナント分離・fail-closed 契約。Fake では検出できない SQL 実バグを担保） |

## Design Decision Record

| Decision | Option A | Option B | Chosen | Rationale |
|----------|----------|----------|--------|-----------|
| L1 base type | AgentBaseGraph | AutonomousBaseGraph | **AgentBaseGraph** | 固定パイプライン。自律的な次アクション決定は不要 |
| Composition pattern | Standalone | GraphNode（subgraph） | **GraphNode** | 3 モード × invoke 4 stage を 3 スロットに収めるため、ドメインの複雑さを内側 graph に閉じ込める（Cat 2 標準） |
| Error propagation | propagate | handle | **propagate** | 開示統制レビューは誤った部分結果より失敗が正しい |
| S-3 Provenance の実装位置 | PostProcessNode | GraphNode 境界 | **GraphNode 境界** | 照合の正本（Stage 2 出力）が post_process に届かない |
| 開示断定の防止 | lexicon のみ | 構造（スキーマ）+ lexicon | **構造 + lexicon** | lexicon 単層は言い換えで迂回可能。判定フィールド自体を持たない構造制約が最も強い |
| 宣言済み入力・検索制限 | docs/config から削除 | runtime へ配線 | **runtime へ配線** | バッチ/自由文上限は checkpoint 肥大・注入面積を抑える S-2 契約、類似度閾値は無関連資料を LLM に渡さない検索契約であり、削除すると設計上の安全性と中核価値を弱めるため |
| 版間矛盾フラグ | LLM 判定 | 決定論算出 | **決定論算出** | 開示統制に直結する判断を LLM の生成に委ねない。LLM は言語化のみ |
| v1 入力形式 | PDF 含む全形式 | CSV/Excel/Word（構造化エクスポート・文書） | **CSV/Excel/Word**（PDF は 🔴 判断項目） | 受理しない形式は攻撃面を持たない。OCR 誤読は開示統制リスクに直結 |
| 核データソース | 公開規則（EDINET・開示府令） | 発行体の非公開社内一次資料 | **非公開社内一次資料** | 公開情報だけを核に据えると差別化が痩せるため、価値を公開情報側に逃がさない |

---

*設計着手時の未決事項のうち、本ドキュメントで解決したものは上記のとおり。社内資料 export の統治契約と PoC 検証計画は導入時に決めるべき事項として残る。*
