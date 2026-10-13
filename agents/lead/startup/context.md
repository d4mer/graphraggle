# Handoff - lead

Last updated: 2026-10-13 07:30 (after the overnight accuracy loop)

## Goal
Run GraphRAG rc-1 in production for the new Solvay corpus (GSK data wiped 2026-10-12, backup on deltahome ~/backups/graphraggle-20261012). Lead delegates code to
dev-worker@graphraggle, prod ops to Hermes (ops-hermes@thinkpad-ops, via `rig send --host thinkpad`). RULE: only Hermes may read corpus/LightRAG content (see memory corpus-data-access-hermes-only).

## State
- Thinkpad prod: lightrag-server (qwen3.8-flash on sigma http://192.168.1.195:1234/v1, Bearer 1234; embeddings oMLX) healthy, baseline .env (identical to loop3/env.original). Store holds the 3 loop-corpus
  transcripts (built gleaning-off, chunk 1200). rag-ingest-worker is STOPPED (stopped for the loop; restart with `docker start rag-ingest-worker` when ingestion should resume). Gateway on baseline retrieval settings.
- Staged Solvay files: ~/rag-project/staging/Solvay (8 transcripts, 1 pdf, 1 docx, 1 pptx). 8 transcripts were ingested via the worker earlier; the loop later wiped/rebuilt with 3.
- Overnight loop driver: scripts/overnight_loop.py (packets 28-33 merged, master dc52419 + docs). Runs in tmux on the Thinkpad; outputs ~/rag-project/probe/loop, loop2 (E4 aborted), loop3. Stop a run with `kill -TERM <python pid>` only
  (never kill the tmux wrapper; if it dies, restore: cp <out>/env.original ~/rag-project/.env && docker compose up -d --no-deps lightrag-server [gateway-api]).
- Probe sets: ~/rag-project/probe/solvay-dev-v1.jsonl (40 q), solvay-heldout-v1.jsonl (15 q). Judge = qwen3.6-35b on oMLX 192.168.1.190 (reasoning model, JUDGE_MAX_TOKENS 4000).

## Results (dev 40 / held-out 15; all hallucination 0 except E2)
E0 baseline 80/80 (re-judged) | E1 gleaning off 82.5/80 | E2 chunk2000 82.5/80, halluc 6.7% | G1 top_k 80 82.5/80 | G2 rerank off 75/80 (worse) | G3 bigger budget 82.5/80 | G4 less context 80/80.
E3 (ENTITY_TYPES) impossible: unsupported in LightRAG v1.5.7; E4 chunk600 aborted (too slow). Median query ~40-48 s.
Hermes failure analysis (9 failures/165 verdicts): 6 of 9 are probe_bad (4) or judge_wrong (2); 3 are one multi-fact synthesis defect (fluent wrong synonym for a garbled ASR term). No evidence of extraction/retrieval loss.
=> accuracy ceiling is test/judge quality, not config. Held-out has only 15 questions (1 question = 6.7 points).

## Decisions pending (operator)
1. Adopt MAX_GLEANING=0 in prod (no accuracy cost, halves extraction calls) - recommended; keep rerank ON and other defaults.
2. Probe set v2 (Hermes: fix bad probes, disambiguate, bigger held-out ~40) + judge prompt fix (clean refusals, attribution) via dev-worker packet; then re-judge saved answers (cheap, `--rejudge`).
3. Resume ingestion of the full Solvay corpus (restart worker; pptx needs packet-27 deploy with python-pptx image rebuild first).
4. Multi-fact synthesis defect: consider transcript cleaning at ingest / prompt change.

## Other open
Rotate OpenWebUI key; real .docx e2e check; GRAG-42 oMLX memory policy; Plane GRAG-43..50 exist (update from macbookm1.local REST); understory note /projects/graphraggle-status-2026-10-12.md (call by curl, one request at a time).
Hermes cannot rig-send to this session (address gone) -> have it write summary files and read them.
