# Hermes: PRODUCTION deploy of structure-cutover-11 (Open WebUI citations door)

OPERATOR GO given 2026-11-19 ("yes, deploy it to production now"). This is a production change: snapshot first, rollback ready, stop and report at any
unexpected result. Tag `structure-cutover-11` (f66ae8f). Stack2 verification: `~/rag-project/scratch/citations/OWUI-CITATIONS-SUMMARY.md` (all pass).
The rerank speed test on stack2 may keep running; do not start any new production work beyond this brief.

## Scope (exactly this, nothing else)
1. Gateway only: `rag-gateway-api` -> `local/rag-gateway:structure-cutover-11` (image you already built from the tag; confirm its id matches stack2's `ad603be2aaa8`).
   `rag-ingest-worker` STAYS on structure-cutover-4. lightrag-server, its store, its settings: untouched (do NOT recreate it; if the shared env_file would recreate it,
   use `docker compose up -d --no-deps rag-gateway-api` and confirm lightrag-server's container id and uptime are unchanged afterwards).
2. Gateway env additions: `BRIDGE_CITATIONS_ENABLED=True`, `BRIDGE_CITATION_LIMIT=12`, `BRIDGE_CITATION_BASE_URL=rag-doc://solvay`. Nothing else in .env changes.
3. Open WebUI (0.12.0, pinned; do not change the image): append a FOURTH entry `http://gateway-api:8000/v1` to `OPENAI_API_BASE_URLS` (existing three keep their order:
   sigma :1234, sigma :18020, .180 :1234) and append a fourth key (`;<RAG_API_KEY>`) to `OPENAI_API_KEYS` / `OPENWEBUI_OPENAI_API_KEYS` positionally. Recreate only the open-webui container
   (`--no-deps`), keeping its data volume/db untouched. Open WebUI downgrade is unsupported but we are not changing its version.

## Before touching anything
- Snapshot, on-host AND a copy to deltahome (fix the destination path you noted earlier; create the dir first and verify the copy by sha256): compose.yml, .env,
  state/ingest.db, open-webui-webui.db (copy while the container is stopped OR use sqlite online backup), current gateway image ids (tag the running images `*-pre-cutover11`).
- Edit compose/.env over ssh with sed/heredoc; NEVER with your file-patch tool for anything containing a key (the `***` masking gotcha). Never print a key value.
- Record the pre-state: container ids and uptimes of lightrag-server, gateway, ingest-worker, open-webui; documents count (ingested 20); /health 200.

## Order
gateway first (verify) -> then Open WebUI. After the gateway swap, verify before touching Open WebUI:
- `/health` 200; `GET /v1/models` with bearer lists `raggle:latest`, 401 without; `lightrag:latest` still on `/api/chat`.
- One real question through `/v1/chat/completions` (stream) and one through `/api/chat`: both answer, annotations present on the first, production query log shows no errors. Use the RAC
  question. You may read the answer; counts and booleans in the report.
After the Open WebUI recreate: it is healthy, the existing three connections still work (list models from each, counts), the fourth connection verifies, `raggle:latest` is in the model picker.
The operator does the visual check of the "N Sources" chip; tell me when the UI is ready for them.

## Rollback (any failed check)
`BRIDGE_CITATIONS_ENABLED=False` is the soft rollback (door stays, no annotations). Hard rollback: restore compose.yml and .env from the snapshot, retag the `*-pre-cutover11` gateway image,
`docker compose up -d --no-deps rag-gateway-api`; remove the fourth Open WebUI entry and key, `up -d --no-deps open-webui`. Open WebUI data is restorable from the snapshot copy. After a rollback verify health and
documents count as above, then report.

## Report (summary file, path to me)
Pre/post container ids and uptimes (lightrag unchanged), image ids, the check results, timings, anything surprising, snapshot locations incl. the deltahome copy sha256. Counts only for corpus data.
