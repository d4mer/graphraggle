# Packet 15: Eval Harness and Auto Gates

## Metadata

- **Status**: accepted
- **Author**: (implementer)
- **Date**: 2026-05-06
- **Dependencies**: Packet 13 (Reranking Post-Scoping), Packet 14 (Multi-Query Expansion)

## Locked Decisions

1. **Hybrid eval dataset**: curated + anonymized production samples.
2. **Automated scoring with human spot-review only for borderline runs.**
3. **Multi-query gate thresholds**: quality lift >= 8%, p95 latency increase <= 20% (vs baseline).
4. **Baselines versioned in repo JSON**; run artifacts in `docs/ops/runs/`.
5. **CI + operator-triggered model**; only operator promotes baseline.
6. **Borderline criteria**:
   - quality delta within ±2% of threshold, OR
   - p95 latency within ±3% of threshold, OR
   - >10% parse/rubric failures.
7. **Rubric fields per query**: `grounded`, `fact_consistent`, `complete` (0/1).
8. **Deterministic local anonymization script**; no raw logs in git.
9. **A/B in one invocation.**
10. **On fail**: explicit failed criteria, no baseline update, rollback recommendation.

## Problem Statement

The system has two major retrieval enhancements (reranking and multi-query expansion) deployed sequentially. There is no systematic, repeatable way to measure whether a candidate configuration (e.g., enabling both features) improves query quality and latency compared to the current baseline. Without an eval harness, promotions are manual and opinionated, risking quality regressions.

## Objectives

1. Provide a **repeatable A/B evaluation harness** that runs candidate and baseline configurations against the same query set in a single invocation.
2. Compute **machine-readable quality and latency metrics** with a scoring rubric (grounded, fact_consistent, complete).
3. Enforce **automated gate thresholds** (quality lift >= 8%, p95 latency increase <= 20%) with explicit borderline detection.
4. Produce **human-readable summaries** for operator review.
5. Support **baseline promotion** only when gates pass; recommend rollback on fail.

## Acceptance Criteria

1. `scripts/eval_run.sh` invokes A/B comparison against same query set, captures per-query latencies.
2. `scripts/eval_gate.py` computes metrics, evaluates thresholds, detects borderline, outputs pass/fail with rollback hints.
3. `scripts/eval_anonymize_samples.py` deterministically anonymizes production query samples (no raw PII in git).
4. `scripts/eval_score.py` parses model-judge structured JSON output and aggregates rubric scores.
5. Eval dataset includes both curated queries and anonymized production samples.
6. Baseline configs stored as versioned JSON in `eval/baselines/`.
7. Run artifacts stored in `docs/ops/runs/` as both JSON and markdown.
8. Tests: `tests/test_eval_gate.py`, `tests/test_eval_anonymize.py`, `tests/test_eval_score.py` all pass.
9. CI hook documented: script invokable in CI and manually by operator.

## Design

### Query Set (Hybrid Dataset)

- `eval/datasets/curated/queries.json`: Hand-authored queries covering known use cases.
- `eval/datasets/production-samples/`: README-only directory; actual samples anonymized locally before eval run.

### A/B Runner (`scripts/eval_run.sh`)

- Reads query set from `eval/datasets/curated/queries.json` (and optionally anonymized production samples).
- Accepts two env config identifiers: `BASELINE_ENV` and `CANDIDATE_ENV` (e.g., different `.env` files or env var prefixes).
- For each query, calls the gateway `/query` endpoint twice (baseline then candidate) with the same query text.
- Captures per-query: answer text, citations count, latency (ms), and response metadata.
- Outputs machine-readable report to `docs/ops/runs/packet-15-eval-report.json`.
- Outputs human-readable markdown summary to `docs/ops/runs/packet-15-eval-summary.md`.

### Scoring (`scripts/eval_score.py`)

- Takes the JSON report from the runner.
- For each query, expects a `judge_output` field (structured JSON from a model-judge or heuristic scorer).
- Aggregates rubric fields: `grounded`, `fact_consistent`, `complete` (0/1 per query).
- Computes per-configuration scores: mean rubric score, pass rate per field.
- Outputs scored report alongside raw metrics.

### Gate Evaluation (`scripts/eval_gate.py`)

- Reads scored report (JSON).
- Computes:
  - **Quality lift**: `(candidate_mean_rubric - baseline_mean_rubric) / baseline_mean_rubric * 100`
  - **p95 latency**: `sorted(latencies)[int(0.95 * len(latencies))]` for each config.
  - **p95 latency increase**: `(candidate_p95 - baseline_p95) / baseline_p95 * 100`
- Evaluates thresholds:
  - Quality lift >= 8% → PASS
  - p95 latency increase <= 20% → PASS
- Borderline detection:
  - Quality delta within ±2% of 8% threshold (i.e., 6% to 10%)
  - p95 latency within ±3% of 20% threshold (i.e., 14% to 26%)
  - >10% parse/rubric failures (queries without valid rubric scores)
- Output: JSON verdict with pass/fail per criteria, borderline flags, rollback recommendations.

### Baseline Management

- Baselines stored as `eval/baselines/packet-NN.json` files in repo.
- Each baseline JSON contains: config snapshot, metric snapshots, timestamp, author.
- Only the operator can promote a candidate baseline to a new versioned baseline file.
- Run artifacts (reports, summaries) go to `docs/ops/runs/` — not committed to git.

### CI Integration

- CI can run `scripts/eval_run.sh` + `scripts/eval_gate.py` against a known baseline.
- Gate failure blocks promotion; operator must manually approve borderline cases.
- No external CI provider assumptions — documented as a shell invocation pattern.

## Implementation Plan

1. Create packet doc (`docs/packets/15-eval-harness-and-auto-gates.md`) — **this file**.
2. Create eval directory assets (datasets, baselines schema, README).
3. Create `scripts/eval_run.sh` — A/B runner entrypoint.
4. Create `scripts/eval_gate.py` — Gate evaluation logic.
5. Create `scripts/eval_anonymize_samples.py` — Deterministic anonymization.
6. Create `scripts/eval_score.py` — Rubric scoring.
7. Create tests: `tests/test_eval_gate.py`, `tests/test_eval_anonymize.py`, `tests/test_eval_score.py`.
8. Update `CHANGELOG.md` and evidence file.
9. Run full test suite, capture output.
10. Set packet status to `accepted`.

## Risks and Open Items

1. **Model-judge dependency**: Rubric scoring currently assumes a structured JSON judge output. If no judge model is available, heuristic scoring (keyword matching) will be used as fallback.
2. **Production data anonymization**: The anonymization script must be deterministic and tested. No raw PII should enter the git repo.
3. **Gateway availability**: The runner requires the gateway API to be running. CI must ensure the stack is up before running eval.
4. **Latency variance**: Network latency to the gateway could introduce noise. Consider multiple runs per query for statistical significance (deferred to future packet).

## Rollback

To rollback Packet 15:
1. Remove `eval/` directory tree.
2. Remove `scripts/eval_run.sh`, `scripts/eval_gate.py`, `scripts/eval_anonymize_samples.py`, `scripts/eval_score.py`.
3. Remove `tests/test_eval_gate.py`, `tests/test_eval_anonymize.py`, `tests/test_eval_score.py`.
4. Remove `docs/packets/15-eval-harness-and-auto-gates.md`.
5. Revert `CHANGELOG.md` Packet 15 section.
6. No app code changes; no API changes.
