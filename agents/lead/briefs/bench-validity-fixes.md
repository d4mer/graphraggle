# Worker brief: make the offline benchmark valid for structure-aware chunking

Work in the worktree `/home/maverick/work/graphraggle-ctx` (branch `context-chunking`). Do not commit or
push; the lead does. Do not read corpus or LightRAG content (you have none; Hermes owns the data).

## Intent
`scripts/chunk_embed_bench.py` ranks chunk sizes well on structure-free transcripts. We now need it to
decide whether the structure-aware chunker (app/structure_chunker.py) helps or hurts on tables, the
PDF and the DOCX (59% of the corpus), and whether any gain survives a fair comparison. The lead's audit
found the defects below. Fix them so a result can be trusted, and so a failure is loud, not silent.

## Fixes (all required; tests for each)
1. **Real embedder tokenizer.** Add an optional WordPiece/HF tokenizer for mxbai-embed-large-v1
   (`--embed-tokenizer <path-or-hf-id>`, loaded from a local path; never download inside the script;
   if absent, say so loudly in the report). Report, per arm, `over_embedder_window_*` counted with THAT
   tokenizer (window 512 incl. special tokens), plus p50/p90/p99/max in embedder tokens, in addition to
   the cl100k numbers. Keep cl100k for chunk sizing.
2. **Gold chunk selection.** `quote_location` currently takes the EARLIEST chunk containing the quote.
   With overlap that is biased. Score a probe at the BEST-ranked chunk that contains the full quote
   (or the best seam pair). Keep seam accounting. Test: a quote inside the overlap of two chunks is
   credited to whichever ranks higher.
3. **Coverage gate.** Report `located%` (quote found in some chunk or seam) per arm and per probe type.
   For the `structure` chunker, if located% is below 100% on probes whose quote is present in the raw
   document text, print the probe ids (ids only) under "LOST TEXT" and exit non-zero unless
   `--allow-lost-text`. Misses caused by the chunker dropping or rewriting text must never be mixed
   into retrieval misses. Add a `lost_text` field to each row.
4. **Table-integrity metric.** For probes with `"row_key"` and `"row_value"` fields (strings, optional),
   report `row_intact%`: the share where a chunk contains BOTH the key and the value. Normalise as the
   quote matching does. Report per arm. Document the probe fields in the script docstring.
5. **Equal-token-budget comparison.** Alongside recall@k, add `recall@T` for token budgets
   T in (2000, 4000, 8000): take the ranked chunks in order until their cl100k token total would exceed T,
   and count the probe as a hit if its gold chunk (or both seam chunks) is inside. `--budgets` overrides.
   Include in the table and in `--compare` (paired bootstrap).
6. **Probe types reported separately.** Use the probe's `type` AND a `source_kind` (`transcript`,
   `table`, `pdf`, `docx`; optional field, default `transcript`). The table in the report must show
   transcript vs structure-rich rows apart, with n per row, and mark any row with n < 15 as
   "n too small to compare".
7. **Binary corpus entries.** The script refuses binary corpus files (correct). Add `--text-copies-dir`:
   a directory of pre-extracted text copies, with a `manifest.json` mapping position -> copy file, so
   a corpus entry that is `.pdf`/`.docx` can be read from its text copy instead (Hermes already has
   such copies in `scratch/phase1/corpus/`). Without the flag, behaviour is unchanged.
8. **Run-level guard.** Print, at the top of the report: tokenizer(s) used, embedder window, chunk
   counts per doc position per arm, and a one-line warning if any arm has p99 in embedder tokens > 512.

## Do not
- Do not change chunker logic in app/structure_chunker.py in this task, except to expose anything
  strictly needed (tell the lead if you think it has a bug; do not silently change it).
- Do not add network calls other than the existing embedder/reranker endpoints.
- No chunk text, question text, evidence text or file names in any report or log.

## Done when
- Full suite passes in a bare environment (`env -i ... pytest`), and the control (new test files
  stashed) is unchanged. Report counts.
- Each fix has a unit test with a synthetic document (no real corpus).
- A short README section in docs/ops/loop/README.md or the script docstring lists the new flags and
  probe fields, because Hermes will write probes to that schema.
- Hand-back lists files changed, flags added, and anything you could not do. Be explicit about what you
  did not verify.
