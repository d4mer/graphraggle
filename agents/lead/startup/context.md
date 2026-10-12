# Handoff - lead

Last updated: 2026-10-12 (after production wipe + model switch)

## Goal
Run GraphRAG (rc-1) in production for first use; next feature = pptx support. Keep `master` green on GitHub.
Lead delegates all code to dev-worker@graphraggle and prod ops to Hermes (ops-hermes@thinkpad-ops via
`rig send --host thinkpad ...`); lead verifies and pushes.

## State (2026-10-12)
- rc-1 is live on the Thinkpad (~/rag-project); Gate 4 = GO (docs/ops/gate4-go-no-go.md).
- Operator discontinued the GSK project: ALL production data WIPED (LightRAG store, inputs, source_docs,
  uploads, worker state DB, logs, old backups dir). Verified: LightRAG healthy, 0 docs; worker up with fresh
  state/ingest.db. chromadb belongs to another stack (memory-stack volume), untouched.
- Full backup of everything (incl. source files, old backups, .env/compose) on deltahome:
  192.168.1.180:~/backups/graphraggle-20261012 (9 tgz + SHA256SUMS, verified). None kept on the Thinkpad.
  Rollback commands in the Gate 4 doc that point at Thinkpad backups/ are obsolete; restore from deltahome.
- LightRAG ingestion LLM switched to sigma Strata qwen3.8-flash (http://192.168.1.195:1234/v1,
  header `Authorization: Bearer 1234`; reachable from Thinkpad over LAN only). Embeddings still oMLX
  192.168.1.190 (mxbai-embed-large-v1). Env change needs `docker compose up -d --no-deps lightrag-server`
  (recreate), not `docker start`. Canary: 1 chunk in 164 s, 31 entities/45 relations, no parse errors.
- packet-27 (pptx extraction, master 1f90c0e) merged but NOT deployed: needs image rebuild with python-pptx.
- Understory note /projects/graphraggle-status-2026-10-12.md and Plane GRAG-43..50 written/updated
  (understory MCP not loaded in session: call http://192.168.1.185:3800/mcp by curl JSON-RPC, one request at a
  time, long timeout; Plane via REST at 192.168.1.185:8088 workspace "agents", run from macbookm1.local
  with ~/plane-selfhost/plane-mcp/agent-tokens.env sourced; project GRAG).

## Hermes approval handling
Approval dialogs appear only in Hermes tmux pane (`ssh 100.64.0.6 tmux capture-pane -p -t ops-hermes@thinkpad-ops`).
Expand "Show full command", accept only commands in the brief; Hermes' terminal masks the key 1234 as `***`.

## Open
- Data is empty: no corpus yet. Operator decides what to ingest next. Before the first real ingest:
  deploy packet-27 (rebuild image, test real decks), consider fixing entity quality (speaker labels,
  duplicate names; GRAG-50). Sigma Strata is shared with dev-worker: ingestion competes with agent sessions.
- Probe-set-v1 was built on GSK docs, so the Gate 2 baseline must be rebuilt on the new corpus.
- Rotate OpenWebUI key; real .docx end-to-end check on new data; OpenWebUI chat history still holds GSK chats.
- Leftover GSK-quoting files remain on Thinkpad (probe/, docs/ops/runs/*) - operator decision pending.
- Optional: oMLX memory policy (GRAG-42), sigma->Thinkpad tailscale gap, packet-22-ll-seed branch (parked).

## Next three actions
1. Ask operator what corpus comes next; run a pptx real-deck dry run with Hermes, then deploy packet-27.
2. Tune ingestion on sigma (LLM_MODEL_MAX_ASYNC, timeouts) on a small first batch; measure time per doc.
3. Rebuild the probe set and quality baseline on the new corpus; start hypercare (Gate 5).
