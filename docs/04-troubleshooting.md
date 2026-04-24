# Troubleshooting

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

Make sure target docs are `ingested`, not `pending`, `submitted`, or `processing`.
