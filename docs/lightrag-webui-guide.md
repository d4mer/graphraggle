# LightRAG Web UI Guide

## Access

```bash
open http://macmini.local:9622
```

Or from another machine on the trusted LAN:

```bash
open http://<macmini-ip>:9622
```

## What It Is

The LightRAG Web UI is the native LightRAG debugging and inspection surface. It lets you browse the internal state of the graph engine without constructing curl commands.

## Intended Use

Use the LightRAG Web UI for:

1. **Browsing ingestion state** — see which documents are indexed, queued, or failed inside LightRAG
2. **Inspecting track status** — follow individual document processing tracks visually
3. **Debugging ingestion failures** — check internal LightRAG error messages before they propagate to the gateway
4. **Verifying graph store and input directory** — confirm volumes are mounted correctly
5. **Manual query testing** — run ad-hoc queries directly against LightRAG to verify index behavior

## What Not To Do Here

1. **Do not use for primary document ingestion** — use the gateway API (`POST /upload`) or filesystem placement in `source_docs/`
2. **Do not use for conversational research** — use Open WebUI (`http://macmini.local:3000`)
3. **Do not use for document generation** — use the gateway API (`POST /generate-document`)
4. **Do not let it become the primary chat surface** — it has no conversation threading, no citation formatting, and no session history

## Relationship To The Gateway API

The gateway API (`http://localhost:8000`) is the control plane. It handles:

- Upload and validation gate
- Document lifecycle tracking
- Company scoping
- Error classification

The LightRAG Web UI talks directly to the `lightrag-server` internal API (`http://lightrag-server:9621`). It shows the raw internal state, which may include documents not yet tracked by the gateway or in intermediate states.

Use the Web UI to diagnose; use the gateway API to operate.

## Security Note

- The LightRAG Web UI is accessible to any machine on the trusted LAN without separate authentication
- Internal API paths still require `X-API-Key: $LIGHTRAG_API_KEY` except for `/health`
- If the LAN is ever untrusted, add a reverse-proxy auth layer before relying on this surface

## When To Use This vs Open WebUI vs Gateway API

| Need | Use |
|------|-----|
| Upload a document | Gateway API (`curl .../upload`) |
| Check if a document is ingested | Gateway API (`.../ingest/status`) |
| Conversational research with citations | Open WebUI (`http://macmini.local:3000`) |
| Debug why a document failed inside LightRAG | LightRAG Web UI (`http://macmini.local:9622`) |
| Inspect track status visually | LightRAG Web UI |
| Generate a grounded memo | Gateway API (`.../generate-document`) |
| Check company attribution | Gateway API (`.../ingest/status` — look for `company`, `company_source`) |
| Reindex a failed document | Gateway API (`POST /ingest/reindex`) |

## Internal Health Check

From inside the container:

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

## Internal Version Check

```bash
podman exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_API_KEY" \
  http://lightrag-server:9621/api/version
```
