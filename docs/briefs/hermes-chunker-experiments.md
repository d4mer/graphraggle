# Hermes brief: chunker experiments on a copy of the loop corpus (branch `chunking`)

**Status: DRAFT. Not approved.** Experiment A (no code): does a different LightRAG chunker
beat fixed-token on the v2 probe set? Plus a content-free shape report the worker needs.
Production is NOT changed: the loop driver backs up `.env`, applies experiment env, and
restores baseline at the end (as in the overnight runs). Operator must be at the terminal
for approval prompts. Results to `~/rag-project/docs/ops/runs/chunker-experiments.md`.

## Hard rules
- Do not run this while `rc-2` release steps are in progress (shared LightRAG and `.env`).
  Release brief: `docs/briefs/hermes-release-rc2.md`. Release goes first.
- Production query path will be unavailable or degraded while the loop re-ingests the loop
  corpus into LightRAG. The operator must approve a window. Say how long.
- The worker (rag-ingest-worker) must be stopped for the loop (the driver refuses
  otherwise). Report if any document is processing first; wait for it.
- Do not read or copy corpus content out. Counts, shapes, scores and judgment only.

## Step 0: shape report (no LightRAG change)
For the transcripts in the loop corpus (and the 6 source transcripts), write a content-free
report: speaker-label pattern(s) (as regex-like shapes, e.g. `<Name>  <m:ss>`), timestamp
pattern, line/turn counts, turn-length percentiles in tokens, share of turns under 10
tokens, whether any file is VTT/SRT, file-name pattern shape (do the names carry a date or a
title? show the shape with letters masked). This goes to the worker for fixtures.

## Step 1: discover chunker settings (do not guess)
LightRAG 1.5.7 ships fixed-token, recursive-character, paragraph-semantic and semantic-
vector chunkers. From the LightRAG image's own docs/`env.example`/source (read-only,
inside the container), report the exact environment variable names and allowed values that
select the chunker and tune it (size/overlap/threshold), and which ones need an embedding
call per sentence. Add the missing whitelist entries to the loop driver's
`ENV_WHITELIST` is the WORKER's job; just report names and values.

## Step 2: experiments (after approval of the names from step 1)
Create `docs/ops/loop/experiments-chunker.json` is the worker's file; Hermes runs whatever
the lead hands over. Planned arms, all re-ingest the same 6 transcripts, same v2 probes
(40 dev / 40 held-out), gleaning off (current production), rerank on:
- **C0 baseline** fixed-token (current): this is the v2 baseline. If a v2 baseline run on
  rc-2 settings already exists, use it and say which run.
- **C1 recursive** chunker.
- **C2 semantic-vector** chunker.
- **C3 paragraph-semantic** only if C1/C2 show a signal; transcripts have no headings.
Report per arm: dev %, held-out %, hallucination %, refusal %, median query s, ingest
minutes, chunk counts per document (counts), failures by category if any answer changed.

## Decision rule
An arm is adopted only if held-out accuracy is higher than C0 on the v2 set by at least
3 questions, with hallucination not higher and refusals not lower. Otherwise keep
fixed-token and say so. Remember 1 held-out question = 2.5 points on 40.

## Rollback
The driver restores the baseline `.env` at the end. Hermes confirms: LightRAG healthy,
`.env` equals the pre-run backup (sha256), store contents restored (re-ingest of the
production corpus is NOT part of this brief: if the loop wiped the store, say so and stop;
the operator decides how to repopulate).
