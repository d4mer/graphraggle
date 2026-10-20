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

## Production Runtime (verified 2026-10-15)

Runtime is **Docker** (`docker compose`) — **not podman**. **(changed 2026-10-09)**

| Container | Image | Note |
|---|---|---|
| `rag-gateway-api` | `local/rag-gateway:rc2` (image `d0f31398fec5`) | **(changed 2026-10-15)** built from tag `rc-2` = commit `11b1833`; previous image `4629dcab0bea` (`p24` = rc-1) is tagged `local/rag-gateway:pre-rc2` for rollback |
| `lightrag-server` | `ghcr.io/hkuds/lightrag:v1.5.7` | unchanged since **2026-10-09**; the repo `compose.yml` was reconciled to this tag on **2026-10-11** (packet-26) |
| `open-webui` | `ghcr.io/open-webui/open-webui:0.12.0` | **(changed 2026-10-14)** canary on a data copy, then cutover; DB migrated (6 revisions), **downgrade unsupported**, rollback = restore the backup tarball. Pinned tag, not `main` |
| `rag-chromadb` | `chromadb/chroma:latest` | |
| `rag-ingest-worker` | `local/rag-gateway:rc2` (image `d0f31398fec5`) | **(changed 2026-10-15)** same image as the gateway. Correcting the stale note that used to sit here: the worker was **not** on a pre-packet-18 build (`d9d9ce2307a2`) — it has run rc-1 (`4629dcab0bea`) since **2026-10-10**, so packet-18's dedupe hardening was already live. Worker rollback uses `compose.rollback-rc2.yml`, not a retag — that override file lives on the Thinkpad and is **not** committed in this repo (see `08-change-log.md`) |

Host ports (production):

1. LightRAG — `9621:9621` on the host **(changed 2026-10-09)**; the repo
   `compose.yml` now maps `9621` too **(reconciled 2026-10-11**, it used to map `9622`**)
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
EMBEDDING_MODEL=bge-m3-mlx-fp16   # dimension 1024, real window 8192 tokens
# same oMLX server: 192.168.1.190:1234
```

BGE-M3 is the only embedder since 2026-10-18 (operator decision). mxbai-embed-large-v1 is
retired: its 512-token window is what made 1200-token chunks fail silently. Nothing in the
stack should name mxbai any more; older change-log entries keep the name because they
describe what was measured at the time.

Structure chunking (the worker cuts, LightRAG obeys) — defaults as shipped:

```bash
STRUCTURE_CHUNKING_ENABLED=true
STRUCTURE_CHUNKING_MAX_PIECE_TOKENS=7500   # window minus margin; NOT a size target
STRUCTURE_CHUNKING_MIN_PIECE_TOKENS=150    # merge floor: a smaller unit welds to its neighbour
STRUCTURE_CHUNKING_MERGE_UNTIL=floor       # floor: one natural unit per piece; window: pack to 7500
STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS=8192 # BGE-M3's window, used for the fit check
STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS=64    # headroom added to chunk_token_size
STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=   # BGE-M3 tokenizer.json dir; empty = conservative over-count
STRUCTURE_CHUNKING_EMBED_URL=http://192.168.1.190:1234/v1  # topic cuts only; empty = no topic pass
STRUCTURE_CHUNKING_EMBED_MODEL=bge-m3-mlx-fp16
STRUCTURE_CHUNKING_EMBED_API_KEY_ENV=OMLX_API_KEY  # the NAME of the variable, never the key
STRUCTURE_CHUNKING_TOPIC_THRESHOLD=0.30    # TextTiling depth that counts as a topic change
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

The repo `compose.yml` now pins the same tag **(reconciled 2026-10-11**, packet-26**)**.

## LightRAG Input Directory — the 409 / "legacy source file not found" cause (2026-10-11)

LightRAG v1.5.7 uses **two different settings for one directory**, and they must be
equal:

1. the **`--input-dir` CLI argument** — where `/documents/upload` **writes** the file
2. the **`INPUT_DIR` environment variable** — where the **parser looks the file up**
   (image default: `/app/data/inputs`)

When they differ, every uploaded binary fails in the parser with:

```
Parse worker failed (native): legacy source file not found
```

