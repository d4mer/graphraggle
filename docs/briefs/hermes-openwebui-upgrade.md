# Hermes brief (DRAFT, needs operator approval): upgrade Open WebUI 0.11.4 -> 0.12.0

**Status: DRAFT. Do not execute until the operator approves.** Production change,
but only the `open-webui` container; gateway, LightRAG and the worker are not
touched. No corpus or chat content is to be read at any point.

## Why / what changes
Current: `ghcr.io/open-webui/open-webui:0.11.4` (2026-09-21), container
`open-webui`, volume `rag-project_open-webui-data` (chat history, settings,
SQLite DB), port 3010, Ollama connection `http://gateway-api:8000`.
Target: **`0.12.0`** (2026-10-10, stable). Pin the explicit tag, never
`latest`/`main`/`stable` (tests/test_compose_config.py enforces it).
Release-note points that touch us: Ollama connection headers/auth now applied to
every request; Ollama replies cut by a token limit report `length` instead of
`stop`; provider errors now pass through the provider's status code; DB
migrations run on first start (one noted: an index for knowledge-base file
lookups). Not stated either way: any change to title/tag/follow-up task prompts.
Our bridge short-circuits OpenWebUI task prompts by their `### Task:` prefix, so
that is the thing to verify, not assume.

## Hard rules
- Open WebUI migrates its database on first start and does not support
  downgrade. **Never start 0.12.0 against the live volume before a verified
  backup exists, and never run the canary against the live volume.**
- Keep the backup on the Thinkpad. The volume holds old chat history that quotes
  data we agreed to remove; do not copy it elsewhere, do not read it.
- Do not change any other service, setting or key. Do not enable any new Open
  WebUI feature. No restart of gateway-api, lightrag-server or the worker.
- Ingestion may be running (docx/pdf being extracted). Open WebUI is independent
  of ingestion; do not stop or slow the worker or LightRAG.

## Steps
1. **Pre-flight (record, do not change):** `docker ps` line for open-webui;
   `curl -s localhost:3010/api/version` (or the health endpoint); gateway
   `/api/tags` returns the model; current image id; record the last 20 gateway
   bridge log lines' field names only (not prompts) for a before/after compare.
2. **Backup:** stop only `open-webui`; tar the volume directory
   (`/var/lib/docker/volumes/rag-project_open-webui-data/_data`) to
   `~/rag-project/backups/openwebui-0.11.4-<UTC-stamp>.tar.gz`; verify with
   `tar -tzf` and record size and sha256; start `open-webui` again; confirm
   healthy. Downtime is this step only (about a minute).
3. **Canary (separate container, COPY of data, different port):**
   - extract the backup into a scratch dir `~/rag-project/scratch/owui-canary-data`;
   - `docker pull ghcr.io/open-webui/open-webui:0.12.0`;
   - run `owui-canary` from 0.12.0 with `-p 3011:8080`, the same environment as
     compose (WEBUI_AUTH=False, ENABLE_OLLAMA_API=True,
     OLLAMA_BASE_URLS=["http://gateway-api:8000"], same network as gateway-api)
     and the scratch dir mounted at `/app/backend/data`. Use the SAME secret
     handling as production (do not print keys).
   - Check: container healthy; migration completed without error in
     `docker logs owui-canary` (report error lines only); `GET :3011` loads;
     the gateway model is listed; send ONE short synthetic chat ("canary:
     reply with the single word ok") and confirm an answer arrives with the
     gateway's Sources block behaviour unchanged; confirm the title/tag
     generation task that Open WebUI fires after a chat is short-circuited by the
     bridge (gateway log shows the task shortcircuit marker for it, with
     `prompt_chars`/`forwarded_chars` fields present) rather than a full
     retrieval.
   - Compare gateway bridge log field names with step 1. Report any field that
     disappeared or any new error line.
4. **Go/no-go for cutover (Hermes reports, operator decides):** only if every
   canary check passed. If any failed, remove the canary, change nothing else,
   report.
5. **Cutover (only after operator "go" in writing):** `docker rm -f owui-canary`;
   remove the scratch dir; edit `~/rag-project/compose.yml` image tag to
   `0.12.0` (exactly that one line; print only that line's diff);
   `docker compose up -d --no-deps open-webui`; wait healthy; repeat the single
   synthetic chat on :3010; confirm `/api/tags` through the UI and the task
   short-circuit again.
6. **Report** to `~/rag-project/docs/ops/runs/openwebui-0.12.0-upgrade.md`:
   versions before/after, backup path+sha256, canary results, migration log
   verdict, any warnings, and the exact rollback commands below. Counts and
   shapes only; no chat or corpus content.

## Rollback (state in the report; run only on operator instruction or if the
cutover health check fails)
1. `docker compose stop open-webui`
2. restore the pinned line in `compose.yml` to `0.11.4`
3. restore the data: move the current volume dir aside
   (`..._data.0.12.0-failed`) and extract the backup tarball into a fresh
   `_data` directory (the DB was migrated; downgrade of the migrated DB is
   unsupported, so the backup is the rollback, not the old image alone)
4. `docker compose up -d --no-deps open-webui`; confirm 0.11.4 healthy.
Any chats created after cutover are lost by this rollback; say so in the report.

## Out of scope
Any configuration, key rotation, new features, other services, ingestion.
