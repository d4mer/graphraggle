# Hermes brief: confirming ingest of chunk size 350 on stack2 (and comparison with production)

**Status: APPROVED by the operator in writing on 2026-10-17 (all steps, including the single held-out scoring). Cutover is NOT approved.** Stack2 only for any write.
Production is only READ via `--probes-only` (queries, no writes). Never `--allow-production`.

## Why
Offline benchmark (chunk-embed-bench.md): fixed 350/50 + mxbai + the existing jina rerank beat the
production 1200/100 at recall@10 by +11.8 pp (350 alone) to +17.6 pp (with rerank); BGE-M3 did
not help. That measured vector retrieval only. This run measures the whole pipeline (graph
extraction at 350-token chunks, mix mode, answers) once, on the real corpus, so we can cut
production over without a second ingest.

## Steps
1. Tools: `~/rag-project/docs/chunkrun4.tgz` (sha256 `baa70e74f6e2be067844850f647542242ffa72a722fe2e7bbc515aa13ed0d231`, commit 4642925, manifest `chunkrun4-MANIFEST.txt`), verify, extract
   to `scratch/chunkrun4`. Confirm in the LightRAG image's env.example that the chunk env names
   are `CHUNK_SIZE` and `CHUNK_OVERLAP_SIZE` (report; stop if not).
2. Pre-flight: production counts (processed/all = 10:10, gateway ingested 10), disk >= 20 GB,
   stack2 healthy; stack2 `.env` equals production's for every loop-whitelisted name except the
   two chunk settings (report differences; production `.env` is not edited).
3. Estimate the ingest time first: expected ~400 chunks (benchmark: 396-407) at the pilot's
   50-107 s per chunk (MAX_ASYNC=1): write the estimate to the report; if > 14 h, stop.
4. Corpus: `scratch/phase1/corpus.json` (8 txt + 2 extracted text copies). Experiments file
   `docs/ops/loop/experiments-chunk350.json` (one arm C350). Probes: frozen dev
   `solvay-dev-v2.jsonl` and frozen held-out `solvay-heldout-v2.jsonl` (sha256 in
   phase1-inventory.md). The held-out set is scored ONCE here, only for C350 and for the
   production comparison, by this approval.
5. Run the loop on stack2 (full explicit flag set, `--draws 2`, retrieval recall ON, dev +
   held-out, `--max-hours 16`), detached, with the pilot's safeguards: 3-read production count
   check (fail only on 2 consecutive real reads != 10:10|10), run.token for monitors,
   status line only RUNNING after real ingest began, stack2 health loss >= 10 min = stop,
   disk < 5 GB = stop. Archive any aborted attempt's state.json before relaunching.
6. After C350 finishes, run the SAME probes against production with
   `--probes-only --arm-name production-1200` (read-only; same judge, same draws; if a
   production gateway query is slow, run serially). No `.env`, restart or store action.
7. Compare with the script's paired bootstrap (C350 vs production-1200) on dev and on
   held-out separately. Report per arm: chunk count in LightRAG, answerable correct %, attribution,
   refusal columns, evidence-in-context %, probe errors, latency p50/p95.
8. Leave stack2 UP and populated. Do not tear down. Do not touch production data.

## Pass/fail (decided before running)
PASS = held-out evidence-in-context rises by >= 8 pp over production AND answerable-correct %
is not lower beyond the paired CI AND refusal accuracy and attribution are not lower.
FAIL = anything else: production stays as is; report where C350 lost (by probe type).

## Hard rules
No content leaves the host. No production change, no worker stop, no `.env` edit on
production. Cutover (copying stack2's store into production) is a separate brief and a
separate written go. Results go to `~/rag-project/docs/ops/runs/chunk350-confirm.md`
(STATUS line first), files not `rig send`. Batch all approvals up front.
