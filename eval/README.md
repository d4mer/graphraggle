# Evaluation Harness

## Overview

This directory contains assets for the A/B evaluation harness (Packet 15):

- `datasets/` — Query sets for evaluation runs
- `baselines/` — Versioned baseline configuration snapshots
- `eval/` — (parent directory)

## A/B Run Workflow

### 1. Prepare Query Set

The eval uses a hybrid dataset:

- **Curated queries**: Hand-authored queries in `datasets/curated/queries.json`.
- **Production samples**: Anonymized queries in `datasets/production-samples/`.

To add production samples:

```bash
# Collect raw queries locally (never commit raw files)
python3 scripts/eval_anonymize_samples.py raw_queries.json anonymized_queries.json
```

### 2. Run A/B Comparison

```bash
# Run with baseline and candidate env configs
# The script calls /query for each query under both configs in one invocation
BASELINE_ENV=baseline CANDIDATE_ENV=candidate ./scripts/eval_run.sh
```

The runner:

1. Loads all queries from the dataset files.
2. For each query, calls the gateway `/query` endpoint with `BASELINE_ENV` settings.
3. Calls the gateway `/query` endpoint with `CANDIDATE_ENV` settings.
4. Captures per-query: answer, citations, latency (ms), metadata.
5. Writes machine-readable report to `docs/ops/runs/packet-15-eval-report.json`.
6. Writes human-readable summary to `docs/ops/runs/packet-15-eval-summary.md`.

### 3. Score Results

```bash
python3 scripts/eval_score.py docs/ops/runs/packet-15-eval-report.json
```

The scorer:

1. Reads the report JSON.
2. For each query, parses the `judge_output` field (structured JSON).
3. Aggregates rubric scores: `grounded`, `fact_consistent`, `complete` (0/1).
4. Computes per-configuration mean rubric score and per-field pass rates.
5. Writes scored report alongside the raw report.

### 4. Evaluate Gates

```bash
python3 scripts/eval_gate.py docs/ops/runs/packet-15-eval-report.json
```

The gate evaluator:

1. Reads the scored report JSON.
2. Computes quality lift and p95 latency increase vs baseline.
3. Evaluates thresholds:
   - Quality lift >= 8% → PASS
   - p95 latency increase <= 20% → PASS
4. Detects borderline conditions:
   - Quality delta within ±2% of 8% threshold (6%–10%)
   - p95 latency within ±3% of 20% threshold (14%–26%)
   - >10% parse/rubric failures
5. Outputs verdict JSON with pass/fail per criteria and rollback hints.

### 5. Baseline Promotion (Operator Only)

**Only the operator** may promote a candidate baseline:

1. Review the gate verdict and human-readable summary.
2. If gates pass (no borderline): create a new baseline file in `eval/baselines/`.
3. If borderline: perform human spot-review of the flagged queries.
4. If gates fail: do NOT update baseline; follow rollback recommendation in verdict.

## Gate Thresholds

| Metric | Threshold | Borderline Range |
|--------|-----------|-----------------|
| Quality lift | >= 8% | 6% – 10% |
| p95 latency increase | <= 20% | 14% – 26% |
| Parse/rubric failures | <= 10% | > 10% |

## Rubric

Each query is scored on three binary fields (0 or 1):

| Field | Description |
|-------|-------------|
| `grounded` | Answer is supported by the retrieved citations (no hallucination) |
| `fact_consistent` | Answer does not contradict facts in the citations |
| `complete` | Answer addresses all aspects of the query |

Mean rubric score = mean of `(grounded + fact_consistent + complete) / 3` across all queries.

## Run Artifacts

All run artifacts are stored in `docs/ops/runs/`:

- `packet-15-eval-report.json` — Machine-readable per-query results
- `packet-15-eval-summary.md` — Human-readable summary
- `packet-15-gate-verdict.json` — Gate evaluation verdict
- `packet-15-evidence.md` — Implementation evidence

These files are **not committed to git** (listed in `.gitignore`).

## CI Integration

To run in CI:

```bash
# Prerequisites: gateway API must be running, .env files configured
# 1. Run A/B comparison
BASELINE_ENV=baseline CANDIDATE_ENV=candidate ./scripts/eval_run.sh

# 2. Score results
python3 scripts/eval_score.py docs/ops/runs/packet-15-eval-report.json

# 3. Evaluate gates
python3 scripts/eval_gate.py docs/ops/runs/packet-15-eval-report.json
EXIT_CODE=$?

# 4. Gate failure blocks promotion
if [ $EXIT_CODE -ne 0 ]; then
  echo "Gate evaluation failed. Promotion blocked."
  exit 1
fi
```

Manual operator run (same commands, no CI gate enforcement).
