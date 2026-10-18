# Hermes brief: structure probes (Part 1 done; Parts 2 and 3 now) - read-only

**Status: requested by the operator on 2026-10-18 ("brief the worker and Hermes").** Read-only on
production: no `.env` edit, no restart, no ingest, no store change, nothing written to production
directories. Approval prompts appear only in your tmux pane. Results go to files under
`~/rag-project/docs/ops/runs/` (not `rig send`). The lead, worker and benchmark may not see corpus
text; you are the only seat that reads it.

## Why
The 68 existing probes all quote plain transcripts. The OrderBook DOCX and the S/4 PDF are 59% of the
corpus tokens and no probe touches them, so the benchmark cannot say whether structure-aware chunking
helps or hurts there. We also have never run the pre-chunker on the real extracted text.

## Part 1: write structure probes (files, not chat)
Use the text copies in `scratch/phase1/corpus/` (the same extraction LightRAG's legacy parser produced).
Write TWO files, in the same JSONL schema as `probe/solvay-dev-v1.jsonl`, plus the new optional fields:
- `~/rag-project/probe/structure-dev-v1.jsonl`
- `~/rag-project/probe/structure-heldout-v1.jsonl`
Each: 15 to 20 probes (target 8 PDF, 8 DOCX; if the documents cannot support that, say how many they can).
- Fields: `id` (prefix `sd-` / `sh-`), `type`, `source_kind` (`pdf` or `docx`), `question`, `evidence`
  (a VERBATIM quote of 4 to 40 words from the text copy, whitespace-exact), plus for table probes
  `row_key` and `row_value` (short strings that must both appear in the same table row of the copy).
- Mix, per file: single-fact lookups inside a table row (the value depends on the row label and column
  header), lookups in a numbered section (answer depends on the section heading), a numeric run
  (look up one value), and 2 to 3 questions whose answer sits near a page header/footer boundary or
  needs the section title to disambiguate. Write questions the way a user would ask, not by copying the
  evidence wording. No question answerable from document 01 to 04, 06, 08 to 10.
- Dev and held-out must use different rows/sections. Do not reuse a quote across files.
- Verify by program that every `evidence` (and `row_key`, `row_value`) is present in the text copy after
  the whitespace/case normalisation in `scripts/overnight_loop.py: normalise_quote`. Report the pass
  count; drop any probe that fails.
- Do not report any question, quote or file name. Report ids, counts and per-document spread only.

## Part 2: APPROVED by the operator on 2026-10-18 - run the new ingest path on the real documents (counts only)
Code: `~/rag-project/docs/chunkrun5.tgz` (commit 808cf6e, branch context-chunking), manifest
`chunkrun5-MANIFEST.txt` with sha256 e798558e7818c9529560b46bd2923b2907253d71e91923ad9a3b76d02ef433e8. Verify the sha256, then unpack into a NEW scratch directory
(e.g. `scratch/chunkrun5/`), never into the live app. Production is not touched.

Setup (scratch only):
- Create a scratch venv under `scratch/chunkrun5/venv` and install `pypdf==5.3.0` and
  `python-docx==1.1.2` into it (network pip in a scratch venv is approved; nothing else is installed
  anywhere). Report the versions that actually installed and whether those pins resolve. Report whether
  `pdftotext` exists on the host; `app/pdf_extract.py` backend "auto" would use it, and production's
  LightRAG parser used pypdf, so run PDF extraction with `backend="pypdf"` as the primary and, if
  pdftotext exists, report the difference in counts only.
- Find mxbai-embed-large-v1's tokenizer files (tokenizer.json or vocab.txt) on the host (oMLX model dir
  or HF cache); copy the files only into scratch. Use `app.embed_tokenizer.load_embed_tokenizer`. If you
  cannot find them, say so and use a WordPiece vocab only if it is the mxbai one; do not guess.
- Run python with `-I`, from scratch, with the unpacked `app` importable. No network except the pip
  step. Do not run the worker or the loop driver.

Run, per the 10 documents (masked names): take the ORIGINAL files for doc05 (docx) and doc07 (pdf) from
`source_docs/` read-only (copy to scratch), and the text copies for the 8 transcripts. Extract with
`extract_docx_text` / `extract_pdf_text` (and report the word count against the text copies LightRAG's
parser produced, as a sanity check: ratio only), then call `app.worker.prepare_structure_payload`
with `STRUCTURE_CHUNKING_ENABLED=1` set in the scratch process env (budget 350 default; also run the
documented 450 ceiling settings as shipped). Report per document:
- kind detected, piece count, piece size p50/p90/max in cl100k AND in mxbai WordPiece tokens, pieces over
  512 WordPiece tokens (count), the chunk_token_size the worker would send, any fallback and its
  `structure_chunking_reason`, and the `coverage_gaps` counts (unaccounted word tokens, drops);
- doc05 and doc07: table rows split across pieces (count), pieces carrying a repeated table header row,
  running header/footer lines dropped (lines and distinct lines), count of wrapped PDF rows rejoined;
- transcripts: turns detected vs line count, turns split across pieces, repeated short lines dropped and
  how many of them were speaker-label-looking (a repeated "Yeah"/"Okay" being dropped is the known risk);
- for fixed 350/50 (cl100k) over the same text: piece count and the same table-rows-split count.
Then the acceptance check: for all 68 existing probes and the 32 structure probes, how many evidence
quotes are found verbatim (probe normalisation) in the concatenated pieces, and the ids (only) that are
NOT found. Do the same against the fixed 350/50 pieces.

## Part 3: LightRAG oversize-piece behaviour (read-only, source only)
From the LightRAG v1.5.7 image's own source (read inside the container or from the image layers; do not
modify or restart anything), report what the `fixed_token` chunker does under
`split_by_character` + `split_by_character_only=true` when a split piece exceeds `chunk_token_size`:
re-cut it, truncate it, raise, or keep it whole. Quote the function name and a few lines of CODE (not
corpus). Also report whether the delimiter string we use (see `render_for_lightrag` in the tarball) can
be sent in the JSON body unchanged, and whether LightRAG strips or alters whitespace in pieces.

## Hard rules
- Read-only on production and on `source_docs/`, `uploads/`, `lightrag_store/`. No ingest, no restart.
- The worker keeps running. Do not run the loop driver.
- Stack2 stays as it is.
- Stop and report if anything surprising happens.

## Report
`~/rag-project/docs/ops/runs/structure-probes.md` with STATUS on line 1, Part 1 counts, Part 2 tables,
and the exact paths of the two probe files so the lead can include them in the next benchmark run.
