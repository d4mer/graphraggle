# Changelog

## 2026-05-06

### Packet 16: GraphRAG Adaptive Expansion

1. Added config settings `GRAPH_EXPANSION_ENABLED` (bool, default false), `GRAPH_EXPANSION_HOPS` (int, default 1), `GRAPH_EXPANSION_MAX_NEIGHBORS` (int, default 10), and `GRAPH_SEED_CITATION_COUNT` (int, default 3) to `app/config.py`.
2. Created `app/graphrag.py` as a deep module with pure graph helper functions: `citation_to_text_for_entity_extraction`, `extract_entities_via_llm`, `expand_graph_neighbors`, `merge_graph_expansion`, `compute_adaptive_hops`, and `build_graph_metadata`.
3. Wired graph expansion into the gateway `/query` endpoint: after merged_citations computed (single or multi-query), extract entities from top `GRAPH_SEED_CITATION_COUNT` citations via LLM bypass, run 1-hop expansion, evaluate adaptive triggers for 2-hop escalation (neighbors < 4 OR scoped citations < 3 OR complex query + low rerank score < 0.15), merge graph pseudo-citations into merged_citations, then existing scope -> rerank -> top-5 path. Fail-open on any graph extraction/expansion failure.
4. Added six `query_scope` metadata fields: `graph_expansion_enabled`, `graph_expansion_applied`, `graph_expansion_error`, `graph_seed_count`, `graph_neighbor_count`, `graph_hops_used`.
5. Preserved existing multi-query/rerank/fallback behavior unchanged.
6. Added 47 unittest-style tests under `tests/test_graphrag.py` covering: entity extraction parse success/malformed fail-open, adaptive hop escalation rules, neighbor cap and remaining-budget for hop2, merge/dedupe stability, metadata fields, config defaults, and integration flow placement.

## 2026-05-06

### Packet 15: Eval Harness and Auto Gates

1. Created `docs/packets/15-eval-harness-and-auto-gates.md` with locked decisions: hybrid eval dataset (curated + anonymized production samples), automated scoring with human spot-review for borderline runs, multi-query gate thresholds (quality lift >= 8%, p95 latency increase <= 20%), baselines versioned in repo JSON, CI + operator-triggered model, borderline criteria (quality delta ±2% of threshold, p95 latency ±3% of threshold, >10% parse/rubric failures), rubric fields per query (grounded, fact_consistent, complete 0/1), deterministic local anonymization, A/B in one invocation, on-fail explicit criteria with rollback recommendation.
2. Created `eval/` directory tree: `eval/datasets/curated/queries.json` (8 curated queries), `eval/datasets/production-samples/README.md` (anonymization workflow doc), `eval/baselines/packet-13.json` and `eval/baselines/packet-14.json` (versioned baseline JSON with config snapshot and metrics schema), `eval/baselines/SCHEMA.md` (baseline file schema documentation), `eval/README.md` (A/B run and promotion workflow documentation).
3. Created `scripts/eval_run.sh` (executable A/B runner entrypoint): reads query set, calls gateway `/query` for each query under baseline and candidate configs in one invocation, captures per-query latency, writes JSON report to `docs/ops/runs/packet-15-eval-report.json` and markdown summary to `docs/ops/runs/packet-15-eval-summary.md`.
4. Created `scripts/eval_gate.py` (gate evaluation): computes quality lift and p95 latency increase, evaluates thresholds (>=8% quality lift, <=20% latency increase), detects borderline conditions, outputs pass/fail verdict with rollback hints.
5. Created `scripts/eval_anonymize_samples.py` (deterministic anonymization): replaces emails, phones, SSNs, IPs, and multi-word names with fixed-salt SHA-256 hashed placeholders; same input always produces same output; no network calls.
6. Created `scripts/eval_score.py` (rubric scoring): parses model-judge structured JSON output, applies heuristic fallback for CI without judge access, aggregates per-configuration mean rubric score and per-field pass rates.
7. Added 35 unittest-based tests in `tests/test_eval_gate.py` covering threshold logic, borderline detection, fail handling, rollback recommendations, and edge cases.
8. Added 17 unittest-based tests in `tests/test_eval_anonymize.py` covering deterministic obfuscation, PII pattern replacement, determinism verification, and file I/O.
9. Added 24 unittest-based tests in `tests/test_eval_score.py` covering judge output parsing, heuristic scoring, rubric aggregation, parse failure handling, and report scoring.
10. Documented CI integration pattern in `eval/README.md` for automated and manual gate evaluation runs.

