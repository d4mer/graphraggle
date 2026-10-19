# Hermes brief: structure-aware ingest of the 10 real documents on stack2 (BGE-M3), then retrieval check

**Status: APPROVED by the operator on 2026-10-19 for actions 1 to 4 only ("approve 1 to 4 and send it to
Hermes"). Action 5 (queries against production) is NOT approved: do not do it.** Approval prompts appear in your
pane. Anything not listed in 1 to 4 is out of scope.
Production (compose project rag-project, LightRAG :9621, gateway :8020, Open WebUI :3010, the live worker,
`.env`, `source_docs/`, `uploads/`, `lightrag_store/`, `state/ingest.db`) is NOT changed by anything here.
Report to `~/rag-project/docs/ops/runs/stack2-structure-ingest.md` (files, not `rig send`); STATUS on line 1.

## Why this run exists
Offline checks (chunkrun5-7) show the structure-aware build produces sound pieces on the real documents:
130 pieces, 0 over BGE-M3's 8192-token window, 100% text coverage, 0 worker fallbacks, probe evidence kept
whole. What nobody has seen is LightRAG ingest those pieces: entity/relation extraction per piece,
embedding with BGE-M3, the oversize check you read in the source, and real retrieval. This is that one
end-to-end run, on the scratch stack, to decide whether to bring a cutover to the operator. It is a gate
on one build, not a comparison of options.

## Approved actions (1 to 4); action 5 is not approved
1. **Back up, then empty stack2's LightRAG store** (`~/rag-project/stack2/lightrag_store/`): copy it to
   `stack2/backups/pre-structure-<UTC>/` with a sha256 manifest first (it holds 1 half-processed document from the stopped
   chunk-350 run). Then clear the store files (not the directory, not the compose files).
2. **Edit stack2's `.env` only** (`~/rag-project/stack2/.env`, backup the file first) to use BGE-M3 for
   embeddings: model `bge-m3-mlx-fp16`, dimension 1024, same oMLX endpoint and key mechanism production uses
   for embeddings (read how production's embedding binding is configured; mirror it; no production edit),
   and nothing else, except unsetting CHUNK_SIZE / CHUNK_OVERLAP_SIZE if present (the worker sends its own
   chunk size per request). Keep the LLM, rerank (jina via oMLX Cohere shape) and query parameters identical
   to production's in-force values (mix, top_k 40, chunk_top_k 20, max_total_tokens 32000, entity/relation 10000,
   rerank on; gleaning as production). Then recreate **lightrag-server2 only** to apply it. Confirm in its
   startup log the embedding model, dimension, and that LightRAG's embedding token limit is 8192.
3. **Run the ingest worker as a scratch process** (not a container, not the production worker): unpack
   `docs/chunkrun7.tgz` (already on the host, sha256 2a1f9282815f03d548327347fdd97ed718f50a8b1e7f5e483debb34899f69d01)
   into `scratch/stack2-structure/`, use the chunkrun5 scratch venv (pypdf 5.3.0, python-docx 1.1.2) and
   `pdftotext`. Environment (process env only): LIGHTRAG_BASE_URL=http://127.0.0.1:9622, LIGHTRAG_INTERNAL_API_KEY from
   stack2's `.env`, SOURCE_DOCS_DIR / UPLOADS_DIR / STATE_DB_PATH all under `scratch/stack2-structure/` (NEVER the
   production paths), STRUCTURE_CHUNKING_ENABLED=1, STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=<bgem3-tok>,
   STRUCTURE_CHUNKING_EMBED_URL=http://192.168.1.190:1234, STRUCTURE_CHUNKING_EMBED_MODEL=bge-m3-mlx-fp16,
   key from the host env (never printed); read `app/config.py` for any other required names. Put COPIES of the
   10 original files in its SOURCE_DOCS_DIR (read-only copies from production `source_docs/`; do not move or modify
   the originals). Order smallest first. Do not start it if `worker` env points anywhere near 9621 or 8020.
   The worker must run with a hard timeout and be stoppable by the run token pattern used before; no detached
   run longer than the checkpoints below without a report.
4. **Retrieval check against stack2 only** (queries, no writes) once all 10 documents are processed: run the loop in
   `--probes-only` read-only mode pointed at stack2's LightRAG/gateway (`:9622` / `:8021`) with `--arm-name
   structure-bgem3` for the dev and held-out probe files, and compute `evidence_in_context%` (judge-free) for the 100
   evidence probes (68 plain + 32 structure-v2; if the loop only takes its own two probe files, run the 32
   structure probes through the same recall helper and say how). Report per probe type and per source kind.
5. **NOT APPROVED - do not do: the same read-only probe run against
   production** (`:8020` gateway queries, no writes) for the 100 probes, as a before-and-after on the identical
   questions. It generates ~100 queries of load on the live stack; skip it.

## Hard rules
- Production untouched; at start and end confirm :9621 processed=10/failed=0, container ids unchanged, `.env`
  mtime unchanged. Any production write is a stop-and-report.
- Do not use `--allow-production` anywhere. Do not run the loop in any mode that clears a store other than stack2's.
- No corpus, question or evidence text in the report: counts, ids, sizes, timings only.
- The BGE-M3 server (oMLX) is shared: batch sensibly, respect its limits, and stop if it shows memory pressure.

## Checkpoints and stop rules
- **Before the first document:** dry-check that the worker's request for one small file would carry the
  `chunking` object and `chunk_token_size` >= largest piece (count only), and that stack2 is empty and on BGE-M3.
- **After the first 3 documents (smallest):** report seconds per document and per piece, the extraction
  throughput, and a projected total. If the projection exceeds **6 hours**, STOP after reporting and wait for
  the lead; do not let it run on. Under 6 hours it may continue unattended, with a status line the lead can read.
- **Stop immediately and report** on: any document in `failed` state (record the error class, e.g. a
  ChunkTokenLimitExceededError means the worker's chunk_token_size logic is wrong), any 5xx burst from the model
  server, any oMLX out-of-memory signal, or production changing.

## Report (counts only)
1. Setup proof: stack2 empty + embedding config + backup manifest path; production-unchanged checks.
2. Per document (masked): file type, worker route taken (structure/text or fallback to upload and why), pieces submitted,
   chunks LightRAG stored (must equal pieces), status, seconds, entities and relations extracted, any warning.
3. Totals: chunks, entities, relations, wall time; compare with production's store (same counts, read-only):
   documents, chunks, entities, relations.
4. Stored-chunk check, read-only from the store files: chunk token-size distribution (p10/p50/p90/max in BGE-M3 tokens,
   from the tokenizer), chunks over 8192 (must be 0), chunks that begin with the context header line (share),
   and the vector dimension.
5. Retrieval: evidence_in_context% for the 100 probes (and, if approved, production's number on the same probes),
   plus the ids that missed. Say which part of the loop/gateway path was queried.
6. Anything surprising, with the exact error text (no document text).

## What happens after
The lead brings the operator a separate cutover brief (stack2 store swapped into production with a verified
backup, rollback commands, the worker/image changes, and the BGE-M3 switch of production's embedding). Nothing
here authorises any of it.
