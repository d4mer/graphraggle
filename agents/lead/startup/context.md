# Handoff — lead

Last updated: 2026-10-08

## Goal

Make the GraphRAG/LightRAG document-query stack give far more precise answers
(OpenWebUI via the Ollama-compatible bridge). Lead delegates all code to
`dev-worker@graphraggle`, verifies, and integrates. Data stays local (no outside
models); latency is not a concern; thinking is controlled per request only.

## Current state

- Repo `git@github.com:d4mer/graphraggle.git` on sigma at `~/work/graphraggle`.
  Active dev branch: `packet-21-standalone-query` at `376af5e` (includes the merged
  worker dedupe fix `2f74255`). Work happens on packet branches, not `master`
  (operator preference: separate branches for code changes).
- Production is the **Thinkpad** (`~/rag-project`), not this machine. Per the operator
  message of 2026-10-07 (not verified from here): packet-20 keyword supply and
  packet-21 standalone-query rewrite are DEPLOYED (gateway image `31395c52e22e`,
  `BRIDGE_REWRITE_NO_THINK=true` in `.env`, acceptance 4/4, rollback tag
  `pre-packet-21`, gate report `docs/ops/runs/stage0-standalone-query-report.md`
  on the Thinkpad). Do not deploy anything from here.
- Test suite here: **435 passed** with env vars `RAG_API_KEY=test
  LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621
  SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db` and
  `python-multipart` installed in `.venv` (it was missing; installed 2026-10-07).
- Live services are not running on sigma; only offline unit tests.

## In flight

None in the queue. packet-23 (`BRIDGE_EMPTY_RETRIES`, default 1, retry once on an empty 200 stream) verified 2026-10-08:
branch `packet-23-empty-retry` (`9a7bb9a`, off `376af5e`), 447 tests pass, 71 fail on old app code,
not pushed, not deployed. Rows `qitem-20261008062345-2dcac58f` and `qitem-20261008063132-fb40e55e` closed.

Done 2026-10-07: `qitem-20261007144357-dfa1720e` (worker seed work),
closed via handoff `qitem-20261007150408-931a1aeb` and recovery row
`qitem-recovery-80b5e9af32b4884b`. Verified: `packet-22-ll-seed` (`d4c31cc`, off `376af5e`),
450 tests pass, new tests fail on old code, pushed to origin. Flag `BRIDGE_REWRITE_SEED_LL`
defaults off.

**Seed measured 2026-10-07 (Thinkpad agent ops-hermes@thinkpad-ops, report
`docs/ops/runs/stage0-ll-seed-report.md` there): NO EVIDENCE it helps; keep default off.**
10 conversations x 2 draws x 2 arms. Mean overlap with P3: S0 0.663, S1 0.617 (delta -0.046,
median draw spread 0.268). c01 NOT recovered (0.562 -> 0.500, within spread 0.125).
Mechanism fires correctly (`rewrite_ll_seeded` true 16/16 rewritten S1, never otherwise).
Would need >=4 draws to read a delta under ~0.25. Branch `packet-22-ll-seed` stays unmerged
with the flag off (or can be dropped). Test containers `rag-gateway-p22-S0/S1` (:8021/:8022)
were left running on the Thinkpad; stop with `docker rm -f rag-gateway-p22-S0 rag-gateway-p22-S1`.

Reaching the Thinkpad agent: it is not on this rig; use `rig send --host thinkpad
ops-hermes@thinkpad-ops "..."` and `rig capture --host thinkpad ...`. Its approval prompts
time out (~6 min) and do not reach the operator reliably; keep briefs free of
`pipeline_status`-style checks (that endpoint hangs on the Thinkpad).

## Decisions and constraints

- Do not brief the worker on anything needing live services; measurement of the seed
  on real retrieval is a separate job for the Thinkpad agent (Part C harness
  `~/rag-project/c21`), after the flag exists. Default stays off until measured.
- Verify before closing: run the suite myself, read the diff, and confirm new tests
  fail on the old code.
- Logs never carry prompt, history, rewrite or keyword text (counts/enums only).

## Next three actions

1. Push packet-23 on the operator's go and ask the Thinkpad agent to deploy it; also find out why 2 of 40 Part C
   keyword steps fell back.
2. Docs reconciliation (review/PRD describe the repo bridge, not production); Plane: update
   GRAG-41/GRAG-11 with the seed result (needs the Plane tools).
3. Operator items: probe set of 30-50 questions (GRAG-15/16), oMLX memory policy (GRAG-42),
   stop the two test containers on the Thinkpad.
