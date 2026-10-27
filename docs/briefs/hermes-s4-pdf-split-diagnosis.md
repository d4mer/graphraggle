# Hermes brief: why does the tag cut the S/4 PDF differently? (read-only checks, counts only)

**Status: DRAFT. NOT APPROVED by the operator. Do not start anything.** Everything below is read-only on production
(nothing at all touches production) and scratch-only elsewhere. Approval prompts appear in your pane.
Report to `~/rag-project/docs/ops/runs/s4-pdf-split-diagnosis.md` (files, not `rig send`); STATUS on line 1.

## Context
Your phase 1 replay (`structure-cutover.md`) found: 8 transcripts and the DOCX byte-exact, the S/4 PDF 98 pieces under the
tag (`f7051e6`) vs 106 stored (`0047a69`, chunkrun9), 0 sha overlap. The worker's analysis (synthetic fixtures only) says the
most likely cause (~80%) is that the text given to the chunker differed because the PDF was extracted by a different
backend (`pdftotext -layout` vs `pypdf`), second (~15%) `transcript_prep` applied in one run and not the other, third (~5%)
the heading-rule change in `f7051e6`. These checks decide it. No code is changed.

## Hard rules
- No write to production (`:9621`, `:8020`, `lightrag_store/`, `state/ingest.db`, `source_docs/`, `uploads/`, `.env`), no
  restart, no query to production. Do not use `--allow-production`.
- stack2's store and its scratch state DB are read-only (open SQLite with `mode=ro`).
- No corpus, question or answer text in the report or in any report file: counts, ids, sizes, hashes, offsets.
  Text dumps (step 3) stay in `scratch/` with `chmod 600` and are never printed or pasted.
- One bare command per approval; no chained or looped commands; background anything long with a log (create the log
  directory first).
- Stop at the first surprise and report it.

## Get the tool (lead delivers)
`scripts/chunk_boundary_diff.py` from master `d871cbf`, copied to `~/rag-project/chunk_boundary_diff.py` with its sha256 in the
lead's message. Run it from your existing clone `scratch/cutover-tag` (it has both commits: the bundle carries full
history, `0047a69` is in it). The tool exports each ref with `git archive` to a temp dir and never prints text.
Usage: `python3 chunk_boundary_diff.py --left 0047a69 --right f7051e6 --text-file <dump> | --pdf <file.pdf>
[--only-function] [--env KEY=VALUE]... [--pdf-backend auto|pdftotext|pypdf] [--repo scratch/cutover-tag] [--json]`.

## Step 1: one database read (decides most of it)
In stack2's scratch state DB (`scratch/stack2-structure8/state/ingest.db`, read-only) read, for the S/4 PDF row only, the
fields `structure_extract_backend` and `transcript_prep` (and `warnings_json` keys, names only) from the stored chunkrun9 run.
For the tag replay, report the same fields from the audit run (`scratch/cutover/audit_tag.py` output or its meta). State
whether the two runs used the same backend and the same `transcript_prep` value. If chunkrun9's rows predate the field, say
so: then the backend is unrecoverable from the database and step 2 is the way to find out.

## Step 2: environment differences
Report (versions and presence only): is `pdftotext` on PATH in the environment that ran chunkrun9 and in the one that ran the
audit (`scratch/cutover/venv-tag`); `pdftotext -v` version; `pypdf` version in `scratch/stack2-structure8` vs
`scratch/cutover/venv-tag`; the value of `WORKER_PDF_TEXT_BACKEND` and `TRANSCRIPT_PREP_ENABLED` in each launcher/env.

## Step 3: same text, both chunkers
1. Rebuild the text chunkrun9 sent for the S/4 PDF: the stored pieces concatenated in order with the piece delimiter
   removed, or if that is not exact, extract the PDF copy with `--pdf-backend pdftotext` and again with `pypdf`. Write each
   dump to `scratch/cutover/`, `chmod 600`, report only its length and sha256.
2. For each dump run `chunk_boundary_diff.py --left 0047a69 --right f7051e6 --text-file <dump> --repo scratch/cutover-tag`
   and report: pieces left/right, identical-at-index, sha overlap, `units_whole` left/right, and with `--only-function`
   how many headings flipped (counts and line numbers only). Also report whether the left-side pieces from the
   `pdftotext` dump reproduce the 106 stored (count and sha overlap against stack2's stored chunks, read-only).
3. Pass criteria to report, not interpret: (a) which backend's dump reproduces the stored 106 pieces on the left
   (`0047a69`) side; (b) whether the right side (`f7051e6`) reproduces the same pieces from that same dump.

## Report (counts only)
Per step what ran and the result, the sha256 of each dump and of the tool, the production-unchanged check (container
ids, `StartedAt`, `RestartCount`, `.env` mtime identical at start and end), and one line: "extractor / transcript_prep /
chunker / undetermined" with the evidence that points there. The lead decides what follows; nothing here authorises a
cutover, a re-ingest, or any change.
