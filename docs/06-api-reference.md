# API Reference

## Auth

Gateway uses:

```http
Authorization: Bearer <RAG_API_KEY>
```

## Health

```bash
curl -i http://localhost:8000/health
```

## Upload

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file"
```

Packet 07 additions:

1. Optional multipart field `company` still stays supported.
2. Upload responses now include `company_source` and `company_source_detail`.
3. Uploads without `company` remain allowed and persist as `company=null`, `company_source="unscoped"`.

Important:

1. If the file path contains spaces, keep the whole `-F` argument quoted
2. Do not escape spaces with `\` inside that quoted string

Correct example:

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/Users/imac/Desktop/GSK/Logistics/ArneNotes16-04.docx"
```

## Batch Upload Through The API

Use the helper script:

```bash
cd /Users/imac/Documents/Programming/graphrag-implementation
export RAG_API_KEY='your-gateway-key'
./scripts/upload_batch.sh "/Users/imac/Desktop/GSK/Logistics"
```

Or use a shell loop directly:

```bash
find "/Users/imac/Desktop/GSK/Logistics" -type f \( \
  -iname '*.txt' -o \
  -iname '*.md' -o \
  -iname '*.html' -o \
  -iname '*.htm' -o \
  -iname '*.json' -o \
  -iname '*.csv' -o \
  -iname '*.pdf' -o \
  -iname '*.docx' -o \
  -iname '*.pptx' -o \
  -iname '*.xlsx' \
\) -print0 | while IFS= read -r -d '' file; do
  curl -i -X POST http://localhost:8000/upload \
    -H "Authorization: Bearer $RAG_API_KEY" \
    -F "file=@${file}"
done
```

## Batch Ingestion Through `source_docs`

If you prefer filesystem-based ingestion instead of API uploads:

```bash
cp -R "/Users/imac/Desktop/GSK/Logistics" ~/rag-project/source_docs/
```

The worker scans `source_docs/` recursively.

Packet 07 filesystem company inference rule:

1. The first directory under `source_docs/` becomes the document `company`.
2. Example: `source_docs/GSK/Logistics/notes.txt` persists as `company="GSK"`, `company_source="path_inferred"`.
3. A file placed directly under `source_docs/` remains unscoped with `company=null`, `company_source="unscoped"`.

Then monitor:

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Ingest Status

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Packet 06 additions:

1. Each document now exposes `query_ready` and `readiness_reason`
2. Status summary now includes `by_readiness_reason`

Packet 07 additions:

1. Each document now exposes `company_source` and `company_source_detail`.
2. Status summary now includes `by_company_source`.
3. Filesystem rows infer `company` from the first directory under `source_docs/`.

Example not-ready shape:

```json
{
  "document_id": "...",
  "status": "failed",
  "validation_state": "accept",
  "query_ready": false,
  "readiness_reason": "upstream_failed",
  "error_stage": "submit",
  "error_code": "submit_transient_failed"
}
```

## Reindex

Reindex now marks documents for worker-handled resubmission. It does not bypass validation or submit directly to LightRAG.

Reindex one document by path:

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"path":"/app/uploads/YOUR-FILE.txt"}'
```

Reindex failed documents by status:

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status":"failed"}'
```

Forced reindex for blocked or already-ingested documents:

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_id":"YOUR-DOC-ID","force":true}'
```

Supported reindex selectors:

1. `document_id`
2. `path`
3. `status`
4. `validation_state`
5. `company`
6. `source_type`

The response distinguishes `queued_documents` from `blocked_documents` and records whether `force` was used.

## Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
```

Packet 07 scoping behavior:

1. `company` is optional on `/query`.
2. If `company` is provided, the gateway only returns citations whose persisted `company` exactly matches that value.
3. Unscoped documents are excluded from company-scoped query responses.
4. If `company` is omitted, the gateway returns all upstream citations, including unscoped documents.
5. The response now includes `query_scope`, plus company provenance fields on each returned citation.
6. This is explicit gateway-side scoping, not a claim of hard multi-tenant isolation inside LightRAG.

Company-scoped example:

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What changed in the logistics plan?","company":"Acme QA","mode":"mix"}'
```

## Generate Document

```bash
curl -i -X POST http://localhost:8000/generate-document \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Draft a memo summarizing this document","document_type":"memo"}'
```

## Internal LightRAG Track Status

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Internal LightRAG Version

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/api/version
```

## Example Smoke-Test Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the smoke test document say about deployment?","mode":"mix"}'
```
