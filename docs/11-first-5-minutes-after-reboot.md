# First 5 Minutes After Reboot

## 1. Start The Stack

```bash
cd ~/rag-project
podman compose up -d
```

## 2. Confirm Containers

```bash
podman compose ps
```

Expected services:

1. `lightrag-server`
2. `rag-gateway-api`
3. `rag-ingest-worker`
4. `open-webui`

## 3. Verify Health

```bash
curl -i http://localhost:8000/health
curl -i http://localhost:8000/api/version
```

## 4. Check Ingestion State

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

If any document is unexpectedly `failed`, inspect its `track_id`.

## 5. Open The UI

```bash
open http://localhost:3000
```

## If Something Is Wrong

Check logs in this order:

```bash
podman logs lightrag-server --tail=200
podman logs rag-gateway-api --tail=200
podman logs rag-ingest-worker --tail=200
podman logs open-webui --tail=200
```

## If Health Fails

Run:

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

## If Ingestion Is Stuck

1. find the `track_id` from ingest status
2. inspect it directly:

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Quick Smoke Query

```bash
curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What documents are currently available?","mode":"mix"}'
```
