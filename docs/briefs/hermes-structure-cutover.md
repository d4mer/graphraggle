# Hermes brief: cut production over to the structure-ingested store (stack2 -> production)

**Status: DRAFT. NOT APPROVED by the operator. Do not start anything. Phases 0 to 2 are read-only or scratch-only and
need their own approval; phase 3 changes production and needs a separate, explicit "go" from the operator for
phase 3 itself. The lead fills in the commit/tag and the open choices below before asking for approval.**
Approval prompts appear in your pane; the operator will be at the terminal for phase 3.
Report to `~/rag-project/docs/ops/runs/structure-cutover.md` (files, not `rig send`); STATUS on line 1.

## What this does and why
Stack2 holds the 10 real documents ingested through the structure path (attempt 3, chunkrun9 = commit `0047a69`):
129 chunks, 1,437 entities, 1,793 relations, BGE-M3 vectors (dim 1024), every chunk byte-exact. Production holds the
same 10 documents as 110 legacy fixed-1200/100 chunks (1,566 entities, 1,964 relations). The cutover makes production
serve the structure-ingested store and makes the production worker cut new documents the same way. It is justified
only by the retrieval comparison (`hermes-production-vs-stack2-recall.md`); the operator decides after reading it.

## Decisions (PROPOSED by the lead; the operator confirms or changes each before approval)
Evidence: `recall-production-vs-stack2.md` and `legacy-bgem3-arm.md` (A production 85, B legacy+BGE-M3 83, C stack2 91).
Embedder effect B-A = -2 (noise); chunking effect C-B = +8 (95% bootstrap 0 to 16, sign test p=0.077); all of the gain is on
the structure probe sets, Solvay probes show no gain. The case for cutover is the chunker, on structure-heavy documents.
1. **Code to deploy: tag `structure-cutover-1` on commit `f7051e6`** (the last runtime-code commit; every later master
   commit is docs, briefs, rig scaffolding or a test, nothing under `app/`). The lead creates the tag after approval; build from the tag, never a branch tip.
   Hermes gets it by `git bundle` from the lead, sha256 verified; nothing is pushed to origin without the operator.
   Phase 1 proves the tag cuts the 10 documents into the same 129 pieces stack2 stored (chunkrun9 = `0047a69` differs from
   the tag by the heading `.search`, key/value-list detection, and the PDF/DOCX extract defaults).
2. **Data path: Option A** (swap stack2's store into production, state DB reconciled). Option B (re-ingest, ~3.2 h of degraded
   retrieval, one more LLM-extraction draw) only if phase 2 shows the state DB cannot be reconciled safely.
3. **Embedder: production is believed to run mxbai-embed-large-v1, not BGE-M3** (container from 2026-10-13, never switched;
   the repo cannot confirm it, only phase 0 can). The swap
   carries BGE-M3 vectors (dim 1024), so the same window must apply stack2's embedding settings to production's
   `.env`, otherwise queries embed with the wrong model against BGE-M3 vectors. Arm B shows the switch alone is neutral, so
   it is a requirement of the data swap, not a benefit. Phase 0 re-confirms production's embedder before anything else.

## Hard rules
- Phases 0 to 2 change nothing in production: no `.env` edit, no restart, no write to `lightrag_store/`,
  `state/ingest.db`, `source_docs/` or `uploads/`. Rehearsals run on **copies** and on stack2.
- Do not add or change any `.env` value except those listed in phase 3. `COMPANY_AWARE_DEDUPE` stays off. Do not use
  `--allow-production`.
- Worker may be mid-extraction: if any document is `processing`/`enqueued` or LightRAG shows `analyzing`/`processing`,
  wait and report; do not interrupt it.
- No corpus, question or answer text in the report: counts, ids, sizes, timings, sha256s.
- One bare command per approval. Chained or looped commands show up as "+ N commands" dialogs that time out unattended
  and stop the run; use single commands or an already-approved script file, and background long jobs with a log.
- Stop at the first failed check, roll back with the commands in phase 3, say so, and stop. Do not retry a denied
  approval; report it.

## Phase 0: pre-flight, read-only
1. Record what runs now: `docker ps` lines for gateway-api, ingest-worker, lightrag-server, open-webui; image ids/tags;
   RestartCount; `/health` of gateway and LightRAG.
2. Production LightRAG env that matters (names and values, no keys): embedding model/dim/max_token_size/binding,
   rerank, query params, CHUNK_SIZE / CHUNK_OVERLAP_SIZE if set. Same for stack2. List every difference.
3. State DB: documents by status, rows sharing a sha256 with another row, rows with `retry_count > 0`, and the
   **schema** (table and column names) of every table the worker reads or writes. Counts only, no content.
4. LightRAG: documents by status, and counts of chunks / entities / relations in production's store (read-only).
5. Diff `~/rag-project/compose.yml` against the repo's `compose.yml` at the tag: differing lines only, do not copy the
   repo file over. Record which env names the production `ingest-worker` service passes, and whether the worker's
   compose service can read a BGE-M3 `tokenizer.json` directory (path and mount) for
   `STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH`; with it empty the fit check over-counts conservatively.
6. Confirm the image build inputs resolve: `pip install -r requirements.txt` in a throwaway build (the repo pins
   `python-docx==1.1.2` and `pypdf==5.3.0` without network when written, never confirmed). Record the result.

## Phase 1: prove the tag on stack2 data (scratch, no LightRAG writes)
1. Build the worker from the tag in a **scratch** image/tree (not `local/rag-gateway:latest`). Verify the source is
   the tag's commit before building.
