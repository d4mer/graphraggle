# Hermes brief: release rc-2 (gateway + ingest worker) to the Thinkpad

**Status: DRAFT. Not approved. Do not start until the operator approves in
writing. Approval prompts appear only in your tmux pane; the operator will be at
the terminal.**

To: ops-hermes@thinkpad-ops. Write results to files, not `rig send` (a send blocks
on an unseen approval). Report file:
`~/rag-project/docs/ops/runs/release-rc2.md`. Lead reads it over ssh.

## What and why
Dev is frozen at tag **`rc-2`** (commit `11b1833`, tag object `01bb28b`), pushed to
origin. The repo has moved on since the running images (gateway `p24` =
`4629dcab0bea` = commit `c89b293`; worker possibly still `d9d9ce2307a2`, a
pre-packet-18 build: **record what is actually running, do not assume**). Changes
in `app/` since `c89b293` (5 files, +881/-30):

1. **TODO #2 lifecycle table + compare-and-set write-backs** (`state_store.py`,
   `worker.py`): illegal status transitions are refused (no-op + log line), poll
   write-backs are CAS'd on `track_id`/`status`.
2. **TODO #3 one duplicate rule, one skip rule** for upload and scan; `.part`
   files always skipped.
3. **packet-27: `.pptx` extraction in the worker**, submitted as text.
4. **COMPANY_AWARE_DEDUPE**, shipped **OFF** (`company_aware_dedupe` default
   false). **It must stay off. Do NOT add it to `.env`.** Reason and probe result:
   `docs/05-known-good-config.md` § Settings Shipped Dark.
5. Plus the packet-18 dedupe hardening (`2f74255`) if the worker is still pre-18.

No schema change: `git diff c89b293 rc-2` has no CREATE/ALTER/PRAGMA lines.
`Containerfile` and `requirements.txt` are unchanged in kind, so the build is the
same recipe as packet-24.

Also in the tag but **not a Thinkpad action**: Open WebUI pin 0.12.0 in
`compose.yml` (production already runs it, you did that cutover).

## Hard rules
- Production query path (gateway :8020, LightRAG :9621, Open WebUI :3010) stays
  up as much as possible; gateway restart is one brief blip. LightRAG and Open
  WebUI are **not touched**.
- Build from the **tag**, not a branch tip. Verify the source you build is commit
  `11b1833a4c269452ce1f8123ee463cb131cca7c3` before building. The `chunking`
  branch is separate and must not be built or deployed.
- Do not add or change any `.env` value. Do not enable any feature. Do not touch
  `source_docs/` or `uploads/` content (no synthetic files in the corpus).
- Worker may be mid-extraction. If any document is `processing`/`enqueued`
  (state DB) or LightRAG shows `analyzing`/`processing`, **wait**; report the
  count and do not interrupt it.
- Stop at the first failed check, roll back with the commands below, say so, and
  stop. Do not retry a denied approval; report it.

## Steps
1. **Pre-flight (record, change nothing):**
   - `docker ps` lines for rag-gateway-api and rag-ingest-worker; image ids and
     tags they run; `docker image ls local/rag-gateway`.
   - gateway `/health` 200; LightRAG `/health`.
   - state DB: documents by status; rows sharing a sha256 with another row;
     count of rows with `retry_count > 0` (counts only).
   - LightRAG upstream: documents by status (counts only).
   - `source_docs/`: count of files by extension (counts only; say how many
     `.pptx` are present and where, because rc-2 will start extracting them).
   - diff of `~/rag-project/compose.yml` against the repo's `compose.yml` at
     `rc-2`: report the differing lines only, **do not copy the repo file over**.
2. **Snapshot before:** SQLite online-backup of `state/ingest.db` to
   `~/rag-project/backups/pre-rc2-<UTC-stamp>/` with sha256 (same as the
   packet-18 worker update). Keep on the Thinkpad.
3. **Rollback preparation:** tag the images that run now:
   `docker tag local/rag-gateway:latest local/rag-gateway:pre-rc2` (and, if the
   worker runs a different image id, `...:pre-rc2-worker` for it). Write
   `~/rag-project/compose.rollback-rc2.yml` overriding `ingest-worker` to the
   worker rollback tag, prove it parses with `docker compose -f compose.yml -f
   compose.rollback-rc2.yml config` (do not print the output).
4. **Build:** image `local/rag-gateway:rc2` from commit `11b1833` (same recipe
   as packet-24). Record the image id. Run `python -c "import app.api,
   app.worker"` inside the new image (import check only, no network).
5. **Deploy gateway:** `docker tag local/rag-gateway:rc2 local/rag-gateway:latest`
   then `docker compose up -d --no-deps gateway-api`. Do not touch the worker yet.
   Acceptance (counts and judgment only):
   a. container id changed, RestartCount=0, image is the rc2 id;
   b. `/health` 200; `/api/tags` lists the model;
   c. two normal bridge queries about content already in the corpus return
      non-empty answers with a Sources block; bridge log lines have
      `prompt_chars` and no `forwarded_chars`; no log value contains prompt text;
   d. one synthetic `### Task:` request (made up, no corpus text) logs
      `task_prompt=true`, `prompt_chars == forwarded_chars`, non-empty answer;
   e. `GET /ingest/status` summary counts equal the step 1 counts.
6. **Deploy worker** (only if step 5 passed and nothing is processing):
   `docker compose up -d --no-deps ingest-worker`. Confirm container id changed,
   image is the rc2 id, RestartCount=0, gateway-api container id unchanged.
7. **Observe 5 scan cycles** (30 s each, ~3 min): worker log has scan lines, no
   `Traceback`/`ERROR`; counts of documents by status after **equal** before; the
   state DB opens. Also report the number of refused-write log lines (the new
   lifecycle guard logs these) with their reason codes only. A non-zero count
   on an idle corpus is a finding to report, not something to fix.
   If any status count changed: do not fix anything, report which statuses and
   how many rows, and stop.
8. **Live path check without polluting the corpus:** do not add files. If
   `.pptx` files are present in `source_docs/` the worker will now try them:
   report how many, their final status and error codes, and chunk counts
   (counts and shapes only).
9. **Report** to `release-rc2.md`: image ids before/after, container ids, tag
   names, status counts before/after, acceptance results, log findings,
   refused-write counts, the compose diff from step 1, and the rollback commands.

## Rollback (any failed check; production query path first)
- Gateway: `docker tag local/rag-gateway:pre-rc2 local/rag-gateway:latest &&
  docker compose up -d --no-deps gateway-api`
- Worker: `docker compose -f compose.yml -f compose.rollback-rc2.yml up -d
  --no-deps ingest-worker`
- If status counts were altered: stop the worker, restore `state/ingest.db`
  from the step 2 backup, restart the worker on the rollback image.
- Confirm `/health` 200 and counts equal step 1 after rolling back.

## Out of scope
- LightRAG upgrade or settings, Open WebUI, `.env` values, key rotation.
- Enabling `COMPANY_AWARE_DEDUPE` or any other dark setting.
- Chunking changes (separate `chunking` branch; they will require a reindex and
  a new baseline and are not part of this release).
- Widening the corpus.

## Decision for the operator before approving
This release restarts the gateway (one brief blip for queries) and replaces the
worker image. If the corpus is being added to right now, say so, so Hermes
waits for the processing queue to be empty before step 6.
