# Hermes brief: third arm - production's legacy chunking re-ingested with BGE-M3 (isolate the embedder), then the same 100 probes

**Status: DRAFT. NOT APPROVED by the operator. Do not start.** Scratch stack only; production is **not touched at all**
in this brief (not even queries). Approval prompts appear in your pane; the operator will be at the terminal.
Report to `~/rag-project/docs/ops/runs/legacy-bgem3-arm.md` (files, not `rig send`); STATUS on line 1.

## Why this run exists
The production-vs-stack2 comparison (`recall-production-vs-stack2.md`) scored production 85/100 and stack2 91/100 on
the same 100 evidence probes, but the two arms differ in **two** things at once:

| arm | chunking | embedder | evidence_in_context |
|---|---|---|---|
| A. production (as it runs) | legacy fixed ~1200/100, 110 chunks | mxbai-embed-large-v1 (512-token window) | 85/100 |
| C. stack2 | structure path, 129 chunks | BGE-M3 (8192 window) | 91/100 |
| **B. this run** | **legacy fixed 1200/100** | **BGE-M3** | ? |

Production never received the 10-18 BGE-M3 switch (its LightRAG container is from 2026-10-13). Without arm B nobody can
say whether the +6 is the chunker or the embedder. B vs A isolates the embedder; C vs B isolates the chunking. The
cutover decision depends on which one it is, because a cutover moves both.

