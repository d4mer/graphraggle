# Change Log

## Important Fixes Discovered During Setup

1. Correct LightRAG image tag is:

```yaml
ghcr.io/hkuds/lightrag:v1.4.15
```

2. Correct embedding model is:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
```

3. Text files must ingest via LightRAG `POST /documents/text`

4. Binary/office files should continue using `POST /documents/upload`

5. Ignore `source_docs/__enqueued__` in the worker

6. Uploads should remain in `uploads/`

7. `gateway-api` and `ingest-worker` must use local build config and `pull_policy: never`

8. LightRAG container command must pass flags only, not `lightrag-server` again

9. Gateway-to-LightRAG networking was validated internally using:

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

10. Final smoke test validated full retrieval and citations from `rag-smoke.txt`

## Packet-24 — OpenWebUI task prompts are capped before forwarding

1. `BRIDGE_TASK_MAX_CHARS` (default `0` = off; set e.g. `24000` to enable) bounds the `### Task:` prompt the bridge forwards to LightRAG `mode=bypass`, where no retrieval truncation runs. When enabled the bridge keeps the first 25% of the budget (the task header) and the last 75% (the most recent chat turns), joined by `[...truncated...]`. The bridge log line always gains `prompt_chars`, plus `forwarded_chars` on task requests — lengths only, never prompt text, and they are on whether or not the cap is.

## Production Releases

All deploys are on the Thinkpad (`~/rag-project`, Docker). Rollback is always
`docker tag local/rag-gateway:<rollback-tag> local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`.

1. **2026-10-07 — packet-20** (gateway-supplied high/low-level keywords) deployed.
2. **2026-10-07 — packet-21** (standalone-query rewrite of follow-ups before retrieval) deployed.
3. **2026-10-08 — packet-23** (one retry when `/query/stream` returns an empty answer) deployed. Rollback tag `local/rag-gateway:pre-packet-23` = `31395c52e22e`.
4. **2026-10-09 — packet-24** (`prompt_chars` / `forwarded_chars` bridge logging; task-prompt cap shipped **OFF**, `BRIDGE_TASK_MAX_CHARS=0`) deployed as image `local/rag-gateway:p24` (`4629dcab0bea`, built from `c89b293`). Rollback tag `local/rag-gateway:pre-packet-24` = `68c1631d7dd3`.
5. **2026-10-09 — reboot drill:** all 20 containers on the host came back by themselves ~1 min after boot with 0 restarts; the Hermes agent seat does not come back and must be relaunched (see `11-first-5-minutes-after-reboot.md`).
6. **2026-10-09 — backup/restore drill:** SQLite online-backup API for `state/ingest.db` + quiescent copy of `lightrag_store/` (218 MB, 3 s) + sha256 manifest; restore verified (0 manifest mismatches, 272 documents, 12 store files equal). Backups in `~/rag-project/backups/`.
7. **2026-10-09 — rc1 quality baseline** (36 probes × 3 draws, automatic scoring): single-fact 1.00, multi-fact 0.83, follow-up 0.89, cross-document 0.56, unanswerable-refusal 0.67; 0 empty answers; median latency 178 s.

### Pending

1. `rag-ingest-worker` is still running image `d9d9ce2307a2`, an old build from before packet-18. The worker dedupe hardening (`2f74255`) is in the repo but **not deployed** to that container.
2. The repo `compose.yml` still pins `ghcr.io/hkuds/lightrag:v1.4.15` and maps LightRAG on host port `9622`; production runs `v1.5.7` on host port `9621`. Not reconciled here — this pass was docs only.
3. `BRIDGE_REWRITE_SEED_LL` (GRAG-41 seed) is off and undeployed.
