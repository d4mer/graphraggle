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

## Check Ingestion

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Wait until the file becomes `ingested`.

## Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
```

## Open WebUI

```bash
open http://localhost:3000
```

## Stop

```bash
cd ~/rag-project
podman compose down
```
