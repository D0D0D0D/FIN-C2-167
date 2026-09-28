# FIN-C2-167 — Corporate Bond Prospectus Disclosure-Control Diff Review Agent

> **Category**: Cat 2 (domain workflow)
> **Industry**: FIN

## Overview

Corporate Bond Prospectus Disclosure-Control Diff Review Agent compares a prospectus draft against
the issuer's own internal source material for the same issuance — the internal risk register,
board materials, financial-forecast assumptions, material contracts and litigation records, and
prior-period disclosures — and returns, with evidence, what the draft appears not to reflect,
where two versions of a fact disagree, and which questions a reviewer should ask.

It is for the issuer's IR, finance and legal functions and the lead underwriter's review desk,
working on a draft before it is filed. Three modes cover the working cycle: `/ingest` accumulates
the internal material, `/invoke` reviews a draft against it, and `/feedback` records what the
reviewers actually decided so later reviews reflect it.

The value is deliberately located in material only the issuer holds. Public rules and filings are
not the corpus; the internal, unpublished record is, because a review that only checks a draft
against public sources tells the issuer nothing it could not already look up.

**It produces candidates and questions — never a disclosure verdict.** That is enforced in two
layers, not one, because a lexicon alone can be paraphrased around:

- **Structurally**: the output schema has no field in which a verdict could be recorded. Stage 3
  produces a *typed closed projection*, and an output carrying a prohibited verdict-shaped field
  is rejected outright.
- **Lexically**: a disclosure-assertion lexicon scans the generated text, and an output whose
  every text field reads as assertive is rejected rather than softened.

Both run again before anything is shown to a human reviewer, alongside a PII re-scan and a
provenance check that every cited `source_id` resolves to a record actually retrieved. A draft
that fails any of them never reaches the review dialogue.

Version-to-version contradiction flags are computed deterministically, not generated. The model's
job is to put findings into words; deciding that two figures disagree is arithmetic, and a
disclosure-control judgement is not delegated to generation.

The agent does not judge legal compliance, approve an issuance, price it, or prepare or submit a
filing. Those stay with the issuer, the underwriter and counsel.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime, with a checkpointer — HITL is enabled and `memory_enabled: true`, so
  suspend/resume needs one;
- **authentication middleware that stamps `company_id` and `issuance_id` as a verified scope** —
  this is not optional, see *Security and Limitations*;
- optionally a `BaseLLM` client and a knowledge-base service, injected through the graph
  configuration.

Both optional dependencies degrade explicitly rather than silently:

| Absent | Behaviour |
|---|---|
| LLM client | The draft stage returns a deterministic fallback and sets `degradation_code: "llm_not_configured"` with a matching `degradation_reason`, so a degraded run is visible in the response rather than looking like a clean one. |
| Knowledge-base service | Retrieval is skipped with `degradation_reason: "KB service not configured"`. |

Runtime parameters read from `config/config.yaml` (defaults in parentheses):

| Key | Meaning |
|---|---|
| `max_retry` (`3`) / `timeout_s` (`60`) | Backbone retry and timeout budget |
| `memory_enabled` (`true`) | Required by HITL — the checkpointer that makes suspend/resume possible |
| `hitl.enabled` (`true`) / `hitl.max_hitl` (`12`) | Human review, and a safety ceiling on refine rounds. The ceiling is a runaway guard, not a budget for the dialogue |
| `ingest.max_file_bytes` (`10 MiB`) | Per-file ceiling on ingested CSV / XLSX / DOCX |
| `ingest.max_batch_records` (`500`) | Per-batch record ceiling |
| `ingest.max_free_text_chars` (`20000`) | Per-field ceiling on free text — bounds both checkpoint growth and prompt-injection surface |
| `retrieval.top_k_internal_sources` (`8`) | Maximum evidence candidates returned within one scope |
| `retrieval.min_similarity` (`0.55`) | Similarity floor. Unrelated material is not handed to the model |
| `payload_store.ttl_seconds` (`300`) | TTL of the short-lived scoped store that holds raw text |

`config/taxonomy.yaml`, `config/disclosure_verdict_lexicon.yaml`, `config/ingest_mapping.yaml` and
`config/report_templates/` carry the domain vocabulary, the assertion lexicon, the ingest column
mapping and the report shape. They are meant to be adapted per deployment.

Accepted input formats are **CSV, XLSX and DOCX**. PDF and scanned paper are deliberately out of
scope: a format that is never accepted carries neither its attack surface nor its
misreading risk, and an OCR error in a financial figure lands directly on disclosure control.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent connects to the platform at start-up. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR. Without the `agenticstar-agentcore`
distribution installed, `src/api/server.py` cannot be imported at all — the graph, the state
schema and the security gates all come from it, and there is no vendored substitute in this
repository.

With the framework installed but no platform around it, the standalone server starts and
`/health` answers `{"status": "ok", "agent": "ProspectusDisclosureReviewGraph"}`. It will not
serve the full workflow, though. `/invoke` accepts an `INVOKE_AUTH_TOKEN` bearer token, but
`/ingest`, `/feedback` and `/resume` require a **verified middleware identity** and reject a
token-only caller. Without middleware stamping `company_id` and `issuance_id`, there is no
tenant scope to retrieve within. The test suite runs without a platform connection.

