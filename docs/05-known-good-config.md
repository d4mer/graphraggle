# Known-Good Config

## Validated Architecture

1. `lightrag-server` is the internal graph/index/query engine
2. `gateway-api` is the external API surface
3. `ingest-worker` handles ingestion orchestration
4. `open-webui` is the conversational frontend

## Validated URLs

1. API docs: `http://localhost:8000/docs`
2. Open WebUI: `http://localhost:3000`
3. LightRAG Web UI (admin/debug): `http://localhost:9622`

## Validated Models

LLM:

```bash
LLM_MODEL=qwen3.6-35b
LLM_BINDING_HOST=http://192.168.1.190:1234/v1
```

Embeddings:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
EMBEDDING_BINDING_HOST=http://192.168.1.180:1234/v1
```

Reranker:

```bash
RERANK_MODEL=jina-reranker-v3-mlx
RERANK_BINDING_HOST=http://192.168.1.180:1234/v1/rerank
```

## Validated LightRAG Image

```yaml
image: ghcr.io/hkuds/lightrag:v1.4.15
```

## Validated Worker Rules

1. uploads stay in `uploads/`
2. `source_docs/__enqueued__` is ignored
3. text-like files ingest through `/documents/text`
4. binary docs ingest through `/documents/upload`

## Text Extensions Sent Through `/documents/text`

1. `.txt`
2. `.md`
3. `.html`
4. `.htm`
5. `.json`
6. `.csv`

## Binary/Office Extensions Sent Through `/documents/upload`

1. `.pdf`
2. `.docx`
3. `.pptx`
4. `.xlsx`

## Validated Smoke-Test Behavior

The system successfully:

1. uploaded `rag-smoke.txt`
2. ingested the full file body
3. retrieved grounded content
4. returned citations containing the full text
