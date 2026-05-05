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
Model 'mxbai-embed-large' not found
```

Correct value:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
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
