# Handoff - lead

Last updated: 2026-10-13 (accuracy target met on the 6-transcript Solvay corpus; widening scope next)

## Goal
Run GraphRAG rc-1 in production for the Solvay corpus (GSK data wiped 2026-10-12, backup on deltahome ~/backups/graphraggle-20261012; backups live ONLY there). Lead delegates code to
dev-worker@graphraggle, prod ops to Hermes (ops-hermes@thinkpad-ops, `rig send --host thinkpad`). RULE: only Hermes reads corpus/LightRAG content (memory corpus-data-access-hermes-only);
one exception: dev-worker may read SOURCE transcripts in /home/maverick/work/solvay-src to author probes (files in /home/maverick/work/solvay-probes, never committed, counts-only reports).

## State
- Thinkpad prod: lightrag-server v1.5.7 on sigma qwen3.8-flash (http://192.168.1.195:1234/v1, Bearer 1234), MAX_GLEANING=0 in .env (backup of old env only on deltahome), embeddings oMLX. Store = 6 Solvay transcripts
  (254 chunks, 433 nodes/560 edges). rag-ingest-worker STOPPED (`docker start rag-ingest-worker` to resume ingestion). Staged files: ~/rag-project/staging/Solvay (8 transcripts + 1 pdf + 1 docx + 1 pptx).
- Driver: scripts/overnight_loop.py (master, packets 28-36; 642 tests). packet-36 adds ok_pct (correct + refused_ok); success check uses held-out ok% >=90, halluc <5, refusal >=80.
  Stop a run only with `kill -TERM <python pid>`; if the tmux wrapper dies: cp <out>/env.original ~/rag-project/.env && docker compose up -d --no-deps lightrag-server.
- Probe files (Thinkpad ~/rag-project/probe/, chmod 600): solvay-dev-v2 (40), solvay-heldout-v2 (40), v2.1 (ho20/ho39 replaced by unanswerables), solvay-heldout-v3 (24 fresh). Judge: qwen3.6-35b on oMLX, rubric v2.

## Results
- Old probes (v1) ceilings 80-82.5%: test/judge quality, not config (6/9 failures probe/judge faults).
- V2 baseline (loop4): dev 82.5 correct / held-out 85.0 correct; counting correct refusals (ok%) dev 97.5, held-out 95; Hermes: 2 of 3 failures were mislabelled probes (ho20, ho39), 1 synthesis error (dev08); judge agreed 5/5 + 3/3.
- V3 fresh held-out (loop6, after loop5 was invalidated by a sigma connection outage 13:19-13:36 UTC, 25 probes affected, judge 500s on oMLX 13:28-13:31): held-out ok 95.8% (23/24; correct 19, refused_ok 4, partial 1 hv06),
  halluc 0, refusal 100%, dev slot (v2.1) 39/40 (ho39 hallucinated: borrowed neighbouring fact), p50 54 s / p95 68 s, 0 boilerplate, 0 judge errors. Driver verdict: success. => accuracy target met on current corpus.
- Remaining weakness: multi-fact synthesis (adds a wrong extra item; garbled-ASR-term synonyms).

## In flight / next three actions
1. Widen scope: restart worker, deploy pptx extractor (packet-27 merged 1f90c0e; image rebuild with python-pptx via Hermes, with rollback), ingest staged pdf/docx/pptx (+ remaining 2 transcripts), then dev-worker authors probes for the spec docs, Hermes re-runs the loop.
2. Re-check accuracy on the wider corpus (new held-out); watch sigma availability (contention with dev-worker and outage risk).
3. Housekeeping: OpenWebUI key rotation, real .docx e2e check, GRAG-42 oMLX memory policy, leftover GSK-quoting files on Thinkpad (probe/, docs/ops/runs) and OpenWebUI chat history (operator has not asked to wipe), Hermes->lead messaging broken (use summary files).

## Access notes
understory: curl POST http://192.168.1.185:3800/mcp (JSON-RPC, tools/call memory_add / memory_query), one request at a time, long timeouts; note path /projects/graphraggle-status-2026-10-12.md.
Plane: ssh macbookm1.local, source ~/plane-selfhost/plane-mcp/agent-tokens.env, REST with X-API-Key; project bfd4e9a9-1ffb-4813-88f8-e44fc3ddd354 (GRAG-43..54 exist; states Todo 97f5df42, In Progress 45b07a8b, Done b318940a).
Hermes approval dialogs only in its tmux pane (memory hermes-approval-dialogs). Hermes has been started on a fresh session (2026-10-13).
