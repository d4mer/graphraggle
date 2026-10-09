# First 5 Minutes After Reboot

Production host: the **Thinkpad**, project dir `~/rag-project`. The runtime is
**Docker** (`docker compose`). Verified in a reboot drill on 2026-10-09.

## 0. What Should Happen By Itself

Docker starts at boot and every RAG container has a `unless-stopped`/`always`
restart policy, so the stack normally comes back on its own about a minute after
boot with **0 restarts**. In the 2026-10-09 drill every container on the host came
back unattended. If that is what you see, skip to step 3 to confirm health and then
do step 5 — the agent seat is the one thing that does **not** come back.

## 1. Start The Stack (only if it did not come back)

```bash
cd ~/rag-project
docker compose up -d
```

## 2. Confirm Containers

```bash
docker compose ps
```

Expected RAG services:

1. `lightrag-server`
2. `rag-gateway-api`
3. `rag-ingest-worker`
4. `open-webui`
5. `rag-chromadb`

The host also runs non-RAG containers (Plane and others). The exact total count is
host-specific, so do not chase a number — check that **all `rag-*` containers plus
`open-webui`** are up.

## 3. Verify Health

Gateway on host port **8020** (container port 8000):

```bash
curl -i http://localhost:8020/health
curl -i http://localhost:8020/api/version
```

Do **not** use LightRAG's `pipeline_status` endpoint as a health check — it hangs.

## 4. Check Ingestion State

```bash
curl -i http://localhost:8020/ingest/status \
  -H "Authorization: Bearer $RAG_API_KEY"
```

If any document is unexpectedly `failed`, inspect its `track_id` (see "If Ingestion
Is Stuck" below). Note that `rag-ingest-worker` may be running an older image than
the gateway — see `05-known-good-config.md`.

## 5. Restore The Agent Seat (Hermes)

A reboot does **not** restore the Hermes agent seat. Relaunch the rig node, then
start the seat inside its tmux pane:

```bash
cd ~/rag-project
hermes chat -c
```

## 6. Open The UI

Open WebUI is on host port **3010** (container port 8080):

```bash
open http://localhost:3010
```

## If Something Is Wrong

Check logs in this order:

```bash
docker logs lightrag-server --tail=200
docker logs rag-gateway-api --tail=200
docker logs rag-ingest-worker --tail=200
docker logs open-webui --tail=200
```

## If Health Fails

Run the internal check from inside the gateway container (LightRAG listens on
9621 inside the network, and is published on host port **9621**):

```bash
docker exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

## If Ingestion Is Stuck

1. find the `track_id` from ingest status
2. inspect it directly:

```bash
docker exec rag-gateway-api curl -i \
  -H "X-API-Key: $LIGHTRAG_INTERNAL_API_KEY" \
  http://lightrag-server:9621/documents/track_status/YOUR_TRACK_ID
```

## Quick Smoke Query

```bash
curl -i -X POST http://localhost:8020/query \
  -H "Authorization: Bearer $RAG_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"What documents are currently available?","mode":"mix"}'
```

## Backups

Backups live in `~/rag-project/backups/` and were last verified 2026-10-09: SQLite
online-backup API for `state/ingest.db`, a quiescent copy of `lightrag_store/`, and
a sha256 manifest. A restore into a scratch dir verified 0 manifest mismatches, 272
documents and 12 equal store files.
