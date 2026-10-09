# Docs Index

Use this file as the main navigation page for the GraphRAG documentation set.

## Start Here

1. `README.md`: documentation bundle overview
2. `01-quickstart.md`: shortest operator path
3. `11-first-5-minutes-after-reboot.md`: reboot recovery checklist

## Daily Use

1. `02-daily-workflow.md`
2. `07-openwebui-guide.md`
3. `09-operator-cheat-sheet.md`
4. `12-large-pdf-ingestion.md`

## Operations

1. `03-admin-operations.md`
2. `06-api-reference.md`
3. `10-printable-handbook.md`

## Troubleshooting And History

1. `04-troubleshooting.md`
2. `05-known-good-config.md`
3. `08-change-log.md`

## Validated Facts

Verified on the production Thinkpad (`~/rag-project`) on 2026-10-09; details in
`05-known-good-config.md`.

1. Runtime: **Docker** (`docker compose`) — not podman
2. LightRAG image tag: `ghcr.io/hkuds/lightrag:v1.5.7` (the repo `compose.yml` still pins `v1.4.15`)
3. Embedding model: `mxbai-embed-large-v1` (dimension 1024)
4. Reranker model: `jina-reranker-v3-mlx`
5. LLM, embeddings and reranker host: `192.168.1.190:1234` — one oMLX server for all three
6. Host ports: gateway `8020`, Open WebUI `3010`, LightRAG `9621`
7. Gateway health: `GET http://localhost:8020/health` — never use LightRAG `pipeline_status`, it hangs
8. `rag-ingest-worker` runs an old pre-packet-18 image; the worker dedupe hardening (`2f74255`) is in the repo but not deployed
9. Text files should ingest through LightRAG `POST /documents/text`
10. Uploads should remain in `uploads/`
11. Ignore `source_docs/__enqueued__`
