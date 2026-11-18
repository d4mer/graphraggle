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

Structure chunking (the worker cuts, LightRAG obeys) — **code defaults as shipped**, read from
the `Field(default=...)` in `app/config.py` (verified 2026-10-21). Every enable flag in this
feature ships **false**: out of the box the worker submits text and uploads exactly as it did
before the chunker existed. `tests/test_docs_defaults_match_config.py` fails if this block and
the code drift apart again.

```bash
STRUCTURE_CHUNKING_ENABLED=false           # off as shipped - see "to turn it on" below
STRUCTURE_CHUNKING_MAX_PIECE_TOKENS=7500   # window minus margin; NOT a size target
STRUCTURE_CHUNKING_MIN_PIECE_TOKENS=150    # merge floor: a smaller unit welds to its neighbour
STRUCTURE_CHUNKING_MERGE_UNTIL=floor       # floor: one natural unit per piece; window: pack to 7500
STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS=8192 # BGE-M3's window, used for the fit check
STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS=64    # headroom added to chunk_token_size
STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY=3.2  # relative headroom on that same number
STRUCTURE_CHUNKING_CHUNK_TOKEN_CEILING=8192 # never ask for a limit above the embedder window; 0 = no cap
STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=   # BGE-M3 tokenizer.json dir; empty = conservative over-count
STRUCTURE_CHUNKING_EMBED_URL=              # empty by default = NO topic pass (deterministic fallback)
STRUCTURE_CHUNKING_EMBED_MODEL=bge-m3-mlx-fp16
STRUCTURE_CHUNKING_EMBED_API_KEY_ENV=OMLX_API_KEY  # the NAME of the variable, never the key
STRUCTURE_CHUNKING_TOPIC_THRESHOLD=0.30    # TextTiling depth that counts as a topic change
```

`chunk_token_size` is not a budget: it is the limit we ask LightRAG to enforce over pieces it is
not allowed to re-cut, and it is computed per document as
`ceil(largest_piece_in_our_units x SAFETY) + FIT_MARGIN`, capped at `CHUNK_TOKEN_CEILING` and never
below the pre-factor `largest + FIT_MARGIN`. The **safety factor** exists because the counter that
prices a piece (`conservative-overcount`, since `requirements.txt` carries no tiktoken)
under-prices the shapes a PDF/DOCX extraction produces. Measured against a cl100k pre-tokenizer
lower bound (`tests/test_pdf_chunk_token_overflow.py`): prose 0.62x (safe), JSON-ish text 1.04x,
code 1.55x, PDF pipe tables 1.94x, dotted number runs 3.00x; the two stack2 rejections sat at 1.21x
and 1.145x. 3.2 is the worst measured ratio plus headroom, and because those ratios are *lower*
bounds it bounds the cases we measured, not every case there is. The shapes in that list are
fixtures in the test file and reproduce here; the two stack2 figures were measured on the GL-07/08
source PDFs, which are not in this repo - re-measure them with `pdftotext` plus a cl100k
pre-tokenizer if those files are on hand. **Counting in LightRAG's own
tokenizer family (install tiktoken, or vendor cl100k) remains the proper fix**; the factor is the
interim. The factor moves no piece boundary — the submitted bytes are byte-identical with it on or
off, pinned by sha256 in that test file. On the row, `structure_chunking_chunk_token_safety`,
`..._largest_piece`, `..._ceiling`, `..._uncapped` and `..._capped` say what was applied, so a later
`ChunkTokenLimitExceededError` is readable without a re-run.

**To turn it on** — recommended for production, **not** defaults; these go in the production
`.env` (and only `STRUCTURE_CHUNKING_ENABLED` is the chunker itself):

```bash
STRUCTURE_CHUNKING_ENABLED=true
WORKER_PDF_EXTRACT_ENABLED=true      # false as shipped; only reached when chunking is on
WORKER_DOCX_EXTRACT_ENABLED=true     # false as shipped; same condition
STRUCTURE_CHUNKING_EMBED_URL=http://192.168.1.190:1234/v1   # topic cuts; empty = no topic pass
```

`WORKER_PPTX_EXTRACT_ENABLED` is the odd one out: it ships **true**, so with
`STRUCTURE_CHUNKING_ENABLED=true` the worker already extracts `.pptx` itself with no extra
flag, while `.pdf` and `.docx` stay on the `/documents/upload` route until their flags are set.
Any extraction failure falls back to `/documents/upload` - a document is never lost to a parser.

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

