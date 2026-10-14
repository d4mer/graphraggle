# Change Log

## Important Fixes Discovered During Setup

1. Correct LightRAG image tag is:

```yaml
ghcr.io/hkuds/lightrag:v1.5.7   # was v1.4.15; production moved on 2026-10-09 and
                                # compose.yml was reconciled on 2026-10-11
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

## 2026-10-11 — LightRAG input directory (production fix, now carried in `compose.yml`)

Production was fixed so Office/PDF files ingest on LightRAG v1.5.7; the repo
`compose.yml` now matches it so the next deploy does not undo it.

1. **Root cause.** LightRAG uses two settings for one directory: the **`--input-dir`
   CLI argument** (where `/documents/upload` writes) and the **`INPUT_DIR` environment
   variable** (where the parser looks the file up; image default `/app/data/inputs`).
   They were different, so every binary upload parsed as:
   `Parse worker failed (native): legacy source file not found`.
2. **Second cause.** The input dir also overlapped the folder the ingest worker scans
   (`source_docs/`), so uploading a file that is already there returned:
   `HTTP 409 — Input directory already contains a file with the same canonical basename`.
3. **Fix.** LightRAG gets an input dir of its own, with `--input-dir` and `INPUT_DIR`
   equal to `/data/lightrag_inputs`, mounted from `./lightrag_inputs`.
   `./source_docs:/data/source_docs` stays mounted. Canary `.docx` and `.pdf` both
   ingested afterwards.
4. **Operator requirements.** `lightrag_inputs/` must **exist on the host and be
   world-writable (`777`)** before the first start (`mkdir -p lightrag_inputs && chmod
   777 lightrag_inputs`). It is gitignored. **Removing/recreating that host directory
   leaves the bind mount stale — restart `lightrag-server` after doing it.**
5. **Ports/images now in `compose.yml`:** LightRAG `v1.5.7` on host `9621` (was
   `v1.4.15` / `9622`), gateway host `8020` (was `8000`), Open WebUI `0.11.4` on host
   `3010` (was `main` / `3000`).
6. **Secrets.** Open WebUI's HyperQwen key is parameterised:
   `OPENAI_API_KEYS: "${OPENWEBUI_OPENAI_API_KEYS:-}"`. Set **`OPENWEBUI_OPENAI_API_KEYS`**
   in the host `.env`; no key value ever goes in the repo. There is no `.env.example`.
7. **Guard test:** `tests/test_compose_config.py` fails if `--input-dir` and `INPUT_DIR`
   ever diverge, if the input dir becomes the worker-scanned `source_docs` dir, if an
   image tag drifts to `latest`/`main`, or if a literal key value appears under any
   `*KEY*` environment entry.

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
2. ~~The repo `compose.yml` still pins `ghcr.io/hkuds/lightrag:v1.4.15` and maps LightRAG on host port `9622`.~~ **Resolved 2026-10-11 (packet-26):** `compose.yml` now pins `v1.5.7`, maps `9621:9621`, gives LightRAG its own `INPUT_DIR`/`--input-dir`, and is covered by `tests/test_compose_config.py`.
3. `BRIDGE_REWRITE_SEED_LL` (GRAG-41 seed) is off and undeployed.
4. Repo changes not yet deployed (worker image is still pre-packet-18): TODO #2 lifecycle table and compare-and-set write-backs, TODO #3 upload/scan semantics, `COMPANY_AWARE_DEDUPE` (default off, see `05-known-good-config.md`), `tests/conftest.py`. Deploy is an operator decision.
