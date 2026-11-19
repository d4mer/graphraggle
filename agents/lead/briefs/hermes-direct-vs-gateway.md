# Hermes: LightRAG-direct vs gateway accuracy comparison (production, queries only)

Operator-approved 2026-11-19. This replaces R2w / GL-10 scoping work (stopped; code stays in master, undeployed).

## Why
The project started because Open WebUI (-> gateway -> LightRAG) answered worse than the LightRAG web UI. We have not measured the two paths on
identical questions since the gateway was rebuilt. The question: is there still a gap, and how big? The operator uses Open WebUI day to day and
wants it to be as accurate as the LightRAG UI.

## What to do (production, queries only; no config change, no deploy, no ingestion)
1. Take the corrected Solvay pool v2 (117 rows: gl13-v2 49 + gl13b-v2 68) you already have, with its answer keys. Reuse the gateway answers from the
   2026-11-15/16 production run if they were stored AND production has not changed since; otherwise re-run the pool through the gateway once
   (`:8020`, same path Open WebUI uses) so both arms hit the same store today. Say which you did.
2. Run the same 117 questions through LightRAG direct (`lightrag-server`, prod port 9621, `/query`) the way the LightRAG web UI does: mode `mix`,
   the UI's default top_k / chunk_top_k / rerank settings, no gateway. Record the exact payload (parameters only) so we can reproduce it.
3. Judge both arms with the same judge and rubric as the earlier scoring (judge-only, answers hidden from the question author). Same ok% definition:
   correct + refused_ok.
4. Report counts only, to a summary file (give me the path):
   - ok%, correct, partial, wrong, hallucinated, refused_ok, refused_wrong per arm
   - the paired table: rows where direct is ok and gateway is not (ids), and the reverse (ids)
   - latency median / p95 per arm
   - for each paired-loss row (direct ok, gateway not): did the gateway retrieve the evidence chunk (reference file_path present) and then answer wrongly, or not retrieve it? Counts and ids.
5. No corpus text in the report. Do not stop at the first anomaly; finish both arms, then report.

## Decision this feeds
- Direct clearly better -> real gap; lead diffs the gateway's payload / prompt / post-processing against direct and fixes the cause.
- About equal -> the gap is closed; accuracy work shifts to the retrieval misses themselves.

Hermes runs on local models only (MiniMax removed from its config 2026-11-19).