## What to build: arm B
**One variable differs from stack2's attempt 3: the worker cuts nothing.** Everything else stays identical to attempt 3.
1. **Stack.** Prefer a new scratch LightRAG ("stack3": its own compose project, store dir, a new host port that is
   neither 9621, 9622 nor 8020, same image v1.5.7) cloned from stack2's compose/`.env`, so stack2's structure store stays
   untouched for the cutover rehearsal. If a third stack is not cheap, reuse stack2 only after backing up its
   `lightrag_store/` and the scratch state DB to `stack2/backups/pre-legacy-arm-<UTC>/` with a sha256 manifest, and restore
   and verify it byte-for-byte at the end (the cutover brief's phase 2 needs it). Say which you chose and why.
2. **Embedding:** BGE-M3 (`bge-m3-mlx-fp16`, dim 1024, max_token_size 8192), same oMLX endpoint and key mechanism as
   stack2. **LLM, rerank, MAX_GLEANING and all query settings identical to stack2 and production** (they already match
   each other; re-check with the same masked env diff as in the comparison and stop for the lead on any query-side
   difference).
3. **Legacy chunking settings = production's.** Read production's LightRAG env (read-only, from the container env /
   `.env`; no production edit) and set the new stack's `CHUNK_SIZE` / `CHUNK_OVERLAP_SIZE` to the **same values**
   production uses (expected 1200 / 100; record what you found). Attempt 3 left them unset on stack2, so do not copy
   stack2's here.
3b. **Worker = the legacy path.** Run a scratch worker (not a container, not the production worker) from the same
   `chunkrun9` tree (`scratch/stack2-structure8/`, sha256 in `stack2-structure-ingest-3.md`) with
   `STRUCTURE_CHUNKING_ENABLED` and `TRANSCRIPT_PREP_ENABLED` **unset/false** (the brief's point: with them off the
   payload is what production's worker sends today, text via `/documents/text`, PDF/DOCX via `/documents/upload`). Process
   env only; all dirs (`SOURCE_DOCS_DIR`, `UPLOADS_DIR`, `STATE_DB_PATH`) under a new `scratch/legacy-bgem3/`, pointing at
   the new stack's port, NEVER production's paths or ports. Put COPIES of the same 10 original files in
   `SOURCE_DOCS_DIR`, smallest first. Print the worker's startup config dump (names and values) and confirm
   structure chunking is off before the first document.

## Hard rules
- Production untouched and **unqueried**: at start and end confirm :9621 processed=10/failed=0, container id,
  `StartedAt`, `RestartCount` and `.env` mtime unchanged, and do not send any request to :9621 or :8020. Any production
  write is a stop-and-report. (Note from the comparison run: LightRAG's query path writes its own LLM-response cache;
  that is a reason not to query production here, not a thing to repeat.)
- Do not use `--allow-production`. Do not touch the cutover brief.
- No corpus, question or evidence text anywhere: counts, ids, sizes, timings, hashes.
- oMLX is shared: batch sensibly, stop on memory pressure or a 5xx burst.
- One bare command per approval. Chained or looped commands show as "+ N commands" dialogs that time out unattended and
  stop the run; use single commands or an already-approved script file, and background long jobs with a log (create
  the log directory first; the last run lost its console log to a directory-creation race).

## Checkpoints and stop rules (ingest)
- **Before the first document:** the new stack is empty, on BGE-M3, and the worker's config dump shows chunking off.
- **After the first 3 documents:** report seconds per document and a projected total. Stop and report if it projects
  over **6 hours**.
- **Stop immediately** on: any `failed` document (record the error class), a 5xx burst, an oMLX out-of-memory signal, or
  production changing.
- **Sanity check at the end of ingest (counts only):** chunks stored vs production's 110 (expected similar; a large
  difference means the worker did not take the legacy path or `CHUNK_SIZE` is wrong: stop and report), chunk token-size
  distribution (p10/p50/p90/max in BGE-M3 tokens; expected max near the legacy 1200 plus overlap, **not** the structure
  run's 26,934-character max), entities, relations, vector dimension 1024, documents 10 processed / 0 failed.

## Retrieval
Same 100 probes, same helper logic and same like-for-like rules as `recall-production-vs-stack2.md` (copy
`probe/run_prod_recall.py` to a new name and point it at the new stack's port with a hard guard that refuses 9621/8020; do
not edit the original): judge-free `evidence_in_context`, mode=mix, `only_need_context=true`, no top_k or budgets sent,
one query at a time with a 5 s pause, stop on a query over 120 s or a 5xx. Same probe files (record their sha256s; they
must equal the ones in the comparison report). Use a **fresh** LightRAG LLM-response cache on the new stack (it is empty
by default), and note that production's cache is warm; that is a known small difference.

## Output (counts and ids only)
1. Setup: stack choice and ports, env diff vs stack2 (names only; expected: nothing but plumbing and ports), production's
   CHUNK_SIZE / CHUNK_OVERLAP_SIZE, the worker config dump, production-unchanged checks at start and end.
2. Ingest: per document (masked): route taken (text or upload), chunks stored, status, seconds, entities, relations;
   totals and the end-of-ingest sanity check above; stack2 restore verification if stack2 was reused.
3. Retrieval, arm B: `evidence_in_context` overall, per set (solvay-dev, solvay-heldout, structure-dev,
   structure-heldout) and per type; query seconds min/p50/max; errors; returned-context chars p50.
4. **Three-way paired table** over the 100 ids: A (production, from
   `recall-production-vs-stack2.jsonl`), B (this run), C (stack2, same file). Counts for each of the eight
   true/false combinations, and the id lists for "B only" vs A and for A-only vs B. Write per-probe booleans to
   `~/rag-project/docs/ops/runs/legacy-bgem3-arm.jsonl` (`{"id":..., "A":bool, "B":bool, "C":bool}` per line).
5. Anything surprising, with exact error text (no document text).

## How the lead will read it
- B - A = the embedder effect (same legacy chunks, mxbai vs BGE-M3).
- C - B = the chunking effect (same BGE-M3, legacy vs structure).
- The lead computes the paired differences and bootstrap intervals from the jsonl and brings the operator the numbers
  next to the cutover decision. LLM entity extraction is not deterministic, so one re-ingest per arm is one draw, not the
  truth; expect noise of a few probes, and read small differences as noise.

## Time and load
Rough budget: ingest about 3 to 4 hours (stack2's structure run took 3.2 h for similar volume), then about 2.5 hours of
queries (the production run took 2h22m for 100 probes), so most of a working day on the shared oMLX server. The operator
chooses the window. Nothing here authorises a cutover or any change to production.
