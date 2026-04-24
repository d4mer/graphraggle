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

## Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
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
