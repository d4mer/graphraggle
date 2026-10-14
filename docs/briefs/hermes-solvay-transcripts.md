# Brief - Solvay ingestion: remaining 6 transcripts (operator GO 2026-10-12)

First finish and write the pilot report (solvay-pilot.md) if you have not. Then do this.
Worker is running and watches ~/rag-project/source_docs. Source: ~/rag-project/staging/Solvay/. Dest: ~/rag-project/source_docs/Solvay/
(already exists). COPY (cp, never mv; staging must stay intact) EXACTLY these 6 files, no others, no "._*" files:
  1. UAT Progress Review and Onboarding-transcript.txt
  2. Title_   OMP Profile Setup and MRP Access Configuration-transcript.txt
  3. Oct 12, 2026 at 10_10 AM Recording-transcript.txt
  4. Title_   S_4HANA and OMP Implementation for Soda Ash-transcript.txt
  5. Cross-GBU OMP Prioritization Update-transcript.txt
  6. It seems there were no meeting summary notes provided. Could-transcript.txt
(note: some names have 3 spaces after "Title_" - take them from `ls` output, quote carefully). Copy them one at a time with a
single `cp -- "<src>" "<dest>/"` per file, small files first as listed. Do not copy the PDF, DOCX or PPTX.

## Watch (expected ~50-70 min total; LightRAG processes one doc at a time)
Every ~5 min until all are done: state DB rows (read-only sqlite: filename,status,company,error_stage,error_code),
LightRAG `/documents/status_counts` (API key from .env via shell var, never print), and
`docker logs --since 10m lightrag-server | grep -iE "error|failed|retry|parse"` (ignore INFO config/timeouts lines).
Stop and report immediately (no retries, no deletes, no restarts) if: any row goes failed/rejected, an LLM error or JSON-parse
failure appears, LightRAG sits on one doc over 25 min, or sigma stops answering
(`curl -s -H "Authorization: Bearer 1234" http://192.168.1.195:1234/v1/models`).

## Report (heredoc, not write_file) to ~/rag-project/docs/ops/runs/solvay-transcripts.md
Per doc: filename, chunks, seconds, status. Totals: docs, entities, relations (graphml node/edge counts, read from a /tmp copy).
Quality counts as in the pilot: entities matching /speaker/i with degree, near-duplicate groups (count + 10 examples), top 20 by
degree, isolated entities. Any errors. Then 3 mix-mode queries (/query) about content from different transcripts, e.g.
"What was decided about OMP prioritization across GBUs?", "What are the UAT progress and onboarding steps?",
"How is S/4HANA and OMP implemented for Soda Ash?" - report latency and the first 300 chars of each answer.

## Constraints
Only `cp` of the 6 files as writes. No rm/mv/chmod/chown, no container restarts. Any approval dialog for a command not listed
here: do not accept; report. Message the lead when done.
