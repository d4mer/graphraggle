# Open WebUI Guide

## Open The UI

```bash
open http://localhost:3000
```

## What It Is For

Use Open WebUI for:

1. conversational research
2. threaded exploration
3. iterative follow-up questions
4. reviewing answers before generating final documents

## Best Practice

1. add and ingest documents first
2. confirm they are `ingested`
3. then use Open WebUI to explore the knowledge base

## Good Usage Pattern

1. ask a focused question
2. refine with follow-up prompts
3. use the API when you need exact programmatic outputs
4. use `generate-document` when you need a memo/report/summary

## Relationship To The Backend

Open WebUI sits on top of the stack. The real graph/query/indexing work still happens in:

1. `gateway-api`
2. `lightrag-server`
3. `ingest-worker`

## If Open WebUI Looks Fine But Answers Are Weak

Check:

1. the target document is `ingested`
2. API query returns citations
3. LightRAG health is good

Commands:

```bash
curl -i http://localhost:8000/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"

curl -i -X POST http://localhost:8000/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'
```
