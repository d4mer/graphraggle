# Brief - Solvay ingestion pilot: 2 transcripts (read-mostly; one copy step)

Operator GO 2026-10-12. LightRAG is empty and on sigma qwen3.8-flash; rag-ingest-worker is running and
watches ~/rag-project/source_docs. Staged files are in ~/rag-project/staging/Solvay/. Pilot = exactly these two:
  1. "SAP OMP Data Extraction Walkthrough-transcript.txt"
  2. "Oct 12, 2026 at 9_30 AM Recording-transcript.txt"
Do NOT copy anything else from staging. Do not touch other files, containers or config.

## Steps
1. `mkdir -p ~/rag-project/source_docs/Solvay` (if needed). Copy the two files one at a time with `cp`
   (copy, never mv; staging must stay intact), file 1 first, note the UTC time of each copy.
2. Watch the worker: `docker logs --since 5m rag-ingest-worker | tail -40` and the state DB
   (`sqlite3 ~/rag-project/state/ingest.db` read-only: select filename,status,company,track_id,error from documents).
   Confirm company = Solvay for both rows. Poll LightRAG `/documents/track_status/<track_id>` (API key from .env via shell var,
   never print it) every 30 s, up to 30 min per doc. Record per doc: submit time, processed time, total seconds, chunks.
3. When both are processed, analyse the graph (counts and names only; read the lightrag_store graphml/kv files
   by copying them to /tmp first, or use the /graph API read-only):
   - entities and relations per doc and total; isolated entities
   - entities whose name matches /speaker/i (list them with degree); any entity that is just a first name or "Participant"
   - top 20 entities by degree
   - near-duplicate groups (same name after lowercasing and stripping punctuation/spaces): count + 10 examples
4. Check for LLM trouble: `docker logs lightrag-server --since 60m | grep -iE "error|timeout|failed|parse|retry"`;
   report the matching lines (not secrets) and whether any chunk was retried.
5. Ask 2 queries via POST /query mode mix (one about each transcript topic, e.g. "What was discussed about SAP OMP data
   extraction?" and "What happened in the 9:30 AM recording on Oct 12?") and report latency + first 300 chars.
6. Report: short heredoc (not write_file) to ~/rag-project/docs/ops/runs/solvay-pilot.md.

## Constraints
- Only `cp` (the two files) and `mkdir` as writes. No rm/mv/chmod/chown. Do not restart containers.
- If a run exceeds 30 min per doc, or the worker marks a row failed, stop and report - do not retry or delete.
- Approval dialogs for commands not listed here: do not accept; report.
