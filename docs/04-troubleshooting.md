# Troubleshooting

## Document stuck at `auto_split`

Symptom: document has `validation_state: auto_split`, `status: rejected`, and `error_message` says the file is too large.

Cause: file exceeds the 500 KB automatic-split threshold.

Fix:
1. Split the file into smaller parts (< 500 KB each)
2. Re-upload each part separately
3. Check `warnings_json` on the original document for details

## Document stuck at `reject`

Symptom: document has `validation_state: reject`, `status: failed`.

Check `error_stage` and `error_code` to understand the failure point:

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | \
  jq '.documents[] | select(.status=="failed") | {
    document_id,
    error_stage,
    error_code,
    error_message,
    error_detail
  }'
```

Common `error_stage` values:
- `validate` — failed pre-ingest validation gate
- `submit` — failed when submitting to LightRAG (transient)
- `track` — LightRAG rejected the track submission

Common `error_code` suffixes:
- `_transient` — temporary failure; safe to reindex
- `_terminal` — permanent failure; fix the root cause first

If `error_stage` is `submit` and `error_code` ends in `_transient`, reindexing will retry:

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_id":"YOUR-DOC-ID","force":true}'
```

## Duplicate rejection

Symptom: upload returns HTTP 409 or document has `error_code: duplicate_content`.

Cause: SHA-256 content hash matches a previously uploaded document.

Fix: either use the existing document, or rename and re-upload if you need a separate copy.

## `GET /ingest/status` returns 401

Cause: the route is bearer-guarded (`Depends(require_bearer)`, `app/api.py`) and has
been since at least commit `3284cc5` (2026-05-05), long before rc-2. A missing or wrong
`Authorization` header is the usual answer:

```bash
curl -s http://localhost:8020/ingest/status -H "Authorization: Bearer $RAG_API_KEY"
```

Verify the **auth you use and the port you hit**: production publishes the gateway on
host `8020` → container `8000`, while several older examples in these docs use
`:8000`, which only resolves from inside the compose network (or against an older
mapping).

One caveat worth recording: during the rc-2 acceptance run on 2026-10-15 an
unauthenticated request was reported returning **200**. The cause was never
established. Treat "it answered without a token" as a reason to check *what you are
pointed at* (another service, a proxy, an older container) rather than as evidence
that the guard is optional.

## The ingest worker logs nothing on a clean scan

Symptom: `docker logs rag-ingest-worker` shows nothing new and you cannot tell whether
the worker is alive.

Cause: this is expected. The worker emits **no** output for a scan that finds nothing
to do. The only lines it ever writes are `WARNING: ...` on stderr and the lifecycle
refusals from the `gateway.state_store` logger (`refused status transition …`).
Silence means "nothing to do", not "dead".

Check liveness with any of:

```bash
docker stats --no-stream rag-ingest-worker      # a scan costs CPU; periodic activity
docker inspect -f '{{.State.Status}} {{.State.StartedAt}}' rag-ingest-worker
stat -c %y ~/rag-project/state/ingest.db        # mtime moves when the worker writes
```

Or make it do work and watch the effect: `POST /reindex`, then watch
`GET /ingest/status` → `summary.by_status` and the rows' `updated_at` move.

Two kinds of silence that are also normal: a row whose bytes match the recorded
`sha256` and whose status is already terminal (`ingested`, `rejected`, or flagged
`auto_split`) is returned before any retry bookkeeping, and so is a row that hit a
non-retryable submit failure (`submit_terminal_failed`) at the `INGEST_MAX_RETRIES`
limit — its `retry_count` stays where it was and nothing further happens until the next
`/reindex`. If you see `refused status transition` in the logs, the worker **is**
running but a write is being blocked by the lifecycle guard — see
`decisions/0001-lifecycle-model.md` for that log format.

## `podman compose` tries to use Docker Compose

Symptom:

```text
Executing external compose provider ... docker-compose
```

Cause:
1. Podman is delegating to Docker Compose

Fix used during setup:
1. remove Docker credential helper dependency from `~/.docker/config.json`

## LightRAG image tag not found

Symptom:

```text
manifest unknown
```

Cause:
1. wrong image tag

Correct image:

```yaml
image: ghcr.io/hkuds/lightrag:v1.4.15
```

## Local app image pull denied

Symptom:

```text
denied: requested access to the resource is denied
```

Cause:
1. compose tried to pull `local/rag-gateway:latest`

Fix:
1. both `gateway-api` and `ingest-worker` must have `build:`
2. both must have `pull_policy: never`

## Gateway health upstream error

Symptom:

```json
"message":"All connection attempts failed"
```

Cause:
1. LightRAG not reachable or crash-looping

Checks:

```bash
podman logs lightrag-server --tail=200
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

## Wrong embedding model

Symptom:

```text
Model 'bge-m3-mlx' not found
```

Correct value:

```bash
EMBEDDING_MODEL=bge-m3-mlx-fp16
```

## Upload ingests only one line of text

Cause:
1. text files were being sent through the binary upload path

Fix:
1. text files must go through LightRAG `POST /documents/text`
2. binary docs continue using `POST /documents/upload`

## Text file ingestion fails with Unicode decode errors

Symptom:

```text
'utf-8' codec can't decode byte ... invalid start byte
```

Cause:
1. the text file is not UTF-8 encoded
2. common examples are Windows-1252 or Latin-1 transcripts

Fix:
1. worker should try decoding text files with fallbacks:
   - `utf-8`
   - `utf-8-sig`
   - `cp1252`
   - `latin-1`
2. text-like files should still be sent through LightRAG `POST /documents/text`

Operational recovery:

```bash
cd ~/rag-project
podman compose up -d --build
```

Then re-upload the failed file and recheck:

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Duplicate upload records

Cause:
1. worker was processing LightRAG internal `__enqueued__` files

Fix:
1. ignore `source_docs/__enqueued__`
2. keep uploads only in `uploads/`

## Query returns no relevant context

Checks:

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Make sure target docs are `ingested` and `query_ready: true`.

If you scoped the query by company, confirm the target docs have matching `company`:

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | \
  jq '.summary.by_company'
```

Unscoped documents are excluded from company-scoped queries. If a document was uploaded without a company and you later query with `company=...`, it will not appear in results.

## Document is ingested but `query_ready` is false

Check `readiness_reason`:

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | \
  jq '.documents[] | select(.query_ready==false) | {
    document_id,
    status,
    readiness_reason,
    supersedes_document_id
  }'
```

Common `readiness_reason` values:
- `upstream_failed` — LightRAG ingestion failed; reindex may resolve
- `superseded` — document was replaced by a newer version; check `supersedes_document_id`
- `blocked` — blocked by another condition; check other fields
