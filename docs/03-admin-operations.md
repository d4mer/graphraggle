# Admin Operations

## Start

```bash
cd ~/rag-project
podman compose up -d
```

## Rebuild

```bash
cd ~/rag-project
podman compose up -d --build
```

## Stop

```bash
cd ~/rag-project
podman compose down
```

## Service Status

```bash
cd ~/rag-project
podman compose ps
```

## Gateway Health

```bash
curl -i http://localhost:8000/health
```

## Logs

Gateway:

```bash
podman logs rag-gateway-api --tail=200
```

Worker:

```bash
podman logs rag-ingest-worker --tail=200
```

LightRAG:

```bash
podman logs lightrag-server --tail=200
```

Open WebUI:

```bash
podman logs open-webui --tail=200
```

## Check Ingest Status

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

## Query Readiness Check

```bash
curl -s http://localhost:8000/documents \
  -H "Authorization: Bearer $RAG_API_KEY"
```

Inspect `query_ready` and `readiness_reason` for operator-facing searchability state.

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

Use `force=true` only when intentionally replacing an in-flight, rejected, split-required, or already-ingested row through the normal worker path.

## Reindex SCT / Workshop GSK Files

Dry-run selection (default behavior):

```bash
./scripts/reindex_sct_docs.sh \
  --endpoint http://localhost:8000 \
  --token "$RAG_API_KEY"
```

Apply reindex calls:

```bash
./scripts/reindex_sct_docs.sh \
  --endpoint http://localhost:8000 \
  --token "$RAG_API_KEY" \
  --apply
```

Optional flags:

1. `--company` defaults to `GSK` and controls `source_docs/<company>` path matching.
2. `--force` forwards `force=true` into each `/ingest/reindex` request.
3. Selector matches filenames/paths containing `SCT`, `Workshop`, `Apr 30`, or `May`.

## Query Defaults And Transcript Guidance

1. Gateway defaults `/query` mode to `hybrid` when mode is omitted.
2. If first retrieval is weak, gateway performs a bounded one-time fallback to `naive`.
3. For transcripts, queries work best when they include a speaker name, time/date clue, and 3-8 exact words from the target passage.

## Find Track IDs

```bash
curl -s http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY" | grep -o '"track_id":"[^"]*"'
```

## Inspect LightRAG Track Status

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Direct Internal LightRAG Health

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

## Direct Internal LightRAG Ollama Version

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/api/version
```

## LightRAG Web UI (Admin / Debug Surface)

The native LightRAG Web UI is intentionally exposed on a dedicated port for admin and debugging tasks.

### Access

```bash
open http://macmini.local:9622
```

Or from another machine on the trusted LAN:

```bash
open http://<macmini-ip>:9622
```

### Intended Use

1. Browse ingestion state visually (documents, tracks, processing status)
2. Inspect track status and internal LightRAG health
3. Debug ingestion failures without constructing curl commands
4. Verify that the graph store and input directory are mounted correctly

### What Not To Do Here

1. Do not use the LightRAG Web UI for primary document ingestion — use the gateway API (`POST /upload`) or filesystem placement in `source_docs/`.
2. Do not use it for conversational research — use Open WebUI (`http://macmini.local:3000`).
3. Do not use it for document generation — use the gateway API (`POST /generate-document`).

### Security Note

- The LightRAG Web UI is accessible to any machine on the trusted LAN.
- No separate login is configured for first rollout; security relies on the LAN boundary.
- Internal API paths still require `X-API-Key: $LIGHTRAG_API_KEY` except for `/health`.
- If the LAN is ever untrusted, add a reverse-proxy auth layer before relying on this surface.
