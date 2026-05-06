# Operator Cheat Sheet

## Start

```bash
cd ~/rag-project
podman compose up -d
podman compose ps
curl -i http://localhost:8000/health
```

## Stop

```bash
cd ~/rag-project
podman compose down
```

## Rebuild

```bash
cd ~/rag-project
podman compose up -d --build
```

## Upload A File

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file"
```

With company scope:

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file" \
  -F "company=Acme Corp"
```

## Check Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Validation State Quick Ref

| `validation_state` | Meaning | Action |
|--------------------|---------|--------|
| `accept` | passed gate | none |
| `warn` | passed with warnings | check `warnings_json` |
| `reject` | failed gate | check `error_message`, fix and re-upload |
| `auto_split` | too large (>500KB) | split file and re-upload |

## Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?"}'
```

Default behavior:

1. Omitted query mode defaults to `hybrid`.
2. Weak first pass triggers one fallback retrieval in `naive` mode.
3. Transcript-like queries use deeper retrieval settings.

Company-scoped:

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","company":"Acme Corp"}'
```

Transcript query pattern:

1. include speaker name/role
2. include date or timestamp clue
3. include a short exact quote fragment

## Generate Document

```bash
curl -i -X POST http://localhost:8000/generate-document \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Draft a memo summarizing the document","document_type":"memo"}'
```

## The Three Surfaces

| Surface | URL | Use for |
|---------|-----|---------|
| Gateway API | `http://localhost:8000` | Upload, status, query, generate |
| Open WebUI | `http://localhost:3000` | Conversational research |
| LightRAG Web UI | `http://macmini.local:9622` | Admin/debug only |

## Open WebUI

```bash
open http://localhost:3000
```

## LightRAG Web UI (Admin / Debug)

```bash
open http://macmini.local:9622
```

## Logs

```bash
podman logs rag-gateway-api --tail=200
podman logs rag-ingest-worker --tail=200
podman logs lightrag-server --tail=200
podman logs open-webui --tail=200
```

## Find Track IDs

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | grep -o '"track_id":"[^"]*"'
```

## Inspect A Track

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Reindex Failed Documents

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"status":"failed"}'
```

## Force Reindex One Document

```bash
curl -i -X POST http://localhost:8000/ingest/reindex \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"document_id":"YOUR-DOC-ID","force":true}'
```

## Reindex SCT / Workshop GSK Batch

```bash
./scripts/reindex_sct_docs.sh --endpoint http://localhost:8000 --token "$RAG_API_KEY"
./scripts/reindex_sct_docs.sh --endpoint http://localhost:8000 --token "$RAG_API_KEY" --apply
```

## Company Summary

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | jq '.summary.by_company'
```

## Known-Good Values

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
RERANK_MODEL=jina-reranker-v3-mlx
```
