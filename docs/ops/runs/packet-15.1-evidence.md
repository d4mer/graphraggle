# Packet 15.1 Evidence

## Scope

Repair the evaluation gate so GraphRAG promotion decisions are based on domain-matched queries and full retrieval evidence.

## Changed Files

1. `eval/datasets/curated/queries.json`
2. `scripts/eval_run.sh`
3. `scripts/eval_score.py`
4. `scripts/eval_gate.py`
5. `tests/test_eval_gate.py`
6. `tests/test_eval_score.py`
7. `CHANGELOG.md`

## Commands Run

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile app/api.py app/graphrag.py scripts/eval_score.py scripts/eval_gate.py
```

## Observed Outputs

1. Full test suite passes.
2. Eval runner now stores citations and `query_scope` for both baseline and candidate arms.
3. Gate output now includes minimum sample-size and graph activation diagnostics.

## Risks Remaining

1. Judge-model scoring quality still depends on the selected model and prompt calibration.
2. Full 12-query production-grade evals may remain expensive and slow; use staged subsets for iteration.
3. Graph activation metrics are diagnostic only in this packet and do not yet enforce quality policy.

## Rollback

1. Restore prior curated query set if needed.
2. Revert `scripts/eval_run.sh`, `scripts/eval_score.py`, and `scripts/eval_gate.py`.
3. Rerun tests to verify fallback behavior.
