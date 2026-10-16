# Hermes brief: Phase 1 of the retrieval accuracy plan (fair corpus + frozen baseline C0)

**Status: Part 1 (steps A, B, C, D0) APPROVED by the operator in writing on 2026-10-16.
Part 2 (the ingest and baseline run on stack2) is NOT approved: it needs its own written go.**
Part 1 is read-only / scratch-only. Operator decisions already taken: use all 6 transcripts +
the other production documents on stack2; smaller chunks are the later Phase 3 route.
Plan: `docs/09-retrieval-accuracy-plan.md`. Stack2 only (127.0.0.1:9622 / :8021, project
`rag-chunk2`). Never production, never `--allow-production`, never :9621/:8020 except
read-only count checks.

## Why
The pilot could not show a difference: 17 chunks in the store, `top_k=40`/`chunk_top_k=20`
retrieved everything, the judge ignored attribution, and 1 draw on 40 questions. We also
learned the embedder silently truncates at 512 tokens (your embedder-check report). Phase 1
builds a corpus where retrieval must choose, freezes the probe split, and records the baseline
the later arms are compared against. No chunking change is tested here.

## Hard rules
- Content never leaves the host: counts, shapes, masked names, numbers, verdict tables only.
- No production change. Do not stop the worker. No `.env` edit on production.
- Stack2 may be cleared by the loop (throwaway); production may not.
- Write everything to files; no `rig send`. Batch approvals up front, run detached.
- Lessons from the pilot apply: status line in the report, prod-count check = 3 reads 60 s
  apart, fail only on 2 consecutive real reads != expected; empty/timeout = unreachable, not a
  stop; run.token so orphaned monitors die; archive any aborted attempt's `state.json` before
  relaunching (do not rely on `--resume`); set RUNNING only after real ingest began.

## Part 1 (read-only / scratch only)

### A. Updated tools
Tarball `~/rag-project/docs/chunkrun2.tgz` (commit 267656f, sha256
`c9a0ff32890af0c306b9a3c9e4d8de69c9e1142872ab8ac8cae16e56ca976707`, manifest
`chunkrun2-MANIFEST.txt`): verify, extract to `~/rag-project/scratch/chunkrun2` (do not
overwrite `scratch/chunkrun`). It adds: attribution and refusal columns, retrieval-recall
probe (`evidence in context%`), `--draws`, `--compare`, optional second judge,
`experiments-retrieval-sweep.json`.

### B. Inventory (counts and shapes only)
1. The 10 documents now in production by extension and size class, and which are
   text-shaped (txt) vs binary (pdf/docx/pptx). The 6 loop transcripts (masked names,
   token counts, line counts) and which of them are also among the production 10.
2. For each binary document: can the rc2 worker image's extractor produce text from it
   offline? Extract each to `scratch/phase1/corpus/` as `.txt` (made by the same
   function the worker uses, in a throwaway container without network) and report token
   counts. This text copy is only for the chunking comparison: production ingests binaries
   through LightRAG upload, not as text; say so in the report.
3. Estimated chunk count at the current chunker (1200/100) for the whole proposed corpus
   = 6 transcripts + the other production documents (text-extracted), deduped by sha256.
   Gate: must be well above 40 (target >= 100). If not, say how many more distractor
   documents (similar-topic transcripts from the staging area) would be needed, count only.
4. Probe sets: `solvay-dev-v2.jsonl` and `solvay-heldout-v2.jsonl` (40 each, the loop4
   pair). For each: how many probes carry an `evidence` quote; of those, how many quotes
   are a verbatim substring of exactly one document in the proposed corpus, of more than one,
   and of none (STT garble); how many probes target a document not in the corpus. Counts by
   probe type (single-fact, multi-fact, combine, follow-up, unanswerable). Do NOT print
   any quote.

### C. Freeze
Copy both probe files to `scratch/phase1/probes/frozen/` and record their sha256. Create a
1-line placeholder held-out file (as in the pilot) for dev-only runs. The held-out set is
not run until the lead sends a separate go for the final comparison. Record the corpus file
list (masked names + sha256) in `scratch/phase1/corpus.manifest`.

### D0. Extra read-only question (source read, no changes)
From the LightRAG 1.5.7 image source: for `chunking.strategy = "custom"`, what does the
request accept and what must the chunker return (list of dicts? keys? `tokens`?
`chunk_order_index`?); can the text that is embedded differ from the text that is stored and
shown to the LLM; is there any parent/neighbour chunk expansion at query time; what happens
when a single chunk exceeds `embedding_token_limit` (the 8192 default). Report names and
signatures only.

## Part 2 (NOT approved; separate go): the baseline run on stack2
Only after the lead sends "Phase 1 part 2 go":
1. Pre-flight: production counts (processed/all = 10:10, gateway ingested = 10) recorded;
   disk >= 20 GB free; stack2 healthy. Record `MAX_GLEANING` and every other loop-whitelisted
   env value in force on production and on stack2 (names and values); stack2 must equal
   production for all of them (report differences; do not edit production).
2. Corpus file = `scratch/phase1/corpus.json` (list of paths). Experiments file with ONE arm,
   `C0-baseline` (no env changes, `reingest: true`, no transcript_prep, no `chunking`).
3. Run the loop with the full explicit stack2 flag set (same as the second-stack brief) plus
   `--draws 3`, dev probes frozen, held-out = placeholder, retrieval recall ON,
   `--max-hours 12`. Output under `docs/ops/runs/phase1/`.
4. Estimate time before starting and write it in the report: ingest of the whole corpus
   (the pilot ingested ~9 chunks in 450-960 s) plus 40 probes x 3 draws at the observed
   135-257 s each. The operator will choose the window; if the estimate exceeds 12 h, say so
   and stop before ingesting.
5. Report `docs/ops/runs/phase1-baseline.md`: STATUS line, corpus chunk count in LightRAG
   (POST /documents/paginated counts), per-draw and mean: answerable correct %, attribution,
   refusal columns, `evidence in context %`, probe errors, latency p50/p95, and the verdict
   transitions between draws (a noise estimate). Leave stack2 UP and populated for later
   arms; teardown is a separate go.

## Out of scope
Any chunker or pre-processor arm, the retrieval sweep (Phase 2), held-out scoring, key
rotation, production changes, LLM-written headers.
