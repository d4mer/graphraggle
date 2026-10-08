# Release Plan: First Operator Use

Status: DRAFT for operator approval (2026-10-08). Owner: lead. Executor: dev-worker (code), ops-hermes@thinkpad-ops (Thinkpad runs).

## Definition of "first use"

The operator asks real questions about real documents through OpenWebUI against the Thinkpad production stack and
can trust the answers enough to act on them, with a known way back if something breaks.

Assumption to confirm: production is the Thinkpad (`~/rag-project`); packets 20, 21 and 23 are already deployed there.
This plan therefore is mostly about gating, measuring and freezing what exists, not building new features.

## Release candidate (RC)

- Code: what is deployed now = gateway image `local/rag-gateway:p23` (`68c1631d7dd3`), built from branch `packet-23-empty-retry` (`9a7bb9a`).
- Flags: `BRIDGE_REWRITE_NO_THINK=true`, `BRIDGE_EMPTY_RETRIES=1`, `BRIDGE_REWRITE_SEED_LL` off (measured: no evidence it helps).
- Not in the RC: `packet-22-ll-seed` code (flag off; do not deploy).
- Rollback: `docker tag local/rag-gateway:pre-packet-23 local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`.

## Gates (in order; each needs evidence in `docs/ops/runs/`)

### Gate 0 - Freeze and reconcile (lead + worker, no live services)
1. Merge decision: land packet-21 and packet-23 on `master` (or the agreed release branch) so GitHub reflects what runs in production. Drop or park `packet-22-ll-seed`.
2. Run the full suite with the env vars in `agents/lead/startup/context.md`; must be all green.
3. Docs reconciliation: PRD/review describe the repo bridge, not production. Update `docs/05-known-good-config.md`, `docs/08-change-log.md`, `docs/07-openwebui-guide.md` to match the RC (flags, image tag, rollback tag).
4. Tag the RC commit (`rc-1`) and push.
Exit: master == deployed code; docs match; tests green.

### Gate 1 - Operator probe set (OPERATOR, blocks Gate 2) - GRAG-15/16
Operator supplies 30-50 real questions with the document(s) each should be answered from and a one-line "what a good answer contains". Without this there is no objective measure of "precise answers". Lead turns it into the eval harness input (packet-15 harness exists).
Exit: probe set committed to a non-public location or `state/`-adjacent path (not `source_docs/`).

### Gate 2 - Quality measurement on production (ops-hermes)
1. Ingestion check: confirm the operator's actual documents are fully ingested (no stuck/failed status; PDF/DOCX/PPTX/XLSX paths for the file types in use - TODO near-term #4).
2. Run probe set through OpenWebUI path (gateway), single run plus repeat to estimate variance (earlier work showed draw spread ~0.27, so one run is not enough; use >=3 draws).
3. Record: answered/empty/refused counts, keyword/rewrite fallback counts, `empty_retries_used`, latency p50/p95.
Acceptance (proposed, operator to adjust): no empty answers; no gateway 5xx; >= agreed share of probes judged good by operator on blind review; no regression vs. pre-packet-20 baseline on the same set.
Exit: report `docs/ops/runs/rc1-quality-report.md`.

### Gate 3 - Operational readiness (ops-hermes + lead)
1. Reboot recovery drill per `docs/11-first-5-minutes-after-reboot.md`; stack returns healthy.
2. Backup of `lightrag_store/` and `state/` taken and a restore verified on a scratch dir (TODO longer-term #4 is currently unmet; this is the one real gap before trusting it with real data).
3. oMLX memory policy decided (GRAG-42) so the LLM/embedding hosts do not evict models mid-query.
4. Remove leftover test containers (`rag-gateway-p22-S0`, `rag-gateway-p22-S1`).
5. Walk `docs/ops/release-readiness-checklist.md` and `docs/ops/live-stack-verification-checklist.md`; all items checked with evidence.
Exit: report `docs/ops/runs/rc1-ops-readiness.md`.

### Gate 4 - Go / no-go (operator)
Lead presents: Gate 0-3 evidence, remaining known risks, rollback command. Operator says go. Nothing after the freeze is deployed without a new go.

### Gate 5 - First use and hypercare
1. Operator uses the system on real work for one agreed period (suggest 1 week).
2. Failure log: operator notes bad answers (question, expected, got). Lead triages weekly: bug (worker packet), tuning (flag), or accepted limitation.
3. Any production regression -> rollback first, diagnose second (`docs/ops/rollback-and-containment.md`: capture evidence before changing state).
Exit: hypercare review; decide next packet from the failure log, not from the backlog.

## Known risks carried into first use
- Retrieval quality on edge cases is unmeasured against the operator's questions until Gate 2.
- Empty-retry path (packet-23) was not exercised live (`empty_retries_used=0` in acceptance).
- No tested backup/restore until Gate 3.2.
- `pipeline_status` endpoint hangs on the Thinkpad; do not use it as a health check.
- Thinkpad agent approval prompts time out (~6 min); keep its briefs short and non-interactive.

## Out of scope for first use
Seed (ll-seed) work, MCP/OpenCode integration, multi-user auth hardening, streaming query, reindex workflow rework (TODO medium/long term).

## Critical path
Operator probe set (Gate 1) is the long pole. Gates 0 and 3 can run in parallel with it; Gate 2 cannot start until it exists.

## Next actions
1. Operator: confirm assumptions above and supply the probe set.
2. Lead: queue Gate 0 to dev-worker as a docs/merge unit (after operator confirms merge target).
3. Lead: brief ops-hermes on Gate 3.1/3.2/3.4 (backup/restore drill, container cleanup), which do not depend on the probe set.
