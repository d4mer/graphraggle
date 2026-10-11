# Packet 26 — reconcile repo compose.yml with production (+ guard test)

Work in your own worktree: `git fetch origin && git worktree add ../graphraggle-p26 -b packet-26-impl origin/packet-26-compose-reconcile`. Push `packet-26-impl` when done. Do NOT switch the shared checkout.
Tests: `RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q` (467 pass on master).

## Why
Repo `compose.yml` drifted from production on the Thinkpad (`~/rag-project/compose.yml`). Production was fixed on 2026-10-11 so that
Office/PDF files ingest on LightRAG v1.5.7. Root cause (proven): LightRAG's upload endpoint writes to the `--input-dir` CLI arg,
but its parser finds the file via the `INPUT_DIR` ENV var (image default `/app/data/inputs`). They were different, and the input dir
also overlapped the folder the worker scans, so uploads 409'd. Fix = LightRAG gets its OWN input dir, with `--input-dir` and `INPUT_DIR` equal.
Canary .docx and .pdf both ingested after the change. The repo must carry this so the next deploy does not undo it.

## Change `compose.yml` to match production exactly, for these items only
lightrag-server:
- `image: ghcr.io/hkuds/lightrag:v1.5.7` (was v1.4.15)
- `--input-dir` value `/data/lightrag_inputs` (was /data/source_docs)
- add volume `./lightrag_inputs:/data/lightrag_inputs` (KEEP `./source_docs:/data/source_docs`)
- add `environment:` with `INPUT_DIR: /data/lightrag_inputs` (comment: must equal --input-dir; parser reads the env var, upload writes to the CLI arg)
- ports `"9621:9621"` (was 9622:9621)
gateway-api: ports `"8020:8000"` (was 8000:8000).
open-webui: image `ghcr.io/open-webui/open-webui:0.11.4` (was :main), ports `"3010:8080"` (was 3000:8080).
  Production also sets OPENAI_API_BASE_URLS / OPENAI_API_KEYS / OPENCLAW_BRIDGE_STATE_DIR / extra_hosts for the HyperQwen connection.
  Add the NON-secret ones (OPENAI_API_BASE_URLS value `'http://192.168.1.195:1234/v1;http://192.168.1.195:18020/v1'`, OPENCLAW_BRIDGE_STATE_DIR `'/app/backend/data/openclaw-bridge'`, extra_hosts `host.docker.internal:host-gateway`).
  **SECRET RULE:** `OPENAI_API_KEYS` carries an API key. NEVER put a key value in the repo. Use `OPENAI_API_KEYS: ${OPENWEBUI_OPENAI_API_KEYS:-}` and document the variable in a comment and in docs.
  If you are unsure whether a value is secret, parameterise it.
Do not change anything else (the gateway/worker/build sections otherwise already match).

## Also
1. `.gitignore`: add `lightrag_inputs/` (runtime dir, like source_docs).
2. Guard test `tests/test_compose_config.py`: parse `compose.yml` with PyYAML (use whatever YAML lib is already in the venv; if none, a minimal text parse is fine, but do not add a new dependency without saying so) and assert: (a) the lightrag-server `--input-dir` value equals `environment.INPUT_DIR`; (b) that path is NOT the same as the worker's scanned source dir (`/data/source_docs`) and is mounted by a volume; (c) the lightrag image tag is pinned (not `latest`/`main`); (d) no literal `sk-`-style or long token-looking value under `OPENAI_API_KEYS`/any `*KEY*` env key (must be a `${...}` reference or empty).
3. Docs: in `docs/05-known-good-config.md` and `docs/08-change-log.md` add the input-dir/INPUT_DIR finding (2026-10-11) with the failure signature: 409 "Input directory already contains a file with the same canonical basename" and "Parse worker failed (native): legacy source file not found". In `docs/11-first-5-minutes-after-reboot.md` / INDEX only if they reference the old port or input dir; fix those references. State plainly that `lightrag_inputs/` must exist on the host and be world-writable (777) before first start, and that removing/recreating the host dir requires restarting `lightrag-server` (the bind mount goes stale).
4. Do not edit app/ code. Do not touch the `.env` template beyond documenting `OPENWEBUI_OPENAI_API_KEYS` if an `.env.example` exists.

## Acceptance
- `git diff origin/packet-26-compose-reconcile -- compose.yml` shows only the items above. Show it.
- `docker compose config --quiet` is not possible here (no docker); instead show the YAML parses and the new test passes.
- Full pytest output: previous count + new tests, all pass.
- Grep the diff for anything key-like and show the grep returns nothing.

## Report back
Files changed, test count, the compose diff, and anything in prod you could not match from this brief.
