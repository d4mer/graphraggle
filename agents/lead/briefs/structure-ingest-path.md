# Worker brief: structure-aware ingest for every document type (supersedes the scope of bench-validity-fixes)

Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking`. Do not commit or push. You may not
read corpus content; use synthetic documents only.

## Outcome (read this first)
When STRUCTURE_CHUNKING_ENABLED is on, EVERY ingested document (txt transcripts, pdf, docx, pptx) reaches
LightRAG already cut at its natural boundaries (turns, sections, table rows with the header repeated) with
deterministic context headers, and LightRAG must not re-cut it. Embedder choice is secondary: the budget
is a setting (default sized for the current embedder, ~450 cl100k tokens max piece), not a design constant.

Today the flag only covers the text path. PDF and DOCX are uploaded via /documents/upload, so LightRAG's
legacy parser flattens them and fixed-token cuts them: that is 59% of the corpus and the part that matters.

## Do
1. **PDF and DOCX extraction in the worker**, flag-gated, producing text that app/structure_chunker.py
   can use. Check what the worker image already has (python-pptx is there); if pypdf / python-docx are not
   in requirements.txt, say so and add them pinned, and tell me (the image rebuild is a release item).
   DOCX: keep tables as rows (tab- or pipe-separated, one row per line, header row first) so the chunker
   sees a table. PDF: page-by-page text, keep page furniture in place (the chunker drops repeated
   headers/footers), do not invent structure. Image-only / scanned PDFs: detect no text and fall back.
2. **Submit through POST /documents/text** with the chunking object so LightRAG obeys our cut points:
   fixed_token with split_by_character=<delimiter> and split_by_character_only=true (see
   render_for_lightrag). IMPORTANT unknown: what LightRAG 1.5.7 does with a piece larger than
   CHUNK_SIZE under split_by_character_only. Do not assume. Make the worker set chunk_token_size to at
   least the largest piece (budget plus header plus margin) so nothing is re-cut, assert that per
   document, and if it cannot, fall back to the existing path and record why. Hermes will confirm
   from the image source.
3. **Never lose a document:** any extraction/chunker failure, empty result or piece over the limit falls
   back to today's behaviour (upload/text as now) with a provenance/warning line. Flag off = byte-identical
   to today. Keep the existing duplicate/skip rules and lifecycle CAS untouched.
4. **Fit check:** add an optional embedder-tokenizer check (local WordPiece vocab path, no download) used
   by a test helper and by scripts/chunk_embed_bench.py, so we can state "no piece exceeds 512 embedder
   tokens" instead of guessing from cl100k. If the tokenizer is not available at runtime, use the
   conservative over-count already in the chunker.
5. **Lost-text check:** a function (and a bench-script gate) that verifies, for a document, that all
   non-furniture text survives into the pieces. Misses from dropped text must be separable from retrieval
   misses.

## Cut from the previous brief (do NOT do unless trivial)
Token-budget recall metrics, per-source_kind tables, row-integrity metric, --text-copies-dir, embedder size
sweeps. Keep only what items 4 and 5 need. If you already built some, leave it, do not extend it.

## Done when
- Full suite green in a bare env, control unchanged; counts reported.
- Synthetic end-to-end tests: a synthetic docx with a 60-row table, a synthetic multi-page pdf with
  repeating headers and numbered sections, a transcript, a pptx; each produces pieces with the table
  header repeated, no piece over budget, furniture dropped, and the payload sent to a fake LightRAG
  carries the delimiter and the chunking object. A fallback test per failure mode.
- Hand-back lists files, new dependencies, the one remaining unknown (oversize-piece behaviour), and
  what you did not verify.
