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

## In flight (updated 2026-10-09)

GOAL NOW: release to prod ASAP (plan `docs/ops/release-plan-first-use.md`). Production = Thinkpad, already running the RC:
gateway `local/rag-gateway:p24` (4629dcab0bea, commit c89b293 on branch `packet-24-task-prompt-cap`, rollback tag `pre-packet-24`=68c1631d7dd3).
- `qitem-20261009210610-00299873` (dev-worker): docs-only reconciliation, branch `release-rc1-docs` off packet-24
  (brief `docs/briefs/release-rc1-docs.md`). When it hands back: verify diff is docs/ only, suite 467 passed, no secrets, then
  fast-forward `origin/master` (currently 2f74255) to it, tag `rc-1`, push.
- OPERATOR DECISION 2026-10-09: update the production ingest worker AFTER rc-1 (prod `rag-ingest-worker` runs old image d9d9ce2307a2,
  pre-packet-18; repo has dedupe fix 2f74255). Do via ops-hermes with a rollback tag, acceptance on a test file, report.
- Done/closed: Gate 1 (probe set, 36 q, ~/rag-project/probe on Thinkpad), Gate 2 run (rc1-quality-report: cross-doc 0.56, unanswerable 0.67,
  0 empty/5xx, p50 178 s), Gate 3 backup/restore drill PASS, reboot drill PASS (stack back in ~1 min; sigma lost ssh to Thinkpad ~70 min, unexplained).
- Still open (operator): oMLX memory policy (GRAG-42; embeddings+rerank+LLM all on 192.168.1.190), go/no-go (Gate 4).
- Findings: bridge answers ~32.5k prompt tokens, ~58 tps decode, ttft ~46 s; the 5-6 tps in oMLX is non-streaming helper calls (prefill included in
  tps) plus contention; 45-80k prompts are another client (max_tokens=32000). Prod has MULTI_QUERY_ENABLED=true. Persistent query log is
  not set (QUERY_LOG_PATH unset) - operator declined changing it for now.
- ops-hermes (Thinkpad): approvals for `rig send` to me block unseen; tell it to write result FILES and read them via
  `ssh 100.64.0.6`. After a reboot its seat is a bare shell: `ssh 100.64.0.6 'rig launch 01M4DDRGCNP2SFY3FB9TYP5KGG ops.hermes'`
  then in its tmux pane `cd ~/rag-project && hermes chat -c`. Its `rig send` envelope garbage goes into a shell if the agent is not running.
- Shared-checkout hazard: the worker and I share this cwd; use `git worktree add` for any commit on a branch other than the checked-out one.
- A production API key appeared in a diff I printed (prod compose.yml has it in clear); never copied into repo; rotation advisable.

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
