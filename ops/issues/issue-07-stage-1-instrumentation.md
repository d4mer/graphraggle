# Issue 07 — Stage 1: Instrumentation

**Branch off:** `packet-18-stage-0-restore-fidelity`
**Work on:** `packet-19-stage-1-instrumentation`
**Tracked in Plane:** GRAG-14, GRAG-15, GRAG-16
**Source PRD:** `ops/prds/prd-precision-first-retrieval.md`

---

## Why this exists

The system answers questions over SAP/OMP implementation documents. LightRAG
v1.4.15 holds the index and graph; a FastAPI gateway (`app/`) fronts it;
OpenWebUI talks to the gateway through an Ollama-compatible bridge at
`/api/chat`.

Two failure modes were diagnosed: answers are vague and generic, and facts
that exist in the corpus never surface. Stage 0 (already merged into the
branch you are starting from) removed the causes that were destroying answers
at query time.

**Stage 1 does not change retrieval behaviour at all.** It builds the ability
to *measure* it. Everything after this stage — replacing the entity schema,
re-chunking, entity-anchored retrieval — requires proving a change on a subset
before committing to a full re-index. None of that is possible today because:

- `query_scope` is assembled richly on every query and then thrown away
- there is no ground truth to measure recall against
- the existing eval harness (`scripts/eval_*.py`) compares gateway-candidate
  against gateway-baseline only, never touches `/api/chat`, and gates on a
  3-field LLM rubric whose run-to-run variance exceeds its 8% threshold

This stage fixes all three. It is a blocker: do not start Stage 2.

---

## Deliverable 1 — Structured per-query logging (GRAG-14)

**New file:** `app/observability.py`
**Touches:** `app/api.py`

Emit exactly one structured JSON line per query, covering both `/query` and
the `/api/chat` bridge path.

Required fields:

| Group | Fields |
|---|---|
| Identity | `request_id`, `timestamp`, `endpoint` (`query` \| `bridge`) |
| Request | `query`, `company`, `mode_requested`, `mode_used`, `top_k`, `chunk_top_k` |
| Retrieval | `citations_raw`, `citations_merged`, `citations_scoped`, `citations_final` |
| Fallback | `fallback_used`, `fallback_reason`, `fallback_answer_used` |
| Multi-query | `mq_triggered`, `mq_trigger_reason`, `mq_rewrite_count`, `mq_error` |
| Graph | `graph_native_route`, `graph_native_applied`, `graph_native_error`, `graph_seed_labels` |
| Synthesis | `graph_synthesis_applied`, `graph_synthesis_error` |
| Rerank | `rerank_applied`, `rerank_error`, `rerank_input_count` |
| Outcome | `answer_chars`, `latency_ms_total`, `latency_ms_by_stage` |

Requirements:

1. `request_id` is generated at request entry (uuid4 hex, 12 chars is enough)
   and **returned in the response envelope `meta`**, so a user reporting a bad
   answer gives you a handle that finds the log line.
2. Most fields already exist in the `query_scope` dict built in `app/api.py`.
   Reuse it — do not recompute.
3. `latency_ms_by_stage` is a flat dict keyed by stage name
   (`first_pass`, `multi_query`, `graph_native`, `rerank`, `synthesis`).
4. Written to stdout as one line of JSON so it lands in container logs.
   Also append to `logs/queries.jsonl` when `QUERY_LOG_PATH` is set.
5. **Logging must never break a query.** Wrap the whole emit in try/except and
   swallow. A failure to log is not a failure to answer.
6. Do not log secrets. The query text itself is fine — it is the operator's
   own corpus.

**Acceptance**

- One JSON line per query, both endpoints, parseable with `json.loads`
- `request_id` in the line matches the one in the response `meta`
- Deliberately breaking the logger (raise inside it) still returns a normal answer
- A query that fails upstream still logs a line, with the error recorded

---

## Deliverable 2 — Probe set (GRAG-15)

**New files:** `eval/datasets/probe/queries.json`, `eval/datasets/probe/README.md`,
`scripts/probe_validate.py`

This is ground truth for recall. Not an LLM rubric — a record of where each
answer actually lives.

### CRITICAL SCOPE BOUNDARY

**You are building the container and the validator. You are NOT writing the
probe questions.** The questions must come from real usage and from someone
who knows which document holds each answer. An agent inventing 40 plausible
questions produces a set that measures nothing.

Commit `queries.json` containing **three worked examples only**, clearly
marked as templates, drawn from the existing graph-shaped queries in
`eval/datasets/graph/queries.json` (which already carry `expected_topics`).
The operator fills in the rest.

### Entry schema

```json
{
  "id": "p001",
  "question": "What relationship is described between Code Orange feedback and planned goods receipt dates?",
  "source_document": "acme/workshop-2026-03-11-transcript.md",
  "source_passage": "verbatim snippet from the document that contains the answer",
  "expected_specifics": ["Code Orange", "planned goods receipt date"],
  "company": "acme",
  "document_class": "transcript",
  "category": "graph-feedback",
  "notes": "optional"
}
```

- `source_document` must match the path as it appears in gateway citations,
  not an absolute filesystem path.
- `source_passage` is used for substring matching against returned chunk
  content, so it must be verbatim.
- `expected_specifics` are the entities, field names or values a correct
  answer has to name. This is the specificity metric.

### `scripts/probe_validate.py`

