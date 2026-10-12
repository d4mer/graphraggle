# Brief - WIPE production GraphRAG data (operator GO 2026-10-12)

Operator decision: GSK project discontinued, wipe all data. Verified backup of everything is on deltahome
(~/backups/graphraggle-20261012, 9 archives + SHA256SUMS). Work in ~/rag-project. Do exactly these steps,
in order, and stop at the first surprise.

## 0. Gate (do not delete anything if this fails)
`ssh -o BatchMode=yes maverick@192.168.1.180 'cd ~/backups/graphraggle-20261012 && sha256sum -c SHA256SUMS'`
must print OK for all 9 archives. If not, stop and report.

## 1. Stop services that use the data
`docker stop lightrag-server`  (rag-ingest-worker is already stopped; leave gateway-api, open-webui, chromadb running).

## 2. Empty the data dirs - DELETE CONTENTS ONLY, NEVER rmdir/rm the directories themselves
(Removing a bind-mounted directory breaks the mount; this bit us before.) For each of
lightrag_store, lightrag_inputs, source_docs, uploads, state, logs, backups:
`find ~/rag-project/<dir> -mindepth 1 -delete`
then `find ~/rag-project/<dir> | wc -l` must print 1 (the dir itself). Keep the directories and their
ownership/permissions as they are (lightrag_inputs must stay writable by the container: do not chmod/chown).
Do NOT touch anything else in ~/rag-project (app/, probe/, docs/, .env, compose*.yml, etc.) or open-webui data.

## 3. chromadb: READ ONLY
Do not delete anything. Report only: where its data lives (volume/dir), its size, and the collection names
with document counts (use the chromadb HTTP API or `docker exec` read-only), so the operator can decide.

## 4. Restart clean
`docker compose up -d --no-deps lightrag-server` (recreate is the proven way to start it), wait for /health.
Verify with the LightRAG API key from .env (never print it): GET /documents shows 0 docs, GET /health ok,
`docker exec lightrag-server ls /data/lightrag_inputs` works (mount alive), env still shows
LLM_MODEL=qwen3.8-flash.
Then `docker start rag-ingest-worker`, wait 60 s, confirm it is Up, logs show a scan with 0 candidates and no
errors, and the new state DB exists (ls -l state/). If the worker crash-loops because state/ is empty,
stop it and report the exact error - do not hand-create files.

## 5. Report
Short heredoc report (not write_file) to ~/rag-project/docs/ops/runs/wipe-report.md: backup gate result,
`find|wc -l` per dir, chromadb inventory, LightRAG doc count, container states, any errors.
Constraints: no `rm -r` on directories, no rmdir, no chmod/chown, nothing outside the listed dirs. Approval
dialogs may appear for the find -delete and docker commands above; any other command, do not accept - report.
