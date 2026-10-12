# Brief — full backup of rag-project to deltahome (NO deletions)

Operator decision (2026-10-12): the GSK project is discontinued; production data will be wiped.
Before anything is wiped, EVERYTHING must be backed up to deltahome and nothing may be kept
as a backup on the Thinkpad. This task is the backup only. Do NOT delete, move or truncate anything
on the Thinkpad in this task.

Target: `maverick@192.168.1.180:~/backups/graphraggle-20261012/` (deltahome, LAN IP; keys already work
Thinkpad -> deltahome with `ssh -o BatchMode=yes`). 288G free there. Create the directory on deltahome.

## Steps
1. Quiesce for a consistent copy: confirm LightRAG is idle (`curl -s localhost:9621/documents/pipeline_status`,
   busy must be false), then `docker stop rag-ingest-worker lightrag-server`. Leave them stopped
   afterwards (report that you did). Do not touch gateway-api, open-webui, chromadb.
2. Stream tar archives straight to deltahome, no local copies, e.g.
   `cd ~/rag-project && tar czf - lightrag_store | ssh -o BatchMode=yes maverick@192.168.1.180 'cat > ~/backups/graphraggle-20261012/lightrag_store.tgz'`
   Archives, one each: `lightrag_store`, `source_docs`, `uploads`, `state`, `lightrag_inputs`, `logs`,
   `backups` (the existing 437M of earlier backups), and `config.tgz` containing `.env`, `compose.yml`,
   `compose.rollback-worker.yml`, `Containerfile`, `requirements.txt`, `probe/`, `docs/ops/runs/`.
   Also copy `open-webui` data is NOT in scope (not being wiped).
3. Verify on deltahome: `sha256sum *.tgz > SHA256SUMS`, then `tar tzf <each> | wc -l` and compare each
   file count with `find <dir> | wc -l` on the Thinkpad (same dir, now stopped). Also compare sha256 of
   one large file inside each archive vs source (e.g. the largest file in lightrag_store).
4. Write a short report with a heredoc (not write_file; long writes time out) to
   `~/rag-project/docs/ops/runs/backup-deltahome.md`: archive names, sizes, file counts both sides,
   checksum result, state of containers. Include `ls -l` of the deltahome directory. Never print .env contents.

## Constraints
- Read-only on the Thinkpad data except stopping the two containers above.
- No `rm`, `rmdir`, `mv`, `chown`, `chmod` anywhere. If something needs one, stop and report.
- If any approval dialog appears for a command not listed here, do not accept it; report.
- Acceptance: all archives present on deltahome, counts match, SHA256SUMS written, report written.