Checks the file before anyone wastes a run on it:

- schema conformance, unique ids
- `source_document` resolves against the state store (`app/state_store.py`)
  or `SOURCE_DOCS_DIR`
- `source_passage` is non-empty and actually occurs in that document
- `expected_specifics` is non-empty

Exit non-zero with a per-entry report on failure.

**Acceptance**

- `python3 scripts/probe_validate.py eval/datasets/probe/queries.json` passes
  on the three committed examples
- It fails loudly on a bad path, a passage that is not in its document, and a
  duplicate id
- README documents the schema and states plainly that questions are authored
  by the operator

---

## Deliverable 3 — `scripts/probe_compare.py` (GRAG-16)

**New files:** `scripts/probe_compare.py`, `eval/targets.example.json`

Runs the probe set against two or more targets and reports the difference.
**Fully deterministic — no judge model.** Same inputs, same report.

### Targets

```json
{
  "gateway":         { "kind": "gateway",       "url": "http://localhost:8000", "api_key_env": "RAG_API_KEY" },
  "bridge":          { "kind": "ollama_bridge", "url": "http://localhost:8000", "model": "lightrag:latest" },
  "lightrag_direct": { "kind": "lightrag",      "url": "http://localhost:9622", "api_key_env": "LIGHTRAG_API_KEY",
                       "mode": "mix", "top_k": 40 }
}
```

Three kinds are required because the PRD needs three arms:

- `gateway` → `POST /query`, the main path
- `ollama_bridge` → `POST /api/chat`, the path in daily use, currently untested
- `lightrag` → LightRAG's own `/query`, **the ceiling**. The gateway should
  never score below it; if it does, the gateway is subtracting value.

A target may also carry `"workspace": "<name>"` so a candidate index built in
a separate LightRAG workspace can be compared against the live one. That is
the mechanism Stage 2 depends on.

### Metrics, per probe × target

| Metric | Definition |
|---|---|
| `retrieved` | `source_document` appears among returned citations |
| `rank` | 1-based position of the first matching citation, else `null` |
| `passage_hit` | `source_passage` appears in any returned chunk content |
| `specificity` | fraction of `expected_specifics` present in the answer, case-insensitive |
| `answer_chars` | length of the answer |
| `latency_ms` | wall-clock for that call |

Aggregates: recall@k for k in {1,3,5,10}, mean reciprocal rank, mean
specificity, p50/p95 latency. Report per-category as well as overall.

### Output

- JSON report to `docs/ops/runs/probe-<UTC-date>-<label>.json` (already gitignored)
- Markdown side-by-side to a sibling `.md`, sorted by probe id, with a
  per-metric delta column and regressions marked
- Exit code 0 always — this is a measurement tool, not a gate. Gating comes later.

### CLI

```bash
python3 scripts/probe_compare.py \
  --probe eval/datasets/probe/queries.json \
  --targets eval/targets.json \
  --compare gateway lightrag_direct \
  --label stage0-verify
```

**Acceptance**

- Runs all three target kinds against the same probe set
- Two runs over an unchanged system produce identical reports apart from latency
- A probe whose document is not retrieved reports `retrieved: false, rank: null`
  rather than crashing
- Handles an unreachable target by recording the failure per-probe and continuing
- Markdown output is readable without opening the JSON

---

## Constraints

1. **Do not change retrieval behaviour.** This stage observes. If you find a
   bug in the query path, write it up — do not fix it here.
2. **Do not re-enable anything Stage 0 disabled.** In particular
   `GRAPH_SYNTHESIS_REPLACE_ANSWER` stays false and `RERANK_BY_DEFAULT` stays
   as it is; the double-rerank item (GRAG-8) is under review and its current
   wording is probably wrong.
3. **Do not touch the index, ingestion, or `app/worker.py`.**
4. **Do not invent probe questions.**
5. New runtime dependencies must be flagged, not silently added. Prefer the
   stdlib plus `httpx`, which is already a dependency.
6. Follow the existing module style: `from __future__ import annotations`,
   type hints, pure functions kept importable without the FastAPI chain (see
   `app/rerank.py` for the pattern).
7. Every new module gets tests under `tests/`, matching existing naming.

---

## Verification

```bash
# tests need the settings env vars, since app.config instantiates at import
export RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test \
       LIGHTRAG_BASE_URL=http://localhost:9621 \
       SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db

python3 -m py_compile app/*.py scripts/*.py
python3 -m pytest tests/ -q          # 290 tests pass before your changes; keep them passing
python3 scripts/probe_validate.py eval/datasets/probe/queries.json
```

Then, against a running stack, one real end-to-end run:

```bash
python3 scripts/probe_compare.py --probe eval/datasets/probe/queries.json \
  --targets eval/targets.json --compare gateway lightrag_direct --label smoke
```

---

## Definition of done

- [ ] One parseable JSON log line per query on both endpoints, with `request_id` echoed in response `meta`
- [ ] Breaking the logger does not break a query
- [ ] Probe schema, README, three template entries, and a validator that fails loudly on bad input
- [ ] `probe_compare.py` runs all three target kinds and is byte-identical across repeated runs, latency aside
- [ ] Markdown report readable on its own
- [ ] `pytest` green, new modules covered
- [ ] No behavioural change to retrieval — diff of `app/api.py` shows logging hooks and `request_id` only
