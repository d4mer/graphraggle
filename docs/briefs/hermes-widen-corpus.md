# Brief - deploy the pptx extractor (packet-27) and ingest the full Solvay corpus (Thinkpad ~/rag-project)

Operator go 2026-10-13 ("widen the corpus"). Accuracy target already met on 6 transcripts; this widens to all staged files. Nothing about corpus content may be pasted back to the lead (counts, sizes, file names, timings, statuses only).

## Part 1 - deploy the worker code (rollback first)
Source: ~/p27src.tgz = `app/` + `requirements.txt` from master (adds app/pptx_extract.py, python-pptx==1.0.2, worker .pptx path). It was packaged only from those two paths.
1. Preflight: LightRAG /health ok, `/documents/status_counts` processed==6 failed==0, `/documents/pipeline_status` not busy. Record `docker images` IDs for local/rag-gateway:latest.
2. Rollback anchors: `docker tag local/rag-gateway:latest local/rag-gateway:pre-packet-27`; `cp -a app app.pre-packet-27`; `cp requirements.txt requirements.txt.pre-packet-27`.
3. Extract: `mkdir /tmp/p27 && tar xzf ~/p27src.tgz -C /tmp/p27`; show me (counts only) which files differ from ~/rag-project/app (`diff -rq /tmp/p27/app ~/rag-project/app`, ignoring *.pre-deploy-*, __pycache__): file NAMES only. If any file other than app/pptx_extract.py and the worker/ingest module(s) differs, STOP and report the names (the gateway code may have drifted from master).
4. If only expected files differ: copy the new app/ and requirements.txt into ~/rag-project, `docker compose build ingest-worker` (image local/rag-gateway:latest; tag the result also as local/rag-gateway:p27), verify `docker run --rm --entrypoint python local/rag-gateway:latest -c "import pptx;print(pptx.__version__)"` prints 1.0.2, then recreate ONLY the worker (`docker compose up -d --no-deps ingest-worker`). Do NOT recreate gateway-api in this task. Do not start ingestion until Part 2.
Rollback if anything fails: `docker tag local/rag-gateway:pre-packet-27 local/rag-gateway:latest; docker compose -f compose.yml -f compose.rollback-worker.yml ...` per docs/ops/rollback-and-containment.md; report.

## Part 2 - snapshot, then ingest
1. Snapshot the current store to deltahome only: `tar czf - lightrag_store state | ssh -o BatchMode=yes maverick@192.168.1.180 'cat > ~/backups/graphraggle-20261012/store-pre-widen-20261013.tgz'` and verify size > 0 there. Nothing kept on the Thinkpad.
2. Ensure the worker is running (`docker start` is not needed after compose up; confirm `docker ps`). Place ALL files from ~/rag-project/staging/Solvay into the worker's source dir exactly as the earlier transcript ingestion did (skip `._*` macOS junk; the 6 transcripts already in the store should dedupe or hit the LLM cache). Expected final set: 8 transcripts, 1 pdf, 1 docx, 1 pptx.
3. Watch via `/documents/status_counts` and `/documents/track_status/<id>` (counts only). Keep MAX_ASYNC=1; sigma is shared, so tell the lead if ingestion looks stalled >30 min with no progress. Do not touch any other config.
4. When pipeline idle: report per file: status, chunks_count, duration, failures; totals processed/failed; node and edge counts from the graph; entity-count for docx/pdf/pptx files (counts only). Specifically confirm the pptx went through the new extractor path (worker log line / tracker source, no content) and that no file landed in `failed`.
5. Write ~/rag-project/docs/ops/runs/widen-corpus.md by heredoc (not write_file), counts only, and message the lead with paths + counts.

Constraints: no rm/mv/chmod/chown beyond what is listed (cp -a, docker tag/build/compose are listed); unlisted approval dialogs: do not accept, report. Never print .env contents.
