# Hermes brief — PRODUCTION CHANGE (operator GO 2026-11-17: "deploy cutover-4 to production"): gateway + worker to tag structure-cutover-4

What changes: ONLY the gateway-api and ingest-worker image, local/rag-gateway:structure-cutover-3 (3b3914a7b5fa) -> local/rag-gateway:structure-cutover-4 (12258645aacc, already built on the thinkpad from the verified bundle at tag structure-cutover-4 = 51e3c39, sha256 4c0b69d9...59fd6; verify the image id before use).
No LightRAG change, no .env value change. Code delta since -3: the grounding gate, the reranker and graph expansion now read chunk text through one shared accessor (accepts content/text/chunk_text/body, string or list) and the query log gains
`grounding_references_with_content` and `grounding_references_content_shapes`. The M1 flag GROUNDING_CHECK_ASKED_IDENTIFIER stays true and stays FLAG-ONLY: no answer, citation or status code may change.
Production is in use: do it in the stages below, one at a time, each with its own verify and rollback; STOP at the first failed verify, roll that stage back, report. One bare command per approval; do not retry a denied approval.

## Stage 0 - preconditions and backup (no change)
Production at rest (11 processed / 0 failed / 0 processing, :9621 and :8020 200, restarts 0). Snapshot state/ingest.db (online backup) + lightrag_store + .env + compose.yml to backups/pre-cutover4-<UTC>/ with a sha256 manifest, copy off-host to
deltahome and verify. Record images/ids (lightrag-server, gateway, worker, open-webui), restart counts and .env/compose mtimes for the end diff. Confirm the rollback image local/rag-gateway:structure-cutover-3 is present.

## Stage 1 - dry run (no production change)
`docker run --rm` the -4 image with dummy env: import smoke (app imports, `python -c "import app.api, app.grounding, app.citation_text"`), no network. Dry-run the state migration on a copy of state/ingest.db: the 11 ingested rows unchanged (same check as the -3 deploy).

## Stage 2 - gateway + worker on -4
compose.yml image line for gateway-api and ingest-worker -> local/rag-gateway:structure-cutover-4 (keep the backup of the previous line). `compose up -d --no-deps --force-recreate gateway-api ingest-worker` (restart does not reload env). Verify: both healthy, /health 200 on :8020, LightRAG untouched
(container id and StartedAt unchanged), worker scan leaves the 11 rows unchanged (no re-ingest), store counts identical (135 chunks / 1483 nodes / 1867 edges).

## Stage 3 - behaviour check
a) 10-probe smoke through the gateway :8020 with request_id captured (any 10 of the production recall probes): 0 5xx, 0 rerank fallback lines, every response has data.query_scope.grounding, and across the 10 `grounding_references_with_content` equals the citation count and
   `grounding_references_content_shapes` shows list (not absent/empty) - this is the first time the gate sees production evidence, report the totals. b) Answers must be unchanged in kind: spot-check 3 questions with answers from the 11-15 scored run (ids only, no text): same verdict class under your judge, or say so if not.
c) The query log still writes (logs/query_log.jsonl) and contains the two new fields. d) Open WebUI unaffected: one chat-style request through the bridge returns normally.

## Rollback (any stage)
Previous image tag local/rag-gateway:structure-cutover-3, compose.yml backup, `compose up -d --no-deps --force-recreate gateway-api ingest-worker`, then /health + counts. Do not touch LightRAG or the store on rollback. Do not touch stack2.

## Report
Section "PROD DEPLOY CUTOVER-4 2026-11-17" in structure-cutover.md, STATUS line 1 (stage reached, pass/fail, rollback if any). End diff vs the Stage 0 snapshot must show ONLY the gateway/worker image tags (and container ids/StartedAt of those two services). Never print keys.
Persist references (file_path + content shape) for any query you run. Lead cannot be messaged from your seat; write the file and say so in your pane.
