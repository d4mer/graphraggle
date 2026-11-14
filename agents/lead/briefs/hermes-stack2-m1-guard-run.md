# Hermes brief — stack2 run for the M1 gate guard metric (all 55 synthetic probes, gateway path, flag ON) (2026-11-14)

Goal: the production M1 run only covered the 12 unanswerable probes against a store that has no NC/CP corpus, so `would_newly_refuse_answerable` was vacuous. Stack2 holds the synthetic
corpora (Northwind Chemicals, Contoso Pharma) plus the 10 mirror docs, so the scoped and cross-company probes have real answers there. Run all 55 manifest probes through the GATEWAY
with the M1 flag on, record request_id per row, and score with the repo scorer. This decides whether enforcing the gate would wrongly refuse a good answer (target: 0).
STACK2 ONLY. Production untouched: docker-inspect + .env-mtime snapshot at start and end, diff empty. Synthetic data, so you may read it.

## Change as little as possible (so the result pairs with your 11-12 baseline: 27/28 scoped, 12/15 cross, 4 refused / 8 halluc of 12)
- Keep stack2 LightRAG exactly as it was for the baseline (image, RELATED_CHUNK_NUMBER=1, RERANK_TIMEOUT=600). Do NOT apply R=2 or the patched image here; only the gateway changes.
- Gateway2 (+ worker2 stays stopped; no ingest) on local/rag-gateway:structure-cutover-3 (3b3914a7b5fa, already built). stack2 .env: GROUNDING_CHECK_ASKED_IDENTIFIER=true,
  QUERY_LOG_PATH=<a writable path inside gateway2, e.g. /app/logs/query_log.jsonl, new file, empty at start>. COMPANY_AWARE_DEDUPE stays OFF (as the baseline).
- Recreate with `compose up -d --no-deps --force-recreate` (restart does not reload env). Back up the stack2 .env/compose.yml first; snapshot the store and state DB (sha256) and prove the
  counts unchanged at the end (170/1735/2298, 37 doc rows per the 11-12 teardown). No ingest.

## Run
1. Same probe runner as the baseline (scratch/gl08/probe_baseline.py, gateway :8021, mode=mix, sequential, 5 s pause, stop-at-first-5xx). Add per row: `request_id` (data/meta from the gateway
   response, the same id the query log writes), `company` (the probe's company field), and keep your existing label (correct/partial/refused/hallucinated/timeout). One row per manifest probe (55 rows).
   Guard 900 s as in the baseline. Do not re-use cached answers: clear stack2's llm_response_cache first (back it up) so the answers are fresh draws.
2. Capture the query-log file for the run. Score with the repo scorer at tag structure-cutover-3 (scripts/score_grounding_run.py; `--help`): `--manifest` once per company manifest.json
   (~/rag-project/staging-synthetic/<Company>/manifest.json), `--results` your results.jsonl, `--log` the query-log capture. If the scorer rejects a label word or field name, report the exact
   message (the vocabulary and field names it accepts are in its docstring) and fix the export, not the scorer.
3. Report counts only: the scorer's full report (per-shape tallies, gate cross-tabs, `would_newly_refuse_answerable` with the labels that would be lost, hallucinations_caught/missed,
   references_without_content, denominators), plus run latency p50/p95 and fallback/5xx counts from the gateway and LightRAG logs. Copy the three artifacts to ~/briefs/out/ (do not delete earlier files there).
4. Also state per answerable row whether `supported` was None (no identifier), True or False, as the scorer prints it, so we know how many answerable questions actually exercise the gate.

## Teardown
Restore stack2 .env/compose.yml (flag line removed, gateway image back to :structure-cutover-2 unless you need it for follow-up; say which), stop stack2, leave store + state DB untouched.

## Rules
One bare command per approval; stop on first failure and report; do not retry a denied approval; do not message the lead mid-turn (lead cannot be messaged from your seat).
## Report
Section "STACK2 M1 GUARD RUN 2026-11-14" in structure-cutover.md; STATUS line 1.
