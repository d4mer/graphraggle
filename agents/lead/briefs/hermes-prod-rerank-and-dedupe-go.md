# Hermes brief — PRODUCTION CHANGE (operator GO 2026-11-13: "approval for all"): R=2 + rerank-timeout patch + COMPANY_AWARE_DEDUPE=ON + gateway tag structure-cutover-3

Code: tag structure-cutover-3 = commit 803c1e8 (master). Bundle at ~/briefs/structure-cutover-3.bundle, sha256 3ce2dea659324064c7953f8c99204e3389a726160b9725f8eaa66b6558f7ee0d
(verify it; `git bundle verify`; build from the tag only, never a branch tip). New since structure-cutover-2: worker skip rule for manifest.json (INGEST_SKIP_BASENAMES) and terminal `skipped`
status for duplicates; chunk_token_size safety factor (STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY=3.2, ceiling 8192) for the PDF overflow; M1 asked-identifier grounding gate
(GROUNDING_CHECK_ASKED_IDENTIFIER, flag-only: adds query_scope["grounding"] and log fields, changes no answer).
Operator approved the production change; deploy window is NOW unless you find a blocker. Do it in the STAGES below, one at a time, each with its own verify and rollback. STOP at the first
failed verify, roll that stage back, report. One bare command per approval; do not retry a denied approval.

## Stage 0 — preconditions and backup (no change)
Production at rest (11 processed / 0 failed / 0 processing, :9621 and :8020 200). Stack2 stopped. Snapshot state/ingest.db (online backup) + lightrag_store + .env + compose.yml to
backups/pre-gl-deploy-<UTC>/ with sha256 manifest, copy off-host to deltahome and verify there. Record images/ids (lightrag-server, gateway, worker) and .env mtime for the end diff.

## Stage 1 — gateway/worker image (additive)
Build local/rag-gateway:structure-cutover-3 from the tag; import smoke test; `docker tag` the current gateway image as pre-gl-deploy for rollback; write + `config -q` the rollback compose.
Dry-run the reconcile/state migration on a copy of state/ingest.db: existing rows unchanged, no `accepted` duplicate rows broken (the next scan promotes legacy accepted duplicates to `skipped`).
Do not retag `latest` yet.

## Stage 2 — LightRAG: patched image + R=2
Image local/lightrag:1.5.7-reranktimeout-depth (3b39cbc9b68d). .env: RELATED_CHUNK_NUMBER=2, RERANK_TIMEOUT=900, RERANK_HTTP_TIMEOUT_SECONDS=1200. `compose up -d --no-deps --force-recreate lightrag-server`
(restart does not reload env_file). Verify: printenv in container, /health, store counts unchanged, 10-probe smoke with 0 fallback/5xx lines in docker logs, p95 sane (expect ~220 s at R=2).

## Stage 3 — gateway + worker on structure-cutover-3, dedupe ON, grounding flag ON
.env: COMPANY_AWARE_DEDUPE=true, GROUNDING_CHECK_ASKED_IDENTIFIER=true (flag-only: it must not change any answer), STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY and _CEILING left at code defaults,
confirm QUERY_LOG_PATH is set and writable so the grounding_* fields are captured. Recreate gateway + worker (--force-recreate, not restart). Verify: health, a /query returns the
same envelope plus query_scope.grounding, the worker scan leaves production documents unchanged (no re-ingest, counts identical), the 11 doc rows keep their status.

## Stage 4 — production recall and the M1 evidence run
Run probe/run_prod_recall.py (the 100 probes) against production and report counts only (correct/refused_ok/partial/halluc) vs the 10-30 baseline 86/100 and stack2 R=2 88/100.
Then the 12 unanswerable probes of the labelled sets with request_id and company recorded per row (meta.request_id from the gateway response) so the lead's scorer
(scripts/score_grounding_run.py, in the repo at 803c1e8) can join them to the query log; copy the results.jsonl and the query-log capture (counts-bearing, no corpus text beyond what the
runner already stores) to ~/work/gl08-run-exports/ equivalent on the thinkpad ~/briefs/out/ and say where. Do NOT run scoring conclusions beyond counts.

## Rules
Production-unchanged-except-intended: at the end a docker-inspect + .env diff vs the Stage 0 snapshot must show ONLY the intended changes (image tags, the five env keys). Never print keys.
Rollback for any stage: previous image tag + .env backup from Stage 0, `compose up -d --no-deps --force-recreate <service>`, then /health + counts. Do not touch stack2.

## Report
Section "PROD DEPLOY GL 2026-11-13" in structure-cutover.md, STATUS line 1 (stage reached, pass/fail, rollback if any). Lead cannot be messaged from your seat; write the file and say so in your pane.
