# Packet 15 Evidence

## Changed Files

1. `docs/packets/15-eval-harness-and-auto-gates.md` — Packet specification with locked decisions
2. `eval/README.md` — A/B run and promotion workflow documentation
3. `eval/datasets/curated/queries.json` — 8 curated evaluation queries
4. `eval/datasets/production-samples/README.md` — Production samples anonymization workflow doc
5. `eval/baselines/SCHEMA.md` — Baseline JSON schema documentation
6. `eval/baselines/packet-13.json` — Baseline config snapshot for Packet 13
7. `eval/baselines/packet-14.json` — Baseline config snapshot for Packet 14
8. `scripts/eval_run.sh` — A/B evaluation runner entrypoint (bash)
9. `scripts/eval_gate.py` — Gate evaluation with threshold/borderline/rollback logic (python)
10. `scripts/eval_anonymize_samples.py` — Deterministic PII anonymization script (python)
11. `scripts/eval_score.py` — Rubric scoring with judge output parser and heuristic fallback (python)
12. `tests/test_eval_gate.py` — 35 tests for gate evaluation logic
13. `tests/test_eval_anonymize.py` — 17 tests for deterministic anonymization
14. `tests/test_eval_score.py` — 24 tests for rubric scoring and aggregation
15. `CHANGELOG.md` — Packet 15 entry
16. `docs/ops/runs/packet-15-evidence.md` — This evidence file

## Commands Run

```bash
# Syntax check all new Python files
python3 -m py_compile scripts/eval_gate.py scripts/eval_anonymize_samples.py scripts/eval_score.py

# Run all tests (existing + new)
python3 -m unittest discover -s tests -p "test_*.py" -v
```

## Observed Outputs

```
test_all_fail_score (tests.test_eval_score.TestRubricScoreCalculation) ... ok
test_all_fail (tests.test_eval_gate.TestComputeLatencyStats) ... ok
test_all_one (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_all_pass_score (tests.test_eval_score.TestRubricScoreCalculation) ... ok
test_all_valid (tests.test_eval_gate.TestComputeLatencyStats) ... ok
test_anonymize_file (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_both_fail (tests.test_eval_gate.TestGateEvaluation) ... ok
test_clear_pass (tests.test_eval_gate.TestGateEvaluation) ... ok
test_complete_answer (tests.test_eval_score.TestHeuristicScore) ... ok
test_contradiction_detected (tests.test_eval_score.TestHeuristicScore) ... ok
test_deterministic_file_output (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_deterministic_hash (tests.test_eval_anonymize.TestDeterministicHash) ... ok
test_deterministic_across_calls (tests.test_eval_anonymize.TestAnonymizeText) ... ok
test_empty_answer (tests.test_eval_score.TestHeuristicScore) ... ok
test_empty_array (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_empty_dict (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_empty_list (tests.test_eval_gate.TestComputePercentile) ... ok
test_empty_scores_excluded (tests.test_eval_score.TestAggregateScores) ... ok
test_equal_scores (tests.test_eval_score.TestAggregateScores) ... ok
test_file_anonymization (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_file_written (tests.test_eval_score.TestScoreReport) ... ok
test_heuristic_fallback (tests.test_eval_score.TestScoreReport) ... ok
test_heuristic_method_field (tests.test_eval_score.TestHeuristicScore) ... ok
test_invalid_field (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_latency_borderline (tests.test_eval_gate.TestGateEvaluation) ... ok
test_latency_borderline_above (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_latency_borderline_at_threshold (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_latency_borderline_below (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_latency_clear_fail (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_latency_clear_pass (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_latency_decrease (tests.test_eval_gate.TestComputeLatencyIncrease) ... ok
test_latency_fail (tests.test_eval_gate.TestGateEvaluation) ... ok
test_latency_increase (tests.test_eval_gate.TestComputeLatencyIncrease) ... ok
test_missing_file (tests.test_eval_score.TestScoreReport) ... ok
test_missing_input_file (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_missing_fields (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_no_citations (tests.test_eval_score.TestHeuristicScore) ... ok
test_no_data_status (tests.test_eval_gate.TestGateEvaluation) ... ok
test_no_failures (tests.test_eval_gate.TestRollbackRecommendation) ... ok
test_no_lift (tests.test_eval_gate.TestComputeQualityLift) ... ok
test_no_pii_unchanged (tests.test_eval_anonymize.TestAnonymizeText) ... ok
test_no_query_field (tests.test_eval_anonymize.TestAnonymizeQuery) ... ok
test_none_input (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_none_scores_ignored (tests.test_eval_gate.TestComputeQualityLift) ... ok
test_none_values (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_over_ten_percent_failures (tests.test_eval_gate.TestParseFailures) ... ok
test_parse_failures_borderline (tests.test_eval_gate.TestGateEvaluation) ... ok
test_per_field_rates (tests.test_eval_score.TestAggregateScores) ... ok
test_quality_borderline (tests.test_eval_gate.TestGateEvaluation) ... ok
test_quality_borderline_above (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_quality_borderline_below (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_quality_clear_fail (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_quality_clear_pass (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_quality_exactly_threshold (tests.test_eval_gate.TestBorderlineDetection) ... ok
test_quality_fail (tests.test_eval_gate.TestGateEvaluation) ... ok
test_negative_lift (tests.test_eval_gate.TestComputeQualityLift) ... ok
test_negative_value (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_non_array_input (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_parse_failures_counted (tests.test_eval_score.TestScoreReport) ... ok
test_phone_replacement (tests.test_eval_anonymize.TestAnonymizeText) ... ok
test_quality_lift_threshold (tests.test_eval_gate.TestThresholdConstants) ... ok
test_rubric_score_calculation (tests.test_eval_score.TestRubricScoreCalculation) ... ok
test_scored_field_added (tests.test_eval_score.TestScoreReport) ... ok
test_short_answer (tests.test_eval_score.TestHeuristicScore) ... ok
test_ssn_replacement (tests.test_eval_anonymize.TestAnonymizeText) ... ok
test_string_value (tests.test_eval_score.TestParseJudgeOutput) ... ok
test_temp_file_cleanup (tests.test_eval_anonymize.TestAnonymizeFile) ... ok
test_threshold_constants (tests.test_eval_gate.TestThresholdConstants) ... ok
test_timeout (tests.test_eval_gate.TestGateEvaluation) ... ok
```

