# Hermes brief: rerun the real-document check with the BGE-M3 / no-cap build, and calibrate topic cuts (read-only, scratch only)

**Status: APPROVED by the operator on 2026-10-19 ("build the tarball and brief Hermes").** Same hard rules as
docs/briefs-hermes-structure-probes.md: no production change, no ingest into any LightRAG store, no restart,
no .env edit, no corpus text in any report, results appended to
`~/rag-project/docs/ops/runs/structure-probes.md` (files, not rig send). Stack2 stays as it is.

Code: `~/rag-project/docs/chunkrun6.tgz` (commit 63c9cc3), manifest `chunkrun6-MANIFEST.txt`,
sha256 27625f252ccb44f109cacbfcec5b65f695aecffa56e26350ae5292ba1df77dc6. Verify, unpack into a NEW scratch dir `scratch/chunkrun6/`. Reuse the scratch venv
(pypdf/python-docx) and `bgem3-tok/` from chunkrun5. Run python with `-I`.

## What changed since chunkrun5
BGE-M3 only (window 8192, tokenizer = bgem3-tok), NO chunk-size cap (a natural unit stays whole; only the
window limits; default max piece 7500), merge floor 150 tokens, pronoun-pair and monolithic transcripts
now detected, PDF extraction prefers `pdftotext -layout` (pypdf fallback), and a topic-shift pass for flat
transcripts that calls the embedder.

## Settings for the run (process env in the scratch process only)
STRUCTURE_CHUNKING_ENABLED=1, STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=<bgem3-tok>,
STRUCTURE_CHUNKING_EMBED_URL=http://192.168.1.190:1234 (oMLX; NOT a 9621/8020 port - the code refuses those),
STRUCTURE_CHUNKING_EMBED_MODEL=bge-m3-mlx-fp16, STRUCTURE_CHUNKING_EMBED_API_KEY_ENV=OMLX_API_KEY (read the
key from the host env; never print it). Defaults for everything else.

## Run, per the 10 documents (masked names), counts only
Same report layout as Part 2, plus:
1. **Pieces:** kind detected, piece count, piece size p10/p50/p90/max in cl100k AND in BGE-M3 tokens, pieces
   over 8192 BGE-M3 tokens (must be 0), chunk_token_size the worker would send, any fallback and
   `structure_chunking_reason`, coverage gaps (unaccounted word tokens, drops).
2. **doc05/doc07:** tables kept whole vs split by row, rows split across pieces, repeated header present in
   each table piece, running header/footer lines dropped, wrapped PDF rows rejoined and which PDF backend
   ran (pdftotext expected; poppler exists on the host), pieces under the 150-token floor (should be ~0),
   tiny-piece count before vs after merging if the stats expose it.
3. **Transcripts:** kind detected for all 8 (expect transcript for all), turns detected, turns split inside
   themselves (doc01's monolithic turns), pieces per document, how many topic cuts were made, embedder calls
   and cache hits, and whether the topic pass ran or fell back (reason). doc01 is ~9k tokens in two turns: report
   its piece count and sizes.
4. **Calibration of the topic cut-off (read-only, scratch):** the code's default threshold is 0.30 and was
   tuned on synthetic data. For each transcript print the distribution of the topic depth scores (min, p10,
   p50, p90, max, and how many gaps exceed 0.20 / 0.30 / 0.40 / 0.50), and the resulting piece count per
   document at each of those four thresholds. Do not tune anything; report so the lead can pick one value.
   Say whether pieces look plausibly topic-sized by SIZE only (p50/p90), never by content.
5. **Acceptance:** of the 68 plain probes and the 32 structure probes (use the v2 docx files and, for
   sd-12/sd-13, report separately because they only match pypdf text), how many evidence quotes are found
   verbatim in the concatenated pieces; list ids not found. Also, for each probe, whether its evidence sits
   inside ONE piece (not split across a cut): count. Do the same for fixed 350/50 and fixed 1200/100
   (cl100k) over the same texts for contrast.
6. **Lost text:** any unaccounted words (counts), any dropped repeated lines in transcripts (counts).

## Report
Append `# Rerun with chunkrun6` to structure-probes.md with the STATUS line updated. Report
anything surprising (an exception, an embedder error, a slow call) with its message but no text.