## Input

| Route | Purpose | Authentication |
|---|---|---|
| `POST /invoke` | Review a prospectus draft against the accumulated material | Middleware identity, **or** `INVOKE_AUTH_TOKEN` bearer |
| `POST /resume` | Continue the review dialogue after a suspension | Middleware identity **only** |
| `POST /ingest` | Load internal source material (CSV / XLSX / DOCX) | Middleware identity **only** |
| `POST /feedback` | Record a reviewer's decision on a finding | Middleware identity **only** |

`/invoke` and `/ingest` take `input` (text) and/or `file_bytes_b64` (a base64-encoded file), plus
`session_id` and `thread_id`. Malformed base64 is a **400**, never an unhandled server error.
`/feedback` takes `source_id`, `accepted`, and optionally `final_reason` and `learnable`.
`/resume` takes `thread_id` and the reviewer's response.

An unauthenticated caller gets **401**; a token-only caller on a write or resume route gets
**403**.

```bash
curl -X POST http://127.0.0.1:8000/invoke \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer ${INVOKE_AUTH_TOKEN}" \
  -d '{
    "input": "第一部 証券情報 …（目論見書ドラフト本文）",
    "session_id": "demo-001"
  }'
```

File type is decided from the **content** — magic bytes and MIME sniffing. A client-declared
`Content-Type` or filename extension is not trusted.

## Output

The response carries `formatted_output`, a dict whose shape is declared by mode rather than
inferred. For `/invoke` it holds `mode`, `gate_kind`, and the five review fields:

```json
{
  "mode": "invoke",
  "gate_kind": "draft",
  "facts": [],
  "evidence_sources": [],
  "unreflected_facts": [],
  "inconsistencies": [],
  "review_questions": []
}
```

`/ingest` returns `ingest_summary`, `/feedback` returns `feedback_summary`. An error returns
`gate_kind: "error"` with `status` and `error_log` — a mode that produced no result never gets a
fabricated one. A degraded run additionally carries `degradation_reason`.

A finding cites the records it came from, and every cited `source_id` is checked against the
records actually retrieved before the finding is released. Raw source text is not carried in the
response, and it is not persisted in state: it lives in a scoped, TTL-bounded payload store and
is referenced indirectly.

## Security and Limitations

**Tenant scope is mandatory and fail-closed.** Retrieval is confined to a single
`(company_id, issuance_id)` pair, and those keys must arrive as a *verified* scope from
authentication middleware. There is no default and no inference: a caller without a verified
scope cannot reach the accumulated material at all. This matters more here than in most
templates — the corpus is one issuer's unpublished internal record, and a cross-scope read would
be a material-information leak.

The graph requires verified-external trust. Write and resume routes go further and require a
middleware-verified identity, because a shared standalone token cannot distinguish one reviewer
from another and session ownership would be unenforceable.

Ingested files pass a format allowlist, content-based type detection, size and record ceilings, a
PII and sensitive-information scrub, and — for DOCX — an XXE-safe XML parse with macros never
executed and embedded objects never read.

Known limits, stated rather than implied:

- Detection is only as complete as the material ingested. A fact that exists nowhere in the
  accumulated record cannot be reported as unreflected.
- The PII scrub is a dictionary and pattern pass. Identifiers outside it can survive.
- Retrieval uses a similarity floor. A genuinely relevant record phrased very differently from
  the draft can fall below it and be missed; the floor exists because handing unrelated material
  to the model is the worse failure. Tuning it per deployment is expected.

Output is machine-generated and assistive. It is not a legal opinion, not a disclosure decision,
and not an issuance approval. Final disclosure content and the decision to issue rest with the
issuer, the lead underwriter and counsel.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2), outer backbone wrapping an inner workflow graph
- Generation mode: `llm`, with a declared deterministic fallback when no client is injected
- HITL: **enabled** (`hitl.enabled: true`, `max_hitl: 12`) — requires `memory_enabled: true`
- Persistence: checkpointer required; raw source text is held only in a TTL-bounded scoped store
- External API clients: deployment-injected; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, using fakes for the model and the stores. Running the
agent itself starts the standalone server:

```bash
uvicorn src.api.server:app --reload
```

Set `INVOKE_AUTH_TOKEN` before calling `/invoke`. `/ingest`, `/feedback` and `/resume` need real
authentication middleware stamping `company_id` and `issuance_id` — the token alone will not
reach them.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the input-channel sanitisation
design, the HITL design and the two-layer disclosure-verdict prevention. `docs/03_test_spec.md`
records the test specification and the verification results, including the retrieval-recall
limitation currently under review.

## Project Structure

```
src/          agent implementation (nodes, services, schemas)
tests/        unit, integration and boundary tests
config/       agent configuration
docs/         design and test specifications
```

See `docs/02_design.md` for the design and `docs/03_test_spec.md` for the test specification.

## Customising

1. Adjust `config/` for your own environment and policies.
2. Replace the knowledge sources and sample data with your own.
3. Review the node implementations under `src/nodes/` for domain-specific logic.
4. Re-run the test suite.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.

---