2. `GROUNDING_CHECK_ASKED_IDENTIFIER=false` (default). When on, the gateway records in
   `query_scope.grounding` whether the identifier the **question** named appears verbatim
   (token match) in the references of the pass that produced the answer, and mirrors the
   counts into the query log (`grounding_verdict`, `grounding_asked`,
   `grounding_unsupported`, `grounding_evidence_pass`,
   `grounding_references_without_content`, `grounding_references_with_content`,
   `grounding_references_content_shapes` - the last two count references by the shape their content
   arrived in: `string`, `list`, `empty`, `absent`). Evidence is read the way the reranker and the
   graph scanner read it (`app/citation_text.py`: `content`/`text`/`chunk_text`/`body`, string or
   list), so the guard cannot be narrower than the readers around it. **Flag only**: it never changes the answer, the
   citation list or the status code, and with the flag off the response and the upstream
   payload are byte-identical to before it existed. It exists because the GL-08 synthetic
   baseline answered 8 of 12 unanswerable probes with an outcome for a code that is in no
   document, while similarity scores of answerable and unanswerable probes overlap and
   cannot separate them. On the GL-08 stack2 run (2026-11-09; that run's export was an
   off-repo artefact and is not versioned, so these two figures are quoted from the handback
   report, not reproducible from this repo): the rule would have fired on 4 of the 8
   hallucinations and on **0** of the 43 answerable probes, because no answerable probe names
   an identifier in its question. It cannot therefore cost scoped or cross-company recall -
   and it also says nothing about those probes. What IS reproducible in-repo is the corpus
   half of the finding (`python scripts/unanswerable_grounding.py <generated corpus>`): 0 of
   12 unanswerable codes appear in any document chunk while all 12 appear in `manifest.json`,
   712 same-company lookalikes per probe, and chunker line-wrapping that makes identifier-
   level (not sentence-level) matching the only durable evidence test. Off until one measured
   run on the real probe sets decides whether it ever acts on the verdict.
   **Measuring it:** set `GROUNDING_CHECK_ASKED_IDENTIFIER=true` plus `QUERY_LOG_PATH` for the
   run, then `python scripts/score_grounding_run.py --manifest <company manifest.json>...
   --results <the runner's *.results.jsonl> --log <that query-log capture>`. It prints counts
   only — no question text, no answers, no reference content — and its headline guard is
   `would_newly_refuse_answerable`, which must be 0. It refuses to score rather than guess: an
   export row it cannot join, a label it does not recognise, a repeated question with no
   `request_id` to disambiguate, or a run with no query log all stop it. Note that the GL-08
   55-row run is only 41 distinct questions because generated scoped probes repeat a question
   text within a company (several facts share one topic label); the scorer reports rows and
   distinct questions separately so no rate hides that.