## 2026-05-06

### Packet 14: Multi-Query Expansion

1. Added config settings `MULTI_QUERY_ENABLED` (bool, default false), `MULTI_QUERY_LONG_QUERY_WORDS` (int, default 20), `MULTI_QUERY_REWRITE_COUNT` (int, default 2), and `MULTI_QUERY_TRANSCRIPT_KEYWORDS` (csv string, default matching existing transcript keywords) to `app/config.py`.
2. Created `app/multi_query.py` as a deep module with pure multi-query helper functions: `should_trigger_multi_query`, `parse_keywords_csv`, `has_weak_answer_signal`, `generate_rewrites_via_bypass`, `merge_and_dedupe_citations`, `build_multi_query_metadata`, and `_normalize_citation_path`.
3. Wired multi-query expansion into the gateway `/query` endpoint: first-pass retrieval with original query, deterministic trigger evaluation (long query word count, transcript keyword hit, weak first-pass signal), LLM-driven rewrite generation via LightRAG `/query` in bypass mode, per-rewrite retrieval, stable merge (original -> rewrite1 -> rewrite2), path-first then content-fallback deduplication, then company scope filter -> rerank -> top-5.
4. Fail-open: rewrite generation or retrieval failure falls back to single-query original path with `multi_query_error` set in `query_scope` metadata.
5. Added seven `query_scope` metadata fields: `multi_query_enabled`, `multi_query_triggered`, `multi_query_trigger_reason`, `multi_query_rewrite_count`, `multi_query_error`, `multi_query_candidate_count_before_dedupe`, `multi_query_candidate_count_after_dedupe`.
6. Preserved existing transcript-like `top_k` expansion and fallback behavior unchanged.
7. Added 42 unittest-style tests under `tests/test_multi_query.py` covering: trigger decisions, rewrite parsing robustness, stable merge + dedupe rules, fail-open metadata, config defaults, and integration-level metadata field presence/counts.

## 2026-05-06

### Packet 14.1: Production Activation Hotfix (Multi-Query + Rerank)

1. Fixed multi-query rewrite generation by changing bypass rewrite requests from `top_k=0` to `top_k=1` to avoid upstream 500 responses.
2. Added rerank endpoint auth/model support in gateway config and rerank requests: `RERANK_BINDING_API_KEY`, `RERANK_MODEL`.
3. Improved rerank citation text extraction to handle list-based citation content and cap payload text length for stability on large transcript citations.
4. Updated tests for rewrite request behavior and added rerank list-content coverage.
5. Verified live production behavior: `multi_query_triggered=true`, `multi_query_rewrite_count=2`, `rerank_applied=true`, `rerank_error=null`.

### Packet 13: Reranking Post-Scoping

1. Added config settings `RERANK_ENABLED` (bool, default false) and `RERANK_BINDING_HOST` (optional URL) to `app/config.py`.
2. Created `app/rerank.py` as a deep module with pure rerank helper functions: `citation_to_text`, `call_rerank_endpoint`, `rerank_citations`, and `truncate_citations`.
3. Wired reranking into the gateway `/query` endpoint: applied only to in-scope citations (after company scope filtering), with fail-open behavior (preserves original order on error).
4. Enforced top-5 citation truncation after reranking (or after original order when disabled/failed).
5. Added five `query_scope` metadata fields: `rerank_enabled`, `rerank_applied`, `rerank_error`, `rerank_input_count`, `rerank_output_count`.
6. Preserved existing transcript-like `top_k` expansion and fallback behavior unchanged.
7. Added 25 unittest-style tests under `tests/test_reranking.py` covering: rerank applied and reorders, feature flag off preserves order, rerank failure fail-open, top-5 truncation, company filter before rerank, and config defaults.

## 2026-05-06

### Packet 12: Retrieval Usability Hotfix

1. Updated gateway query guidance to reflect default `hybrid` mode when omitted and one-time fallback to `naive` on weak retrieval.
2. Added transcript-query operator guidance recommending speaker/date anchors plus short quote fragments.
3. Added worker-side minimal transcript normalization before `/documents/text` submission (newline collapse, safe timestamp-noise cleanup, whitespace trimming).
4. Added executable helper script `scripts/reindex_sct_docs.sh` for dry-run/apply reindex of matching SCT/workshop GSK docs via `/ingest/status` plus `/ingest/reindex`.
5. Updated API/admin/operator/OpenWebUI/quick-reference docs and script docs for Packet 12 operations.

## 2026-04-24