2. Replay the 10 original documents (copies) through the tag's worker chunking functions with the same environment
   attempt 3 used, and compare each piece's sha256 + length with the 129 stored chunks in stack2's store (read-only).
   Report per document: pieces vs stored, matches. **Pass = 129/129 match.** Any difference is a stop-and-report: the
   lead sends it to the worker (the tag then differs from what was validated, and the retrieval number may not apply).
3. Report the tag's startup config dump for the worker: STRUCTURE_CHUNKING_ENABLED, WORKER_PDF_EXTRACT_ENABLED,
   WORKER_DOCX_EXTRACT_ENABLED, TRANSCRIPT_PREP_ENABLED (names and values only).

## Phase 2: rehearse the swap on stack2 (scratch, option A only)
1. Back up stack2's store and the scratch worker's state DB (`scratch/stack2-structure8/state/ingest.db`) with sha256s.
2. Make a **copy** of production's `state/ingest.db` and a copy of production's `source_docs/` layout under a scratch
   dir. Propose, as a script file plus a dry-run report, the reconciliation that makes the copied state DB agree with the
   stack2 store: the rows for the 10 documents point at the stack2 doc ids / track ids, status processed, paths at the
   production directories, sha256s unchanged. Counts only in the report.
3. Prove it: start a **scratch** worker (tag build, scratch ports, scratch dirs only, never 9621/8020) against stack2's
   LightRAG with the reconciled copy and let it run 5 scan cycles. Pass = it submits **nothing**, counts of documents
   by status equal before, no `Traceback`/`ERROR`. If it resubmits anything, stop and report why.
4. Write the exact phase-3 commands, with the real paths, into `~/rag-project/docs/ops/runs/structure-cutover-plan.md`
   for the lead to read before asking the operator for phase 3.

## Phase 3: cutover (needs the operator's separate "go for phase 3")
Estimated production retrieval downtime: the time LightRAG is stopped (target under 5 minutes). The operator is at
the pane for all of it.
1. **Snapshots (on the Thinkpad, with sha256 manifests):** `lightrag_store/` copy to
   `backups/pre-cutover-<UTC>/`, SQLite online-backup of `state/ingest.db`, copy of `.env` and `compose.yml`.
2. **Rollback preparation:** tag the images that run now (`...:pre-cutover` for gateway and worker); write
   `compose.rollback-cutover.yml`; prove it parses with `docker compose -f compose.yml -f compose.rollback-cutover.yml
   config` (do not print the output).
3. **Build** the tag image(s) (same recipe as rc-2); record the image id; import check inside the image.
4. **Stop the production worker first** (so nothing submits mid-swap), then stop LightRAG (`lightrag-server`).
   Confirm both stopped and the gateway still answers `/health` (queries will fail until step 7).
5. **Swap the store:** move production's current `lightrag_store/` aside (not delete), copy stack2's store in, verify
   file sha256s against stack2's. Apply the reconciled state DB (the phase 2 script, on the real DB, after the step 1
   backup).
6. **Apply env (and only these):** `STRUCTURE_CHUNKING_ENABLED=true`, `WORKER_PDF_EXTRACT_ENABLED=true`,
   `WORKER_DOCX_EXTRACT_ENABLED=true`, the BGE-M3 tokenizer path if phase 0 found a mount, and the embedding settings
   only if phase 0 showed production is not already on BGE-M3. Nothing else; no feature flags.
7. **Start LightRAG, then the worker and the gateway image:** `docker compose up -d --no-deps lightrag-server`,
   wait for health, then `ingest-worker` and `gateway-api` from the tag image.
8. **Acceptance (counts and judgment only):**
   a. LightRAG startup log: embedding model BGE-M3, dim 1024, max token size 8192; `/health` 200.
   b. documents 10 processed / 0 failed; chunks 129; entities 1,437; relations 1,793 (stack2's numbers).
   c. vector dimension 1024 on a sample of chunk entries.
   d. gateway `/health` 200; `/api/tags` lists the model; two normal bridge queries about content already in the corpus
      return non-empty answers with a Sources block; a synthetic `### Task:` request (made up, no corpus text) logs
      `task_prompt=true`.
   e. worker log: structure chunking on, 5 scan cycles with no resubmission, no `Traceback`/`ERROR`, document counts by
      status equal before and after.
   f. Re-run the 100-probe `evidence_in_context` helper on production `:9621` (read-only): expected within a few probes
      of stack2's 91/100, since the store is the same. A large drop means the swap or the embedder is wrong: roll back.
   g. Open WebUI untouched: same container id.
9. Report acceptance as a table, the exact image ids, and the rollback commands' current validity.

## Rollback (any failed check in phase 3)
1. Stop `ingest-worker` and `lightrag-server`.
2. Restore: move the cutover store aside, put `backups/pre-cutover-<UTC>/lightrag_store` back, restore the state DB
   backup and `.env`/`compose.yml` copies, verify sha256s against the manifests.
3. `docker tag local/rag-gateway:pre-cutover local/rag-gateway:latest`; `docker compose up -d --no-deps lightrag-server`
   then the worker (override file `compose.rollback-cutover.yml` for the worker image) and gateway.
4. Confirm 10 processed / 0 failed, 110 chunks, a normal query works, gateway `/health` 200, then report.

## Report (counts only)
Per phase: what ran, pass/fail per check, image ids, the sha256 manifests' paths, production-unchanged checks for
phases 0 to 2, and timings (downtime in phase 3). The lead reads it over ssh.

## Not covered by this brief
Re-ingesting new documents, any change to rerank/LLM/query parameters, and the retrieval accuracy of final answers
(this brief only moves the store and the worker; it does not tune anything).
