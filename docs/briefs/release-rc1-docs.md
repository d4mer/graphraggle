# Brief: release rc1 - docs reconciliation (DOCS ONLY, no app/ or tests/ changes)

Branch: create `release-rc1-docs` FROM `packet-24-task-prompt-cap` (c89b293). Commit there. Do not push, do not merge.
Goal: the docs describe what actually runs in production on the Thinkpad, so the operator and the fleet can trust them.
Read first: docs/INDEX.md, docs/05-known-good-config.md, docs/08-change-log.md, docs/11-first-5-minutes-after-reboot.md, docs/07-openwebui-guide.md.

## Verified production facts (2026-10-09, read from the Thinkpad; use these, do not invent others)
- Host: Thinkpad, project dir ~/rag-project, runtime is DOCKER (`docker compose`), NOT podman.
- Containers/images: rag-gateway-api = local/rag-gateway:p24 (image 4629dcab0bea, built from commit c89b293); lightrag-server = ghcr.io/hkuds/lightrag:v1.5.7;
  open-webui = ghcr.io/open-webui/open-webui:0.11.4; rag-chromadb = chromadb/chroma:latest; rag-ingest-worker = image d9d9ce2307a2 (an OLD
  build from before packet-18 - the worker dedupe hardening 2f74255 is in the repo but NOT yet deployed to this container).
- Host ports: LightRAG 9621 (repo compose maps 9622; production maps 9621:9621), gateway 8020 (container 8000), Open WebUI 3010 (container 8080).
  Gateway health: GET http://localhost:8020/health. Do NOT use LightRAG `pipeline_status` as a health check (it hangs).
- LLM, embeddings AND reranker are ALL served by oMLX at 192.168.1.190:1234 (older docs say embeddings/rerank on 192.168.1.180: wrong for production).
  Models: LLM qwen3.6-35b (served as Qwen3.6-35B-A3B), embeddings mxbai-embed-large-v1 (dim 1024), reranker jina-reranker-v3-mlx.
- Non-secret production .env settings: REQUEST_TIMEOUT_SECONDS=1800, TIMEOUT=1800, LLM_TIMEOUT=1800, WORKER_TIMEOUT=1800, WORKERS=2, MAX_ASYNC=1,
  MAX_PARALLEL_INSERT=1, LLM_MODEL_MAX_ASYNC=1, EMBEDDING_FUNC_MAX_ASYNC=1, RERANK_TIMEOUT=240, RERANK_BY_DEFAULT=True, RERANK_ENABLED=true,
  MULTI_QUERY_ENABLED=true (LONG_QUERY_WORDS=20, REWRITE_COUNT=2), GRAPH_EXPANSION_ENABLED=false, BRIDGE_REWRITE_NO_THINK=true.
  Defaults in force (not in .env): BRIDGE_EMPTY_RETRIES=1, BRIDGE_TASK_MAX_CHARS=0 (cap off), BRIDGE_REWRITE_SEED_LL off, BRIDGE_TASK_SHORTCIRCUIT on.
- Deploy history (all on the Thinkpad): packet-20 keyword supply and packet-21 standalone-query rewrite deployed 2026-10-07; packet-23 empty-answer retry
  deployed 2026-10-08 (rollback tag pre-packet-23 = 31395c52e22e); packet-24 (prompt_chars/forwarded_chars logging, task-prompt cap shipped OFF) deployed 2026-10-09
  (rollback tag local/rag-gateway:pre-packet-24 = 68c1631d7dd3). Rollback command: `docker tag local/rag-gateway:pre-packet-24 local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`.
- Reboot drill 2026-10-09: all 20 containers (RAG stack + Plane + others) came back by themselves ~1 min after boot, 0 restarts (restart policy unless-stopped/always, docker enabled at boot).
  After a reboot the Hermes agent seat is NOT restored: relaunch the rig node, then start `hermes chat -c` in ~/rag-project inside its tmux pane.
- Backup/restore drill 2026-10-09: backup = SQLite online-backup API for state/ingest.db + quiescent copy of lightrag_store/ (218 MB, 3 s) + sha256 manifest; restored into a scratch dir
  and verified (manifest 0 mismatches, 272 documents, 12 store files equal). Backups live in ~/rag-project/backups/.
- oMLX facts: tps shown for NON-streaming requests includes prefill (misleading for tiny calls); streaming requests show decode-only. Bridge answers: ~32.5k prompt tokens, ~58 tps decode, ~46 s median time to first token (prefill ~450 tok/s).
- Known quality baseline (rc1, 36 probes x 3 draws, automatic scoring): single-fact 1.00, multi-fact 0.83, follow-up 0.89, cross-document 0.56, unanswerable-refusal 0.67; 0 empty answers; median latency 178 s.

## Changes
1. docs/05-known-good-config.md: replace the stale image/host/port facts with the block above (keep structure; mark what changed).
2. docs/08-change-log.md: add a dated "Production releases" section listing packets 20, 21, 23, 24, the drills, and the pending items (worker image not yet updated).
3. docs/11-first-5-minutes-after-reboot.md: rewrite for docker compose, real ports, real container names, expected 20 containers is host-specific so say "all rag-* containers + open-webui"; add the Hermes seat restore steps.
4. docs/INDEX.md "Validated Facts": update to match item 1.
5. docs/07-openwebui-guide.md: fix the Open WebUI URL/port and anything that contradicts the facts above; do not rewrite unrelated content.
6. Bring in release docs from the planning branch: `git checkout packet-22-ll-seed -- docs/ops/release-plan-first-use.md docs/briefs/` (ONLY those paths; do NOT take anything else from that branch: it carries unmerged seed code).
7. Do not copy any secret, API key, token or password into any file. Do not touch compose.yml or any file under app/ or tests/.

## Acceptance
- `git diff --stat packet-24-task-prompt-cap` shows only docs/ files.
- `grep -rn -i "podman" docs/11-first-5-minutes-after-reboot.md` is empty; `grep -rn "192.168.1.180" docs/05-known-good-config.md docs/INDEX.md` is empty.
- Suite still passes: RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q  (expect 467 passed).
- Commit on `release-rc1-docs`, hand the row back to orch-lead@graphraggle with the diff stat.