3. `COMPANY_FROM_QUESTION=false` and `COMPANY_ALIASES=""` (defaults). With the flag on, a company
   named in the question text scopes the query the same way the explicit `company` request field
   does, and an explicit field still wins. The known-company list is `SELECT DISTINCT company`
   over the gateway's own `documents` rows, so only a company the store actually has documents for
   can scope anything; `COMPANY_ALIASES` maps what a person types to that directory name
   (`Northwind Chemicals=nwind,north wind;Contoso Pharma=contoso`) and aliases naming a company
   with no rows are ignored rather than trusted. Matching is case- and punctuation-insensitive and
   whole-phrase, longest name first; a one-word company whose name is also an ordinary English
   word only matches when the question capitalises it, because "what did the target say" must not
   scope to a company called Target. A question naming an organisation the store does not know
   stays unscoped (today's behaviour) and records what it saw as `company_candidates` in the log
   line, which is how the operator finds out what to alias.
   `COMPANY_SCOPE_ENFORCEMENT` decides how far the scope reaches:
   - `citations` (default) restricts the citations the gateway returns and the Sources block the
     user sees. The answer is still whatever LightRAG produced from everything it retrieved.
   - `retrieval` answers from the named companies' material only: retrieve with chunk content, drop
     every reference whose `documents` row is not one of the named companies, and generate again
     from what is left (`app/scoped_pass.py`, `mode: bypass` - the retrieve-then-generate pattern
     `app/generation.py` already ships). The evidence-adding stages (multi-query, naive fallback,
     graph expansion) are skipped while that pass runs, because they would put the other companies'
     material back in.
   The retrieval leg uses LightRAG's retrieval-only `POST /query/data`, so `retrieval` does **not**
   cost an extra model call - one retrieval plus one generation, like an unscoped question. If
   `/query/data` is unavailable or returns no usable chunk text the pass falls back to retrieving
   through `/query` (which does force a discarded generation) and logs
   `scoped_pass_retrieval_route` / `..._retrieval_fallback_reason` / `..._model_calls` so the route is
   visible rather inferred. If **both** retrieval routes are rejected by LightRAG the request is not
   failed: the pass reports `scoped_pass_declined_reason=retrieval_routes_failed:<codes>` and logs
   `scoped_pass_retrieval_errors` (status code plus the server's validation message, never document
   text), and the answer is served the way `citations` enforcement serves it — the ordinary answer with
   the company filter still applied to the citations. Which of `data.references` or `data.chunks` carries the chunk text,
   and under which key, has not been observed on this stack: both are read, chunk items are grouped by
   document path, and the counts of each are logged (`scoped_pass_retrieval_shape`).
   The citation→document join is shared by every door (`app/document_identity.py`) and is ordered:
   the recorded `documents.submitted_source`, then exact `path`, then `filename` /
   `original_filename`, then the citation's **basename** against those columns and the tail of `path`,
   then content-hash widening. The basename step is not a convenience: LightRAG strips directories from
   `file_source`, so a citation's `file_path` is a bare filename while `documents.path` is absolute —
   an exact-path join attributed nothing on the filesystem door (stack2 R0: 815 citations, company set
   on 0). One stored name holding **different bytes for different companies** is never attributed: the
   citation is dropped and counted (`scoped_pass_references_ambiguous`,
   `ambiguous_citation_count` on a citation scope) because guessing here credits one company with
   another's document.
   `COMPANY_QUALIFIED_FILE_SOURCE` (default false) makes that case impossible for **new** ingests: the
   text door submits `<company-slug>--<basename>` and records it in `documents.submitted_source`, the
   key the join prefers. It changes no bytes, no chunk boundaries and no chunker header (a test submits
   the same file with the flag on and off and compares the payload); the upload door already stores a
   unique `<uuid>--<name>`, and its name is now recorded rather than inferred. Treat it as a policy for
   future files: re-submitting an existing document under a new name creates a second LightRAG document,
   and every existing row keeps resolving through the basename rule.
   `scripts/gl10_basename_audit.py` counts the cases apart over a corpus or a state database, which is
   how you decide whether a corpus needs that re-ingest or the join alone is enough.
   The cap is `COMPANY_SCOPED_CONTEXT_MAX_CHARS` (default 12000 characters) and it binds: a stack2 run
   dropped 100 kept references to it. The kept set is a **prefix of the retrieval order** — the first
   block that does not fit closes the fill — so a lower-ranked chunk is never in the prompt while a
   higher-ranked one from the same retrieval is withheld. Only the highest-ranked block may be cut to
   the budget, and the cut is marked in the text so it cannot be quoted as complete evidence. The log
   reports `scoped_pass_context_blocks_kept`, `..._context_chars`, `..._context_unused_chars`,
   `..._context_truncated_blocks` and `..._context_first_dropped_index` (where the ranked list stopped
   being kept), which is how you tell "the cap is too small" from "retrieval found the wrong things".
   `COMPANY_SCOPED_HARD_REFUSAL` (default **false**) refuses without asking the model when the question
   names an identifier (a code like `NC-3617`) that appears nowhere in the material the pass chose to
   send. It reuses the shipped `app/grounding.py` identifier and verbatim-token rules and reports the M1
   vocabulary (`scoped_pass_hard_refusal_verdict=no_support_for_asked_identifier`), never the code or
   the chunk text. Off by default because that classifier's false-refusal rate was measured on synthetic
   probes and never on a dev set of answerable questions: measure it on a stack run before letting it
   decide what a person reads.
   `COMPANY_KEYWORD_HINT` (default false) is a separate opt-in that names the company in the
   `hl_keywords`/`ll_keywords` of that retrieval; it changes recall, never the guarantee.
   **The trade-off that remains is the operator's call.** On the Open WebUI bridge `retrieval` loses
   the token stream (the answer arrives at the end, not
   progressively). It is also only as good as attribution: a reference is kept only when its
   `file_path` joins a `documents` row, and upload-door citations carry a basename that can never
   equal the stored path, so on an upload-ingested corpus they are dropped as `unattributable` and a
   scoped answer can come back "I could not find it". That count is in the log line
   (`scoped_pass_references_unattributable`) precisely so a run can be read without guessing.
   `COMPANY_SCOPED_CONTEXT_MAX_CHARS` (default 12000) caps the second call's prompt by dropping
   whole references, never part of one.
   The query log line carries `company_scope` (companies matched, where from, candidates) and the
   scoped-pass counts, so a stack2 run can be scored for scoping before anything else changes. The
   open questions that could make this cheaper - does v1.5.7 `/query` accept a document/path filter,
   or support per-company workspaces - are listed in
   `docs/decisions/gl10-company-from-question-map.md`; nothing in this packet assumes an answer.

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
5. `INGEST_SKIP_BASENAMES` (default `manifest.json`) is never submitted. The rule
   matches the whole basename, case-insensitively, and never an extension, so a real
   `orders.json` still ingests. A refused file gets a terminal `skipped` row naming
   the rule - visible to an operator, not a failure row and not silence. Added for
   GL-07/08: every synthetic company folder carries a harness `manifest.json`, and in
   every folder it was submitted and failed on chunk size.
   **Limit:** `/upload` stages files as `<id>--<name>`, so an API-uploaded
   `manifest.json` is not matched by a basename rule and is ingested like any other
   document. Only the filesystem scan door is covered.

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
