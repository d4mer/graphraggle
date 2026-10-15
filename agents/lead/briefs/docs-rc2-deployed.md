# Brief: record the rc-2 production release in the docs (master, docs only)

Work in /home/maverick/work/graphraggle on master (base origin/master). Docs only, no
code. Hand back the diff; do not commit or push.

Facts (from Hermes' run report, Thinkpad ~/rag-project/docs/ops/runs/release-rc2.md,
2026-10-15): tag `rc-2` = commit `11b1833`. Gateway `rag-gateway-api` and worker
`rag-ingest-worker` both run `local/rag-gateway:rc2` = image `d0f31398fec5` (built from
the lead's git-archive tarball, 23 files sha256-verified). Previous image `4629dcab0bea`
(p24 = rc-1) tagged `local/rag-gateway:pre-rc2`. The worker had been on rc-1 since
2026-10-10, NOT on pre-packet-18 `d9d9ce2307a2` (the docs are stale). No `.env` change;
`COMPANY_AWARE_DEDUPE` stays absent (default false). State DB backup before the release:
`~/rag-project/backups/pre-rc2-20261015T083456Z/`. Acceptance all passed; 5-min
observation 0 errors, 0 refused writes, status counts unchanged (10 ingested).
Rollback: `docker tag local/rag-gateway:pre-rc2 local/rag-gateway:latest && docker compose
up -d --no-deps gateway-api`; worker via `compose.rollback-rc2.yml`
(`docker compose -f compose.yml -f compose.rollback-rc2.yml up -d --no-deps ingest-worker`).

Do:
1. docs/05-known-good-config.md: runtime table rows for gateway and worker -> rc2
   (`d0f31398fec5`, commit `11b1833`, 2026-10-15); fix the stale worker row; keep the
   "Settings Shipped Dark" section.
2. docs/08-change-log.md: new dated Production Releases entry for rc-2 listing what is now
   live (TODO #2 lifecycle table + CAS, TODO #3 one duplicate/skip rule, packet-27 pptx
   extraction, COMPANY_AWARE_DEDUPE shipped OFF, packet-18 dedupe hardening was already
   live); rollback tag `pre-rc2`. Rewrite Pending: delete items 1 and 4 (resolved) and
   correct item 1's premise.
3. Operational findings to record (docs/04-troubleshooting.md or 08, your choice):
   a. `GET /ingest/status` requires `Authorization: Bearer <RAG_API_KEY>` (the route has
      had `require_bearer` since before rc-2; Hermes saw an unauthenticated 200 earlier,
      cause unexplained: say "verify the auth you use").
   b. The worker logs NOTHING on clean scans (no logging in worker.py); liveness is
      visible only as CPU or via refused-write/warn lines. Say how to check liveness.
   c. Live Thinkpad compose.yml inlines the Open WebUI OpenAI key; the repo form uses
      `${OPENWEBUI_OPENAI_API_KEYS:-}`. Operator follow-up, key rotation not yet done.
4. Bare-env suite must still pass (698 on master).
