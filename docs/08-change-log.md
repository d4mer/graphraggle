# Change Log

## Important Fixes Discovered During Setup

1. Correct LightRAG image tag is:

```yaml
ghcr.io/hkuds/lightrag:v1.4.15
```

2. Correct embedding model is:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1
```

3. Text files must ingest via LightRAG `POST /documents/text`

4. Binary/office files should continue using `POST /documents/upload`

5. Ignore `source_docs/__enqueued__` in the worker

6. Uploads should remain in `uploads/`

7. `gateway-api` and `ingest-worker` must use local build config and `pull_policy: never`

8. LightRAG container command must pass flags only, not `lightrag-server` again

9. Gateway-to-LightRAG networking was validated internally using:

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

10. Final smoke test validated full retrieval and citations from `rag-smoke.txt`

## Packet-24 — OpenWebUI task prompts are capped before forwarding

1. `BRIDGE_TASK_MAX_CHARS` (default `0` = off; set e.g. `24000` to enable) bounds the `### Task:` prompt the bridge forwards to LightRAG `mode=bypass`, where no retrieval truncation runs. When enabled the bridge keeps the first 25% of the budget (the task header) and the last 75% (the most recent chat turns), joined by `[...truncated...]`. The bridge log line always gains `prompt_chars`, plus `forwarded_chars` on task requests — lengths only, never prompt text, and they are on whether or not the cap is.
