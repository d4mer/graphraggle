# Known-Good Config

Production runs on the **Thinkpad** in `~/rag-project`. Everything in the
"Production Runtime", "Validated URLs", "Validated Models" and "Production `.env`"
sections was read off that host on **2026-10-09** (rc1). Lines that replaced older
values are marked **(changed 2026-10-09)**; the older text described the earlier
podman/macmini setup and was wrong for what runs now.

## Validated Architecture

1. `lightrag-server` is the internal graph/index/query engine
2. `gateway-api` is the external API surface
3. `rag-ingest-worker` handles ingestion orchestration
4. `open-webui` is the conversational frontend
5. `rag-chromadb` is the vector-store container that runs alongside them

## Production Runtime (verified 2026-10-09)

Runtime is **Docker** (`docker compose`) — **not podman**. **(changed 2026-10-09)**

| Container | Image | Note |
|---|---|---|
| `rag-gateway-api` | `local/rag-gateway:p24` (image `4629dcab0bea`) | built from commit `c89b293` |
| `lightrag-server` | `ghcr.io/hkuds/lightrag:v1.5.7` | **(changed 2026-10-09)** — the repo's `compose.yml` still pins `v1.4.15`; production does not run that tag |
| `open-webui` | `ghcr.io/open-webui/open-webui:0.11.4` | pinned tag, not `main` |
| `rag-chromadb` | `chromadb/chroma:latest` | |
| `rag-ingest-worker` | image `d9d9ce2307a2` | **an old build from before packet-18.** The worker dedupe hardening (`2f74255`) is in the repo but is **not yet deployed** to this container |

Host ports (production):

1. LightRAG — `9621:9621` on the host **(changed 2026-10-09)**; the repo
   `compose.yml` maps `9622`, production maps `9621`
2. Gateway — host `8020` → container `8000` **(changed 2026-10-09)**
3. Open WebUI — host `3010` → container `8080` **(changed 2026-10-09)**

Health check: `GET http://localhost:8020/health`. **Do not** use LightRAG's
`pipeline_status` as a health check — it hangs.

## Validated URLs (production)

1. Gateway API docs: `http://localhost:8020/docs`
2. Gateway health: `http://localhost:8020/health`
3. Open WebUI: `http://localhost:3010`
4. LightRAG Web UI (admin/debug): `http://localhost:9621`

## Validated Models

The LLM, the embeddings model **and** the reranker are all served by the one
**oMLX** server at `192.168.1.190:1234`. **(changed 2026-10-09)** Older docs put
embeddings and reranking on a **different host**; that is wrong for production. The
exact binding strings are in the production `.env` (not copied here).

LLM:

```bash
LLM_MODEL=qwen3.6-35b          # served by oMLX as Qwen3.6-35B-A3B
LLM_BINDING_HOST=http://192.168.1.190:1234/v1
```

Embeddings:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1   # dimension 1024
# same oMLX server: 192.168.1.190:1234
```

Reranker:

```bash
RERANK_MODEL=jina-reranker-v3-mlx
# same oMLX server: 192.168.1.190:1234
RERANK_ENABLED=true
RERANK_BY_DEFAULT=True
RERANK_TIMEOUT=240
```

## Validated LightRAG Image

```yaml
image: ghcr.io/hkuds/lightrag:v1.5.7   # production, changed 2026-10-09
```

The repo's `compose.yml` still says `v1.4.15`. That file was deliberately not
touched by the rc1 docs pass; reconcile it in a separate code packet.

## Production `.env` Settings (non-secret, verified 2026-10-09)

```bash
REQUEST_TIMEOUT_SECONDS=1800
TIMEOUT=1800
LLM_TIMEOUT=1800
WORKER_TIMEOUT=1800
WORKERS=2
MAX_ASYNC=1
MAX_PARALLEL_INSERT=1
LLM_MODEL_MAX_ASYNC=1
EMBEDDING_FUNC_MAX_ASYNC=1
RERANK_TIMEOUT=240
RERANK_BY_DEFAULT=True
RERANK_ENABLED=true
MULTI_QUERY_ENABLED=true
MULTI_QUERY_LONG_QUERY_WORDS=20
MULTI_QUERY_REWRITE_COUNT=2
GRAPH_EXPANSION_ENABLED=false
BRIDGE_REWRITE_NO_THINK=true
```

Gateway defaults that are **in force without being written in `.env`**:

1. `BRIDGE_EMPTY_RETRIES=1` (packet-23)
2. `BRIDGE_TASK_MAX_CHARS=0` — task-prompt cap **off** (packet-24, operator decision)
3. `BRIDGE_REWRITE_SEED_LL` off (GRAG-41 seed not deployed)
4. `BRIDGE_TASK_SHORTCIRCUIT` on (GRAG-12)

## oMLX Behaviour Notes (measured 2026-10-09)

1. The `tps` oMLX reports for **non-streaming** requests includes prefill, so it is
   misleading for tiny calls. Streaming requests report decode-only.
2. Bridge answers: ~32.5k prompt tokens, ~58 tps decode, ~46 s median time to first
   token (prefill ~450 tok/s).

## Quality Baseline (rc1, 36 probes × 3 draws, automatic scoring)

1. single-fact `1.00`
2. multi-fact `0.83`
3. follow-up `0.89`
4. cross-document `0.56`
5. unanswerable-refusal `0.67`
6. 0 empty answers; median latency 178 s

## Validated Worker Rules

1. uploads stay in `uploads/`
2. `source_docs/__enqueued__` is ignored
3. text-like files ingest through `/documents/text`
4. binary docs ingest through `/documents/upload`

## Text Extensions Sent Through `/documents/text`

1. `.txt`
2. `.md`
3. `.html`
4. `.htm`
5. `.json`
6. `.csv`

## Binary/Office Extensions Sent Through `/documents/upload`

1. `.pdf`
2. `.docx`
3. `.pptx`
4. `.xlsx`

## Validated Smoke-Test Behavior

The system successfully:

1. uploaded `rag-smoke.txt`
2. ingested the full file body
3. retrieved grounded content
4. returned citations containing the full text

## Gateway Bridge Setting Added In Packet-24

1. `BRIDGE_TASK_MAX_CHARS=0` (default = off; set e.g. `24000` to enable) caps the OpenWebUI `### Task:` prompt the bridge forwards to LightRAG `mode=bypass` — 24000 chars is about 6k tokens, which protects the LLM prefix cache and decode speed. Normal (non-task) requests are never affected. The bridge log line always reports `prompt_chars`, plus `forwarded_chars` on task requests, so the size population is visible with the cap off — lengths only, never prompt text.

## Drills Verified 2026-10-09

1. **Reboot:** all 20 containers on the host (RAG stack + Plane + others) came back
   by themselves ~1 min after boot, 0 restarts (restart policies
   `unless-stopped`/`always`, Docker enabled at boot). The Hermes agent seat is
   **not** restored automatically — see `11-first-5-minutes-after-reboot.md`.
2. **Backup/restore:** SQLite online-backup API for `state/ingest.db` + a quiescent
   copy of `lightrag_store/` (218 MB in 3 s) + a sha256 manifest; restored into a
   scratch dir and verified (manifest 0 mismatches, 272 documents, 12 store files
   equal). Backups live in `~/rag-project/backups/`.
