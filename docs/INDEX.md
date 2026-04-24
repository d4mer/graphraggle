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

1. LightRAG image tag: `ghcr.io/hkuds/lightrag:v1.4.15`
2. Embedding model: `mxbai-embed-large-v1`
3. Reranker model: `jina-reranker-v3-mlx`
4. LLM host: `192.168.1.190:1234/v1`
5. Embedding/rerank host: `192.168.1.180:1234`
6. Text files should ingest through LightRAG `POST /documents/text`
7. Uploads should remain in `uploads/`
8. Ignore `source_docs/__enqueued__`
