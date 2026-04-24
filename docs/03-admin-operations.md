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
