# Hermes brief: stop the chunk-350 run, then structure shape report (read-only)

**Status: APPROVED by the operator in writing on 2026-10-17.** Reason: the chunk-350 run cuts
every document, including the PDF/DOCX text copies, with a fixed token cutter; the operator does
not trust that for tables and non-standard layouts, and the PDF is about half of the chunks.
The ingest is only ~30 minutes in. Production is not touched by either part.

## Part 1: stop the run cleanly (stack2 only)
1. Stop the detached `run_chunk350.sh` and its loop driver and monitor (use the run.token
   mechanism / the driver PIDs; do not `pkill` broadly). Do not stop anything else.
2. Restore `probe/loop_corpus.json` from `scratch/pilot/loop_corpus.json.orig` and verify
   (6 entries, the original). Remove any `monitor.stop` leftovers; archive the aborted
   attempt's state under `docs/ops/runs/chunk350/attempt1-aborted/`.
3. Record what stack2 holds now (status counts, chunk count so far) and which values the loop
   wrote to stack2's `.env` (CHUNK_SIZE/CHUNK_OVERLAP_SIZE only). Do NOT revert stack2's
   `.env` and do NOT tear stack2 down; do not clear its store (a later brief will).
4. Confirm production unchanged: LightRAG :9621 processed=10/all=10, gateway ingested=10, no
   container churn, `.env` untouched. Set STATUS in `chunk350-confirm.md` to STOPPED BY
   OPERATOR (reason as above) with the UTC time.

## Part 2: read-only structure shape report (counts and shapes only; no text leaves the host)
For each of the 10 corpus documents (masked names; use the text copies in
`scratch/phase1/corpus/` AND, for the PDF and DOCX, what LightRAG's own parser stored: read the
stored chunk text sizes and structure markers via read-only GETs against production, never
print text): 
1. Format and size: chars, lines, tokens (tiktoken cl100k).
2. Table-likeness: count of lines with a pipe-delimited row (`|`), with tab separators, with 3+
   runs of 2+ spaces (columnar), numeric-heavy lines (>= 50% digits/punctuation); length of the
   longest run of consecutive such lines; whether header-separator rows (`---`) occur.
3. Headings and structure: markdown headings (`#`), numbered headings (`1.2.3`), ALL-CAPS short
   lines, page markers or running headers/footers (a short line repeated >= 3 times), bullet and
   numbered lists, blank-line paragraph breaks, mean/p90/max paragraph length in tokens, the
   longest unbroken paragraph.
4. For the PDF and the DOCX specifically: did LightRAG's parser keep tables as markdown
   tables, as flattened text, or drop them (shape counts only)? Which parser path
   (`native` or `legacy`) does production use for each, and what chunking strategy / env does
   it apply to uploaded binaries (LightRAG env `CHUNKING_STRATEGY`-style names, if any)?
5. Probe coverage: for the 34 dev and 32+ held-out evidence probes, how many gold quotes lie
   inside a table-like region vs plain text, per document (counts only).
6. One paragraph: what fraction of the corpus (tokens) is table-like or list-like, and
   which documents a token cutter would damage most.

## Hard rules
No content leaves the host; no production change; do not touch the worker; no `.env` edits on
production; no ingest; stack2 stays up. Report to `~/rag-project/docs/ops/runs/shape-report.md`
(STATUS line first), files not `rig send`. Batch approvals up front.