### Added

1. Initial GraphRAG implementation repository structure
2. Full operator documentation set under `docs/`
3. Installer script under `scripts/install_rag_stack.sh`
4. Top-level repository README
5. Top-level TODO list for follow-up hardening work

### Validated

1. Stack startup with Podman on Mac mini M4
2. LightRAG image tag `ghcr.io/hkuds/lightrag:v1.4.15`
3. oMLX LLM endpoint at `192.168.1.190:1234/v1`
4. oMLX embedding endpoint at `192.168.1.180:1234/v1`
5. oMLX rerank endpoint at `192.168.1.180:1234/v1/rerank`
6. Full text retrieval from `rag-smoke.txt`

### Fixed

1. Incorrect LightRAG image tag without `v` prefix
2. Incorrect embedding model name `mxbai-embed-large`
3. LightRAG container command duplication issue
4. Worker ingestion path for text files by switching to `POST /documents/text`
5. Worker duplication of internal `source_docs/__enqueued__` files
6. Worker text decoding by adding fallbacks for non-UTF-8 text files like Windows-1252 transcripts
7. Unsupported file upload returning `pending` instead of `rejected` — added upload-time extension validation so unsupported files are immediately rejected with `status=rejected`, `validation_state=reject`, `error_stage=validation`, `error_code=unsupported_extension`
8. Pre-ingest validation gate (Packet 02) — added `app/validation.py` with deterministic file classification, verdict assignment (`accept`, `warn`, `reject`, `auto_split`), duplicate detection, size thresholds, and encoding fallback detection; integrated into upload endpoint and worker pre-submission path

### Packet 03: Schema And Status API Expansion (2026-05-04)

1. Added 9 new nullable columns to `documents` table: `content_hash`, `detected_mime`, `detected_encoding`, `byte_size`, `page_count`, `source_uri`, `supersedes_document_id`, `ingested_at`, `warnings_json`
2. Added 9 corresponding migration entries in `MIGRATIONS` dict (idempotent, skip-if-exists)
3. Enhanced `upsert_document` and `update_document_state` to handle new columns
4. Expanded `/ingest/status` summary with `by_error_stage` and `by_company` slices
5. Enhanced upload endpoint response to include all 26 target fields
6. Worker now sets `ingested_at` when document reaches `ingested` status
7. Worker now passes `byte_size` in upsert calls for filesystem-scanned files
8. Created ADR 0007 documenting schema expansion decisions

### Packet 06: Query Readiness And Reindex Controls (2026-05-05)

1. Added canonical derived `query_ready` and `readiness_reason` logic to document/status API output
2. Expanded `/ingest/status` summary with `by_readiness_reason`
3. Added `app/models.py` to check in the API request and response contract, including scoped `ReindexRequest` selectors
4. Replaced `/ingest/reindex` stub with worker-routed requeue semantics using explicit operator scope filters and `force` gating
5. Updated worker scan logic to honor operator reindex requests without bypassing validation or submit/poll behavior
6. Updated operator docs and Packet 06 evidence with readiness and reindex QA commands

### Packet 07: Company Attribution And Retrieval Scoping (2026-05-05)

1. Added persistent `company_source` and `company_source_detail` provenance fields to document state plus `by_company_source` summary output
2. Standardized company attribution precedence as explicit request value, then filesystem path inference, then unscoped null
3. Implemented filesystem inference from the first directory under `source_docs/` and backfilled that attribution during worker scans
4. Expanded upload and query API payloads to expose company provenance directly to operators
5. Added explicit `/query` scoping metadata and exact-match company citation filtering for company-scoped requests without claiming hard LightRAG multi-tenancy
6. Updated Packet 07 ops docs and API reference with provenance and scoping policy, verification commands, and QA guidance

### Packet 08: LightRAG Web UI Exposure (2026-05-05)

1. Added dedicated port mapping `9622:9621` to `compose.yml` for intentional LightRAG Web UI exposure on trusted LAN
2. Updated ADR 0005 with exposure method decision (dedicated port) and auth decision (deferred to first pass)
3. Added LightRAG Web UI section to admin operations doc with access URL, intended use, and security note
4. Added LightRAG Web UI URL to known-good config
5. Added LightRAG Web UI entry to operator cheat sheet with purpose distinction
6. Added step 6b (LightRAG Web UI admin/debug) to daily workflow
7. Added three-surface distinction table to Open WebUI guide clarifying when to use each surface
8. Updated Packet 08 packet file with accepted decisions and implementation result
