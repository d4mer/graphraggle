# Open WebUI Guide

## Open The UI

```bash
open http://localhost:3010
```

## First-Time Configuration

Use Open WebUI against the local stack through the gateway's Ollama-compatible routes.

### 1. Sign In Or Create The First Admin User

Open:

```bash
open http://localhost:3010
```

If prompted, create the initial local admin account.

### 2. Open The Admin Panel

In Open WebUI:

1. click your profile icon
2. open `Admin Panel`
3. open `Settings`
4. open `Connections`

### 3. Verify The Ollama Connection

The stack is configured so Open WebUI should already use:

```text
http://gateway-api:8000
```

If the UI requires you to add or confirm it manually, use:

1. Provider type: `Ollama`
2. Base URL: `http://gateway-api:8000`

Do not point Open WebUI directly to `lightrag-server` or the raw oMLX hosts for this workflow.

### 4. Verify The Model Appears

Go to the model selector in Open WebUI and confirm you can see the LightRAG-exposed model.

Expected model behavior:

1. Open WebUI talks to the gateway
2. gateway proxies the Ollama-compatible routes
3. LightRAG handles graph-backed retrieval underneath

### 5. If No Model Appears

Run these checks:

```bash
curl -i http://localhost:8020/api/version
curl -i http://localhost:8020/api/tags
```

If those fail, Open WebUI will not have a working backend model connection.

### 6. Recommended Chat Settings

Use these workflow rules:

1. ingest documents first
2. confirm they are `ingested`
3. then start the research chat
4. ask focused questions first
5. use follow-up questions to refine results

### 7. Recommended Naming Convention

Use thread titles in Open WebUI that map to actual work, for example:

1. `Vendor onboarding research`
2. `Customer policy comparison`
3. `Q2 report drafting`
4. `Contract renewal analysis`

## What It Is For

Use Open WebUI for:

1. conversational research
2. threaded exploration
3. iterative follow-up questions
4. reviewing answers before generating final documents

## Recommended Workflow In Open WebUI

Use Open WebUI for research and exploration, not as the primary file ingestion tool.

Recommended sequence:

1. upload or place files into the stack first
2. confirm ingestion from `gateway-api`
3. open Open WebUI
4. ask retrieval questions
5. use the API for exact operational tasks like upload, ingest status, and document generation

## Best Practice

1. add and ingest documents first
2. confirm they are `ingested`
3. then use Open WebUI to explore the knowledge base

Also:

1. keep Open WebUI focused on research and analysis
2. use `gateway-api` for operational actions
3. verify citations through the API when answer quality matters

## Good Usage Pattern

1. ask a focused question
2. refine with follow-up prompts
3. use the API when you need exact programmatic outputs
4. use `generate-document` when you need a memo/report/summary

Example research prompts:

1. `Summarize the key requirements across the ingested onboarding documents.`
2. `What does the policy say about deployment validation?`
3. `Compare the new document with the previously ingested procedures.`
4. `List the main operational risks mentioned in the uploaded files.`

## Three Surfaces — When To Use Each

The stack exposes three distinct UI/API surfaces. Use each for its intended purpose:

| Surface | URL | Purpose |
|---------|-----|---------|
| Gateway API | `http://localhost:8020` | Uploads, ingest status, query tests, document generation — the operational control plane |
| Open WebUI | `http://localhost:3010` | Conversational research, threaded exploration, iterative follow-up questions |
| LightRAG Web UI | `http://localhost:9621` | Admin and debugging — inspect ingestion state, track status, internal health |

Rules:

1. Always use the gateway API for operational actions (upload, status, generate-document).
2. Use Open WebUI for research and analysis conversations.
3. Use LightRAG Web UI only when you need to debug or inspect the internal state of the graph engine.
4. Do not let LightRAG Web UI become the primary ingestion or chat surface.

## Relationship To The Backend

Open WebUI sits on top of the stack. The real graph/query/indexing work still happens in:

1. `gateway-api`
2. `lightrag-server`
3. `ingest-worker`

Open WebUI should be treated as the conversational shell only.

Use these other paths when needed:

1. `gateway-api` for uploads
2. `gateway-api` for ingest status
3. `gateway-api` for exact query tests
4. `gateway-api` for document generation

## If Open WebUI Looks Fine But Answers Are Weak

Check:

1. the target document is `ingested`
2. API query returns citations
3. LightRAG health is good
4. Open WebUI is pointed at the gateway, not a different backend
5. retry with a transcript-focused prompt using speaker/date/phrase anchors

Gateway retrieval defaults you should expect:

1. Query mode defaults to `hybrid` when omitted.
2. If retrieval is weak, gateway runs one fallback pass in `naive` mode.
3. Transcript-like prompts automatically receive deeper retrieval depth.

Transcript prompt pattern:

1. include speaker name or role
2. include day/time anchor if known
3. include a short exact phrase from the meeting

Commands:

```bash
curl -i http://localhost:8020/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"

curl -i -X POST http://localhost:8020/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What does the document say?","mode":"mix"}'

curl -i http://localhost:8020/api/tags
```

## Daily Use Checklist

Before using Open WebUI each day:

1. start the stack
2. verify `http://localhost:8020/health`
3. verify `http://localhost:8020/api/version`
4. confirm target docs are `ingested`
5. then open `http://localhost:3010`

## Known-Good Configuration For This Stack

1. Open WebUI URL: `http://localhost:3010`
2. Gateway API URL: `http://localhost:8020`
3. Ollama-compatible backend URL for Open WebUI: `http://gateway-api:8000`
4. Do not use direct LightRAG or raw oMLX URLs in Open WebUI for this workflow
