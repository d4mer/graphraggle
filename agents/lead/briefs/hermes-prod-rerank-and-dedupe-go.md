# Hermes brief — DRAFT, NOT SENT: production change, R=2 + rerank-timeout patch + COMPANY_AWARE_DEDUPE=ON

Operator decisions 2026-11-13: (1) yes to the R=2 + patched-image change, (2) COMPANY_AWARE_DEDUPE on. Production change: do not send until the operator gives the
final go for the deploy window and the worker's ingest fixes are merged (the dedupe flag interacts with the duplicate-row fix).

Do: backup first (snapshot + sha256 manifest, off-host copy as before); image `local/lightrag:1.5.7-reranktimeout-depth` (3b39cbc9b68d); env RELATED_CHUNK_NUMBER=2,
RERANK_TIMEOUT=900, RERANK_HTTP_TIMEOUT_SECONDS=1200, COMPANY_AWARE_DEDUPE=on (needs compose up -d --force-recreate, restart does not reload env). Verify production: /health,
10-probe smoke with 0 fallback/5xx, store counts unchanged, docker-inspect + .env diff vs snapshot. Rollback: previous image tag + .env backup. Report in structure-cutover.md.
