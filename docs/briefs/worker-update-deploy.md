# Brief: update the production ingest worker to the rc-1 build (operator authorised 2026-10-09)

To: ops-hermes@thinkpad-ops. Write results to FILES (docs/ops/runs/rc1-worker-update.md under ~/rag-project and a copy in probe/runs/); do NOT `rig send` to the lead (it blocks on an unseen approval). The lead reads the files over ssh.

## Facts
- `rag-ingest-worker` has run for ~11 days on image d9d9ce2307a2 (a pre-packet-18 build, tag local/rag-gateway:pre-packet-18).
- In compose, `ingest-worker` and `gateway-api` share the same image tag `local/rag-gateway:latest`. Previous deploys used `up -d --no-deps gateway-api`, so the worker was never recreated.
  `latest` is now p24 (4629dcab0bea, = commit c89b293 = what tag rc-1 contains in app/). So updating the worker is just recreating that one service.
- New worker code vs the old container: dedupe hardening (2f74255: identical files must end up as ONE ingested document; mutual-duplicate bug and orphaned duplicates fixed) plus shared-module changes since packet-18.
  Risk to watch: the new dedupe logic may change the status of EXISTING documents that share a sha256 with another document.

## Steps (stop and report at the first failure; production query path must stay up the whole time)
1. Pre-checks: worker idle (no ingest activity in its log for 15 min; no document with status processing/enqueued in the state DB); gateway health 200.
2. Snapshot BEFORE: `state/ingest.db` via the SQLite online-backup API into ~/rag-project/backups/pre-worker-update-<timestamp>/ with sha256; plus counts of documents by status and the number of rows sharing a sha256 with another row (counts only).
3. Rollback preparation (do not apply): write ~/rag-project/compose.rollback-worker.yml, an override that sets `ingest-worker: image: local/rag-gateway:pre-packet-18`, and prove it parses with `docker compose -f compose.yml -f compose.rollback-worker.yml config` (output not printed, just success).
4. Update: `docker compose up -d --no-deps ingest-worker` (ONLY this service). Confirm the container id changed, image is 4629dcab0bea, RestartCount=0, gateway-api untouched (same container id, health 200).
5. Observe 5 scan cycles (INGEST_SCAN_INTERVAL_SECONDS=30, so ~3 min): worker log has scan lines and no Traceback/ERROR; counts of documents by status AFTER equal BEFORE; state DB still opens. If any status count changed, do NOT fix anything: report exactly which statuses and how many rows, and stop.
6. Functional check without polluting the corpus: do NOT add files to source_docs/ or uploads/. Instead run the repo's dedupe unit tests inside nothing production-related is needed; just state that the code under test is covered by the repo suite (467 passed on rc-1). If you believe a live check is essential, ask the lead first.
7. Write the report: image ids before/after, container ids, status counts before/after, log findings, rollback commands:
   `docker compose -f compose.yml -f compose.rollback-worker.yml up -d --no-deps ingest-worker` (and, if statuses were altered, restore ingest.db from the pre-update backup while the worker is stopped).
8. If anything goes wrong at any step: roll back immediately with the override, say so in the report, and stop.