The input dir must also be **separate from the folder the ingest worker scans**
(`source_docs/`). If it is the same folder, `/documents/upload` refuses the file with:

```
HTTP 409 — Input directory already contains a file with the same canonical basename
```

Production therefore runs LightRAG with its **own** input dir, and the repo
`compose.yml` now matches:

```yaml
environment:
  INPUT_DIR: /data/lightrag_inputs     # MUST equal --input-dir below
command:
  - --input-dir
  - /data/lightrag_inputs
volumes:
  - ./lightrag_inputs:/data/lightrag_inputs
  - ./source_docs:/data/source_docs    # still mounted for the worker's dedupe/state view
```

Operator facts about `lightrag_inputs/`:

1. The host directory **must exist before the first start** and be **world-writable
   (`777`)** — LightRAG runs as a non-root user inside the container and uploads fail
   with a permission error otherwise:
   `mkdir -p ~/rag-project/lightrag_inputs && chmod 777 ~/rag-project/lightrag_inputs`
2. It is a runtime directory and is **gitignored** (`lightrag_inputs/`), like
   `source_docs/`.
3. **Removing and recreating the host directory leaves the bind mount stale** — the
   running container keeps pointing at the deleted inode. Restart `lightrag-server`
   after doing that.
4. Canary `.docx` and `.pdf` both ingest after this change (verified on production
   2026-10-11).

## Open WebUI — HyperQwen connection (non-secret values in `compose.yml`)

`compose.yml` carries `OPENAI_API_BASE_URLS`, `OPENCLAW_BRIDGE_STATE_DIR` and
`extra_hosts: host.docker.internal:host-gateway` for the HyperQwen endpoints. The key
itself is **never** in the repo:

```yaml
OPENAI_API_KEYS: "${OPENWEBUI_OPENAI_API_KEYS:-}"
```

Set **`OPENWEBUI_OPENAI_API_KEYS`** in the host `.env` file (the same file as every
other secret). There is no `.env.example` in this repo, so the variable is documented
here and in the change log only.

> **Known drift, accepted (2026-10-15):** the live `compose.yml` on the Thinkpad
> currently **inlines** the Open WebUI OpenAI key value instead of using the `${...}`
> reference shown above. The repo form stays parameterised, which is what
> `tests/test_compose_config.py` enforces. The operator has accepted the difference as
> a risk (single-user machine on a LAN) and is not scheduling a rotation. No key value
> belongs in this repo or in any doc.

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

## Settings Shipped Dark (default off, operator decision to enable)

1. `COMPANY_AWARE_DEDUPE=false` (default). The duplicate rule is company-blind: a
   document whose SHA-256 matches any active row is skipped as `duplicate_content`,
   even when that row belongs to another company, so a company-scoped upload of
   identical bytes is silently never submitted and can never be cited by its owner.
   Set `true` to let a company-scoped document through when the only SHA match
   belongs to another company (or is unscoped). Company comparison is strip-only,
   no case folding, exactly as query scoping does it.
   **Do NOT enable.** The live probe (2026-10-14; full report on the Thinkpad at
   `~/rag-project/docs/ops/runs/company-dedupe-probe.md`, run reports are not
   versioned) showed LightRAG v1.5.7 dedupes by
   content hash globally: an identical-bytes second submit under another filename
   returns HTTP 200 with a new track, but creates a `dup-*` FAILED placeholder
   (0 chunks, never PROCESSED) and does not index the content again. With this
   flag on, a company-B upload of company-A's bytes would end as a visible
   `failed` row and every retry would add another junk `dup-*` row upstream. It
   does not make the document queryable by company B. The correct fix is
   attribution at query time (one indexed copy serving every company that owns
   identical bytes), tracked as its own item.
   The probe also showed that submitting **different** bytes under an already-used
   `file_source` returns a synchronous **HTTP 409** ("Document storage already
   contains ... Delete the existing record before re-inserting"). The worker sends
   `file_source=<full path>` and never deletes upstream documents, so editing a
   source file in place is expected to hit this 409 and stay stuck as
   `submit_terminal_failed` until an operator reindex (inferred from the probe and
   the code, not yet reproduced end to end).

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
