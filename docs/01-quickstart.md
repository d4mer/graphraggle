# Quickstart

## Start

```bash
cd ~/rag-project
podman compose up -d
```

## Verify

```bash
cd ~/rag-project
podman compose ps
curl -i http://localhost:8000/health
```

## Upload A File

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file"
```

Add a company scope (optional but recommended for multi-company deployments):

```bash
curl -i -X POST http://localhost:8000/upload \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -F "file=@/path/to/file" \
  -F "company=Acme Corp"
```

## Validation Gate

Before ingestion, every file passes a validation gate:

| File size | Gate behavior |
|-----------|---------------|
| < 100 KB | accepted |
| 100 KB – 500 KB | accepted with warning |
| > 500 KB | auto_split — file is rejected and must be split before re-upload |

Encoding is auto-detected with fallback: UTF-8 → UTF-8-sig → CP1252 → Latin-1.

Duplicate detection uses SHA-256 content hashing — re-uploading the same file content is rejected.

## Check Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

A document is query-ready when `status` is `ingested` **and** `query_ready` is `true`.

Watch for `validation_state` to confirm the gate verdict:

- `accept` — passed validation
- `warn` — passed but has warnings (check `warnings_json`)
- `reject` — failed validation (check `error_message`)
- `auto_split` — oversized; split the file and re-upload

## Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
```

Scope to a company:

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","company":"Acme Corp","mode":"mix"}'
```

## The Three Surfaces

| Surface | URL | Use for |
|---------|-----|---------|
| Gateway API | `http://localhost:8000` | Upload, ingest status, query, generate — the control plane |
| Open WebUI | `http://localhost:3000` | Conversational research, threaded exploration |
| LightRAG Web UI | `http://macmini.local:9622` | Admin/debug only — inspect graph state and ingestion failures |

## Stop

```bash
cd ~/rag-project
podman compose down
```
