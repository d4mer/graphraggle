# RAG Stack Docs

This folder contains the operator documentation for the Mac mini RAG stack.

## Stack Summary

The stack consists of:

1. `lightrag-server`
2. `gateway-api`
3. `ingest-worker`
4. `open-webui`

Main URLs:

1. API docs: `http://localhost:8000/docs`
2. Open WebUI: `http://localhost:3000`

## Document Map

1. `01-quickstart.md`: shortest daily path
2. `02-daily-workflow.md`: full step-by-step daily workflow
3. `03-admin-operations.md`: restart, rebuild, logs, health, inspection
4. `04-troubleshooting.md`: common failures and exact fixes
5. `05-known-good-config.md`: validated settings and behavior
6. `06-api-reference.md`: curl cookbook
7. `07-openwebui-guide.md`: Open WebUI usage guide
8. `08-change-log.md`: important implementation fixes discovered during setup
9. `09-operator-cheat-sheet.md`: one-page quick reference
10. `10-printable-handbook.md`: consolidated operator handbook
11. `11-first-5-minutes-after-reboot.md`: reboot recovery checklist

## Key Paths

1. Project root: `~/rag-project`
2. Source docs: `~/rag-project/source_docs`
3. Uploads: `~/rag-project/uploads`
4. Logs: `~/rag-project/logs`
5. State DB: `~/rag-project/state/ingest.db`

## Key Rules

1. Uploads stay in `uploads/`
2. Text files ingest via LightRAG `POST /documents/text`
3. Binary docs ingest via LightRAG `POST /documents/upload`
4. Ignore `source_docs/__enqueued__`
5. Use `EMBEDDING_MODEL=mxbai-embed-large-v1`
