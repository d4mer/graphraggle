# Handoff — lead

Last updated: 2026-10-08

## Goal

Make the GraphRAG/LightRAG document-query stack give far more precise answers
(OpenWebUI via the Ollama-compatible bridge). Lead delegates all code to
`dev-worker@graphraggle`, verifies, and integrates. Data stays local (no outside
models); latency is not a concern; thinking is controlled per request only.

## Current state

- Repo `git@github.com:d4mer/graphraggle.git` on sigma at `~/work/graphraggle`.
  Active branch (operator correction 2026-10-08; CLAUDE.md's "master" is stale):
  `packet-22-ll-seed`, HEAD `4cd7e09` (contains packet-21 + dedupe fix `2f74255`). Work happens on
  packet branches, not `master`.
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

## In flight (updated 2026-10-08 evening)

- `qitem-20261008193140-7f2bdffa` packet-24 (cap OpenWebUI task prompts, `BRIDGE_TASK_MAX_CHARS` default 24000, log
  `prompt_chars`/`forwarded_chars`) with dev-worker on branch `packet-24-task-prompt-cap` (off packet-23). OPERATOR
  AUTHORISED DEPLOY once I verify (suite, diff, tests fail on old code). Deploy via ops-hermes with rollback tag
  `pre-packet-24`, acceptance, then report.
- ops-hermes (Thinkpad, `rig send --host thinkpad ops-hermes@thinkpad-ops`): Part A quality run (draw 2/3), then Part B
  backup/restore drill, Part C cleanup (`docs/briefs/gate2-gate3-ops.md`); addenda: oMLX tps study (ssh macbookm1 read-only
  approved) and task_prompt correlation. Finding so far: slow decode = 45k-80k uncached prompts + swap/model-unload thrash on
  macbookm1; suspect OpenWebUI task prompts. Release plan: `docs/ops/release-plan-first-use.md` (Gate 1 closed; Gate 0
  waiting on merge-target decision; reboot drill + oMLX memory policy need operator).

## Earlier (done)

Nothing in flight; all queue items terminal (stale pending row `qitem-20261007104613-3a8ea104` closed 2026-10-08). packet-23 (`BRIDGE_EMPTY_RETRIES`, default 1, retry once on an empty 200 stream) verified 2026-10-08:
branch `packet-23-empty-retry` (`9a7bb9a`, off `376af5e`), 447 tests pass, 71 fail on old app code,
pushed to origin and DEPLOYED to the Thinkpad 2026-10-08 (operator go; image `local/rag-gateway:p23` `68c1631d7dd3`,
rollback tag `pre-packet-23` = `31395c52e22e`, acceptance 2/2 with `empty_retries_used=0`, retry path not exercised live,
test containers removed; report `docs/ops/runs/stage0-empty-retry-deploy.md` on the Thinkpad; rollback:
`docker tag local/rag-gateway:pre-packet-23 local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`). Rows `qitem-20261008062345-2dcac58f` and `qitem-20261008063132-fb40e55e` closed.

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

Reaching the Thinkpad agent (live): `rig send ops-hermes@thinkpad-ops@host-765234ea "<msg>"`. Its approval prompts
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

1. Keyword fallbacks explained (Thinkpad agent, 2026-10-08, report `docs/ops/runs/stage0-keyword-fallback-reasons.md`):
   Part C 2/40 (c07 P1, c08 P1) were rewrite-OFF context-less follow-ups, 3 attempts in ~2.7 s, so validation
   rejection (not timeout, not http error; exact enum lost with the removed containers). c22 0/40, prod 0/4 (log
   window short after redeploy). Not a bridge bug; packet-21 removes that input class. No action. Small gap:
   persist `keyword_failure_reasons` in the measurement driver's capture.
2. Docs reconciliation (review/PRD describe the repo bridge, not production); Plane: update GRAG-41/GRAG-11 with the
   seed result and packet-23 deploy (needs the Plane tools).
3. Operator items: probe set of 30-50 questions (GRAG-15/16), oMLX memory policy (GRAG-42). Local docs commits on
   `packet-22-ll-seed` are unpushed.
