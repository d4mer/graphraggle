# Packet 36 - count correct refusals as correct in the driver's headline score

File: scripts/overnight_loop.py (summary ~line 600-640, table ~645-675, success check ~731), tests/test_overnight_loop.py.

Problem: `correct_pct` counts only verdict == "correct". Correct refusals (`refused_ok`) on unanswerable probes are excluded, so a system that
refuses correctly scores LOWER. loop4 had dev 33 correct + 6 refused_ok + 1 wrong of 40 and reported 82.5%; the true "answered right" rate is 97.5%.

Change (no network, no LightRAG; offline tests only):
1. In the per-set summary add `ok_pct` = 100 * (correct + refused_ok) / n (same rounding/denominator as `correct_pct`), and `ok_partial_credit_pct` = ok_pct + 0.5 * partial%.
   Keep `correct_pct` and all existing keys unchanged (backward compatible; --rejudge output of old runs must still work).
2. Results tables (results.md / results-rejudged.md): add columns `dev ok%` and `heldout ok%` next to the existing ones; keep existing columns.
3. Success check (SUCCESS: ...): use heldout `ok_pct` >= 90 instead of `correct_pct`; hallucination < 5 and refusal >= 80 unchanged. Update the docstring.
4. "best experiment" ranking: rank by dev `ok_partial_credit_pct` instead of `partial_credit_pct`.
5. state.json results: add `dev_ok_pct`, `heldout_ok_pct`; keep old keys.
6. `--rejudge` must recompute ok_pct from saved verdicts, so loop4 can be re-scored without re-running anything.
Tests: a summary with 33 correct/6 refused_ok/1 wrong gives correct_pct 82.5 and ok_pct 97.5; success check passes on held-out 34 correct + 4 refused_ok + 2 hallucinated?
(ok 95, halluc 5.0 -> NOT success because halluc must be < 5) and passes on 36 correct+4 refused_ok (ok 100, halluc 0, refusal >=80); old summaries without the new keys still render.
Acceptance: `.venv/bin/python -m pytest tests/ -q` all green (634+ tests), diff limited to the two files. Report: commit hash and test count only.
