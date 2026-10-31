# Handoff - lead

Last updated: 2026-10-31 (structure-cutover-2 LIVE; PPTX ingested; both backups off-host). Replaces the 2026-10-14 handoff.

## Goal
Run GraphRAG in production for the Solvay corpus. Lead delegates code to dev-worker@graphraggle, prod ops to Hermes
(ops-hermes@thinkpad-ops, `rig send --host thinkpad`). RULE: only Hermes reads corpus/LightRAG content (memory
corpus-data-access-hermes-only); dev-worker may read SOURCE transcripts in /home/maverick/work/solvay-src for probes only.
Hermes cannot message lead: it writes ~/rag-project/docs/ops/runs/structure-cutover.md (STATUS on line 1); lead reads it over ssh.

## State (verified 2026-10-30/31)
- Production runs the structure-ingested store, code = tag `structure-cutover-2` on 0047a69 (image local/rag-gateway:structure-cutover-2 = b2647c2579cf).
- Chunking: app/structure_chunker.py (our code decides every cut; delimiter + LightRAG split_by_character_only; no size budget,
  ceiling 7500 tokens for the BGE-M3 8192 window; merge floor 150; turns/headings/tables/lists rules). TRANSCRIPT_PREP_ENABLED false.
- Prod .env: BGE-M3 (bge-m3-mlx-fp16, dim 1024), STRUCTURE_CHUNKING_ENABLED/WORKER_PDF/DOCX_EXTRACT true,
  RELATED_CHUNK_NUMBER=1 (rerank cap, operator option B), RERANK_TIMEOUT=600. Rerank = jina-reranker-v3-mlx on oMLX (macbookm1).
- Store: 11 documents (10 original + Solvay_OPR_ECCFlatfiles_v2.pptx), 135 chunks, 1,483 entities, 1,867 relations. Staged set fully ingested.
- Recall (100 probes, evidence_in_context): 86/100 after cutover and unchanged after the PPTX (zero flips). Validated build uncapped was 91/100;
  the -5 is the R=1 depth cost on unseen held-out probes. Legacy production before cutover was 85. p50 ~85 s, p95 ~128 s.
- Backups, all verified bad=0 and also on deltahome: pre-cutover-20261030T075612Z (~/backups/graphraggle-cutover-...),
  pre-pptx-20261030T184549Z (~/backups/graphraggle-pptx-...), timeout-pre-20261030T155212Z (local only).
- Stray containers on the Thinkpad: lightrag-server2 (stack2, used for the A/B), rag-gateway-api2 (old rc2). Stack2 was stopped after the R=3 arm.

## Open / next
1. Stack2 R=5 arm (RELATED_CHUNK_NUMBER=5, RERANK_TIMEOUT=900) is QUEUED, needs operator GO. R=3 gave 29/34 held-out at p50 ~244 s, too slow.
2. Next-GO candidates from Hermes (operator decides): EMBEDDING_TIMEOUT raise; image patch so RERANK_TIMEOUT>300 is honoured
   (aiohttp ClientTimeout); the R=5 arm. Possible recovery of the 5 lost probes needs faster reranking, not just a bigger R.
3. The heading fix (f7051e6, 13 all-caps lines in the S/4 PDF) was NOT deployed; ship only after its own validation.
4. Sigma (:1234) is a single-request server shared with Hermes and other consumers; contention explains outlier latencies. Engine
   switched Strata -> Basalt on 2026-10-30. Do not use a sigma "quiet gate" (self-referential).
5. New documents need sourcing; any ingest goes through a Hermes brief (see agents/lead/briefs/hermes-pptx-ingest.md as the pattern:
   snapshot, scratch dry-run, ingest, verify counts, 100-probe regression).
6. Housekeeping: OpenWebUI key rotation, GRAG-42 oMLX memory policy, leftover GSK-quoting files on Thinkpad, deltahome cleanup candidates
   (listed in the report, operator call), reconcile.py root-owned-DB fix is in the Thinkpad scratch copy only.

## Repo hygiene (HARD RULE, operator 2026-10-14)
- Lead works ONLY on `master` in /home/maverick/work/graphraggle. First action each session and before any brief:
  `git fetch && git rev-list --left-right --count HEAD...origin/master`. Master is currently AHEAD of origin by this session's brief commits
  (unpushed; push needs operator authorization). git status should show only rig scaffolding.
- Worker briefs name the base: branch from current origin/master. Briefs and handoffs are committed the same day.
- Commit code only after the full suite passes (`.venv/bin/python -m pytest tests/ -q`); docs-only commits do not need it.
- Never push or change production without the operator; Hermes executes production changes only on an explicit GO.

## Access notes
understory: curl POST http://192.168.1.185:3800/mcp (JSON-RPC memory_add/memory_query). Plane: ssh macbookm1.local, tokens in
~/plane-selfhost/plane-mcp/agent-tokens.env, project bfd4e9a9-1ffb-4813-88f8-e44fc3ddd354. Hermes approval dialogs appear only in its
tmux pane (memory hermes-approval-dialogs).
