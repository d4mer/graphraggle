# Hermes brief: chunker experiments (branch `chunking`)

**Status: APPROVED by the operator on 2026-10-15 for steps 0 and 1 only (read-only).
Step 2 (the experiment runs) is NOT approved by this brief: it re-ingests and WIPES the
LightRAG store, so it needs its own written go, a protected window and a verified
backup (see "Why step 2 is gated").** Production is not changed by steps 0 and 1.
Operator is at the terminal for approval prompts. Results go to
`~/rag-project/docs/ops/runs/chunker-experiments.md` (files, not `rig send`).

## Context
rc-2 is released (gateway and worker on `local/rag-gateway:rc2`). Chunking work lives on
branch `chunking`; the worker has built a flag-gated transcript pre-processor
(`TRANSCRIPT_PREP_ENABLED`, default off) there. Two questions: (A) does another LightRAG
chunker beat fixed-token on the v2 probes, (B) does the pre-processor help. This brief
gets the facts needed to design the experiments without changing anything.

## Hard rules
- Read-only. No `.env` edit, no container restart, no store change, no ingest.
- Do not read or copy corpus content out. Counts, shapes, regex-like patterns and
  distributions only. File names are masked (letters replaced) in anything you report.
- The worker is running; do not stop it. Do not run the loop driver.

## Step 0: content-free shape report of the transcripts
For the transcripts in the loop corpus and the 6 source transcripts (and, separately,
for the 10 documents currently in `source_docs/`, by extension): speaker-label pattern(s)
as shapes (e.g. `<Name>  <m:ss>`, `<Name>: `), timestamp pattern, line and turn counts,
turn-length percentiles (p10/p50/p90/max) in approximate tokens, share of turns under
10 tokens, whether any file is VTT/SRT/Q-and-A style, whether labels carry roles or
emails, and the file-name shape with letters masked (do names carry a date or a title?).
This is the input for the worker's fixtures.

## Step 1: discover the chunker settings (do not guess)
LightRAG 1.5.7 ships fixed-token, recursive-character, paragraph-semantic and
semantic-vector chunkers. From the LightRAG image's own `env.example`, docs or source
(read inside the container, read-only) report: the exact environment variable names and
allowed values that select the chunker and tune it (size, overlap, similarity threshold),
which ones call the embedding model per sentence, and the current effective values in
production (names and values of non-secret chunk settings only). Say which of those
names are NOT in the loop driver's `ENV_WHITELIST` (`scripts/overnight_loop.py`); the
lead will have the worker add them.

## Why step 2 is gated (read this before asking for it)
`scripts/overnight_loop.py` clears the LightRAG store before each re-ingest, so running
it against production **replaces the whole corpus with the 3 to 6 loop transcripts**.
Step 2 would therefore need all of: a quiescent verified copy of `lightrag_store/`
(as in the 2026-10-09 backup drill, with sha256 manifest and a proven restore), the
worker stopped, a window the operator names, and a restore at the end. Alternatively a
second LightRAG instance on another port and store; Hermes should say in the report
whether that is feasible on this host (memory, disk, oMLX capacity) without touching
production. Do not do either until the lead sends a separate go.

## Step 2 (NOT approved): the arms, for planning only
All arms use the same 6 transcripts, the v2 probes (40 dev / 40 held-out), gleaning off,
rerank on:
- C0 baseline fixed-token, current. Reuse an existing v2 baseline run if one exists and
  say which.
- C1 recursive chunker. C2 semantic-vector chunker. C3 paragraph-semantic only if C1/C2
  show a signal.
- P1 transcript pre-processor on (`"transcript_prep": true`), fixed-token chunker.
Adopt an arm only if held-out accuracy beats C0 by at least 3 questions (1 question =
2.5 points) with hallucination not higher and refusals not lower.

## Report
Step 0 and 1 results, whether a second LightRAG instance is feasible, and an estimate of
the minutes per arm (ingest plus probes) so the operator can choose a window.
