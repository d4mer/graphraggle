# Handoff — lead

Last updated: 2026-10-12 (after rc-1 worker update, LightRAG input-dir fix, packet-26)

## Goal
Get GraphRAG released to production for first use (plan: docs/ops/release-plan-first-use.md). Operator: "get back to dev and release to prod asap".
Keep `master` on GitHub green. I do not write code; dev-worker@graphraggle does (queue). Ops on the Thinkpad (prod) go through
`rig send --host thinkpad ops-hermes@thinkpad-ops "..."` (files-only reports, never rig send back; Hermes approval dialogs are invisible to
the operator — if the pane shows a Dangerous Command dialog for a brief-approved command, select "Allow once" via
`ssh 100.64.0.6 'tmux send-keys -t "ops-hermes@thinkpad-ops" Enter'`).

## Where things stand
- master = fca1b3f (pushed). Tag rc-1 = e436e98. Working branch earlier was packet-22-ll-seed (18 commits ahead of master, unmerged:
  BRIDGE_REWRITE_SEED_LL work + prompt-size investigation); packet-24 (task prompt cap, default OFF) is in master and deployed.
- Prod (Thinkpad ~/rag-project, Docker, ssh 100.64.0.6): gateway p24 image 4629dcab0bea; ingest-worker updated to the SAME image on 2026-10-10
  (backup backups/pre-worker-update-20261010-075725Z; rollback override compose.rollback-worker.yml, image local/rag-gateway:pre-packet-18, unapplied).
- LightRAG v1.5.7 input-dir fix applied 2026-10-11: compose has `--input-dir /data/lightrag_inputs` + volume + `environment: INPUT_DIR: /data/lightrag_inputs`.
  Root cause: upload writes to the CLI arg, parser reads INPUT_DIR env; plus the old input dir overlapped the worker's scan folder (409s).
  Canary .docx and .pdf both ingested and were cleaned up. Backups: backups/compose.yml.pre-inputdir*-2026101*.
  Hazard: recreating the host dir lightrag_inputs leaves a stale bind mount -> restart lightrag-server (I did one restart after Hermes rmdir'd it).
- Repo compose.yml reconciled to prod (packet-26, guard tests, 479 tests pass). DO NOT copy repo compose over prod: prod holds the real OpenWebUI key
  (repo uses ${OPENWEBUI_OPENAI_API_KEYS:-}).
- Packet-25 (scan-based submit) built and REJECTED: LightRAG /documents/scan archives processed files and may re-ingest abs-path text docs. Branch packet-25-impl kept unmerged.
- Flags in force: BRIDGE_TASK_MAX_CHARS=0, BRIDGE_REWRITE_SEED_LL off, MULTI_QUERY on, GRAPH_EXPANSION off, RERANK on, MAX_ASYNC=1.

## In flight
Nothing queued. No open worker or Hermes jobs.

## Decisions pending (operator)
1. RESOLVED 2026-10-12: failed rows triaged (Hermes rc1-failed-triage.md): 4 already searchable, 4 test artifacts (scenario06 placeholder, 2x v157-regression probes), 1 missing probe file, 2 canaries. Operator decision: retry nothing, delete nothing, leave all as is (incl. 5 failed LightRAG stub entries).
2. Gate 4 go/no-go. Evidence: Gate 2 quality baseline (cross-doc 0.56; unanswerable hallucination p35 0/3), latency median ~178 s,
   reboot drill + backup/restore drill done, Office/PDF ingest now works.
3. Rotate the prod OpenWebUI API key (OPENAI_API_KEYS) — it was printed in a diff once (never committed).
4. oMLX memory policy (embeddings+rerank+LLM on one box, swap ~89%); GRAG-42.
5. A real-document check: drop one real .docx into source_docs and confirm it shows in the LightRAG web UI under the current date.
6. Unexplained: sigma->Thinkpad VPN gap (~70 min after the reboot drill); latency gap 178 s vs ~80-90 s estimate (no persistent query log, operator declined QUERY_LOG_PATH).
7. Plane updates (GRAG-41/11/15/16/42) need Plane tools.

## Constraints and hazards
- Shared checkout (/home/maverick/work/graphraggle) is on release-rc1-docs with the worker's uncommitted agents/worker/startup/context.md; use `git worktree`
  (lead worktree: ../graphraggle-lead-wt) for any commit.
- Test command: RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q
- Hermes reports live on the Thinkpad (~/rag-project/docs/ops/runs/rc1-*.md); not committed by operator decision. Judge Hermes claims against its source evidence
  (it misread an input dir once and reported a stale mount as fine).

## Next three actions
1. Ask the operator for the Gate 4 go/no-go.
2. If go: run the real-document check, then tag the release and update docs/08-change-log.md.
3. Decide whether to merge packet-22-ll-seed (BRIDGE_REWRITE_SEED_LL) or leave it parked.
