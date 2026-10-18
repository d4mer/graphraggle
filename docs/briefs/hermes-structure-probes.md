# Hermes brief: structure probes (Part 1 now; Part 2 pre-chunker check later) - read-only

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

## Part 2: DEFERRED - do not start
Running the shipped pre-chunker on the 10 text copies (counts only) needs a fresh tarball of the
`context-chunking` branch with the benchmark fixes. The lead will send it with a follow-up message and
the exact checks. Stop after Part 1 and report.

## Hard rules
- Read-only on production and on `source_docs/`, `uploads/`, `lightrag_store/`. No ingest, no restart.
- The worker keeps running. Do not run the loop driver.
- Stack2 stays as it is.
- Stop and report if anything surprising happens.

## Report
`~/rag-project/docs/ops/runs/structure-probes.md` with STATUS on line 1, Part 1 counts, Part 2 tables,
and the exact paths of the two probe files so the lead can include them in the next benchmark run.