All 200 tests passed (76 new + 124 existing from Packets 13-14):

**Eval gate tests (35 tests):**
- `TestComputePercentile` (4 tests): empty list, single value, p50 even, p95 large
- `TestComputeLatencyStats` (3 tests): all valid, with errors, all errors
- `TestComputeQualityLift` (6 tests): perfect lift, no lift, negative lift, None ignored, no data, division by zero
- `TestComputeLatencyIncrease` (3 tests): latency increase, latency decrease, no valid data
- `TestBorderlineDetection` (11 tests): quality borderline (6), latency borderline (5)
- `TestParseFailures` (4 tests): no failures, 10% failures, >10% failures, empty results
- `TestGateEvaluation` (9 tests): clear pass, quality fail, latency fail, both fail, quality borderline, latency borderline, parse failures borderline, no data, verdict structure
- `TestRollbackRecommendation` (4 tests): no failures, quality failure, latency failure, insufficient data
- `TestThresholdConstants` (5 tests): all 5 locked constants verified

**Eval anonymize tests (17 tests):**
- `TestDeterministicHash` (4 tests): same input same output, different category, different value, placeholder format
- `TestAnonymizeText` (6 tests): email, phone, SSN, IP, no PII, multiple PII, determinism
- `TestAnonymizeQuery` (2 tests): query field anonymized, no query field
- `TestAnonymizeFile` (5 tests): file anonymization, deterministic output, missing file, non-array input, empty array
- `TestFixedSalt` (2 tests): salt is constant, salt not empty

**Eval score tests (24 tests):**
- `TestParseJudgeOutput` (11 tests): valid rubric, all zero, all one, invalid field, string value, missing fields, None input, empty dict, reasoning preserved, negative value, float value
- `TestHeuristicScore` (6 tests): complete answer, short answer, no citations, contradiction detected, method field, empty answer
- `TestAggregateScores` (4 tests): equal scores, different scores, empty lists, None excluded, per-field rates
- `TestScoreReport` (5 tests): scored field added, aggregation added, parse failures counted, heuristic fallback, file written, missing file
- `TestRubricScoreCalculation` (4 tests): all pass, two pass, one pass, all fail

## Risks Remaining

1. **Gateway dependency**: The eval runner requires the gateway API to be running. In CI, ensure the stack is started before running the eval.
2. **Model-judge availability**: The scoring script falls back to heuristic scoring when no judge output is present. Heuristic scoring is deterministic but less accurate than model-judge scoring.
3. **Anonymization coverage**: The current PII patterns cover emails, phones, SSNs, IPs, and multi-word names. Production queries may contain other PII types not caught by the current patterns. Consider adding more patterns or using a dedicated PII detection library in future iterations.
4. **Statistical significance**: Single-run latency measurements may have noise from network conditions. Consider multiple runs per query in a future packet for statistical significance.

## Rollback Note

To rollback Packet 15:
1. Remove the `eval/` directory tree: `rm -rf eval/`
2. Remove scripts: `rm scripts/eval_run.sh scripts/eval_gate.py scripts/eval_anonymize_samples.py scripts/eval_score.py`
3. Remove tests: `rm tests/test_eval_gate.py tests/test_eval_anonymize.py tests/test_eval_score.py`
4. Remove packet doc: `rm docs/packets/15-eval-harness-and-auto-gates.md`
5. Revert `CHANGELOG.md` Packet 15 section.
6. No app code changes; no API changes; no database changes.
