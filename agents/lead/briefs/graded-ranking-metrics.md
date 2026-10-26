# Worker brief: graded evidence and ranking metrics for the probe helper

Branch from current `origin/master` (check `git fetch && git rev-list --left-right --count HEAD...origin/master` reads
`0 0` first). Work on a packet branch or worktree of your own; do not commit or push, hand back a diff. You may NOT read
corpus, probe contents, staging or any LightRAG data; use synthetic fixtures with invented text. No network, no ssh.
Bare-env full suite must pass: `.venv/bin/python -m pytest tests/ -q`.

## Why
Our retrieval check, `evidence_in_context` in `scripts/overnight_loop.py` (~1151), answers one binary question: is any gold
quote anywhere in what LightRAG returned. The recall comparisons (production 85, stack2 91, legacy+BGE-M3 83) could only
say "present or not". They could not say whether the evidence sits near the top of the returned context, whether a
multi-fact probe got all of its facts or one of three, or whether the best evidence outranks supporting evidence.
Inspiration: github.com/saurabhkamal/RAG-ReRanker-Comparison, whose retrieval metrics are pure arithmetic against graded
relevant pages (grade 2 = contains the answer, 1 = partly relevant): Hit, Precision, Recall, MRR, MAP, NDCG. Our corpus has
no page numbers, so the unit here is the gold evidence quote, not the page. Read that repo's `eval/retrieval_metrics.py` for
the formulas only (via `gh api`, do not vendor code; there is no licence).

## What to build
1. **New module `scripts/ranking_metrics.py`**, pure functions, no I/O, no LLM:
   - `recall_fraction(gold_hits)`: fraction of a probe's gold quotes found (graded-weighted variant too).
   - `first_hit_rank(ranked_units, gold)`: 1-based position of the first returned unit containing any gold quote, else None.
   - `reciprocal_rank`, `average_precision`, `ndcg_at_k` with grades 2/1/0 using `(2^grade - 1) / log2(position + 1)`
     divided by the ideal ordering, as in the reference repo.
   - Each relevant quote counts once (a quote that appears in two returned units is credited at its first appearance).
   - Use `normalise_quote` from `overnight_loop` (the same normalisation the existing check and `chunk_embed_bench.py` use).
2. **Probe schema, optional and backward compatible.** Today `evidence` is a string or list. Add an optional
   `evidence_grades` (list of ints 0 to 2, same length as the evidence list; missing means all 2). A probe with no
   evidence is `na`, never a miss, exactly as now. Existing probe files must score identically on the old metric.
3. **Ranked units.** `context_strings` (~1101) deliberately returns every string in the response, unordered by meaning,
   because the `only_need_context` envelope is not documented in this repo. Do not guess the envelope. Add an
   order-preserving extractor that returns the strings in the order the payload carries them (depth-first, as the walk
   already does) and treat each string as one unit. Document in the docstring that rank is "position in the response
   walk", and that it is only meaningful if LightRAG lists chunks in ranked order; mark rank metrics `unverified-order`
   in the summary until a human confirms the order against a real response (that is Hermes' job, not yours).
4. **Wire into `scripts/overnight_loop.py` reporting.** Keep `evidence_in_context_pct` unchanged and still the success
   check input. Add per-arm columns: `evidence_recall` (mean fraction of gold quotes found), `mrr`, `ndcg@5`, `hit@1`,
   `hit@5`, each only over probes with evidence. Include them in the paired comparison (`paired_bootstrap_difference`)
   so two arms get a CI on `mrr` and `evidence_recall`, not just on the binary hit.
5. **Also `all-quotes` mode.** The Hermes recall helpers require every normalised quote to appear, while
   `evidence_in_context` accepts any. Report both (`any_hit`, `all_hit`) so the two numbers can be reconciled; do not
   change which one gates success.
6. **No change to the Hermes helpers.** `probe/run_stack2_recall.py` and `run_prod_recall.py` live on the Thinkpad and are
   outside this repo. Make `ranking_metrics.py` importable on its own so they can adopt it later; say in the hand-back
   what they would need to call.

## Tests (synthetic text only)
- Hand-computed cases for each formula: single gold at rank 1, 3 and absent; two gold quotes with grades 2 and 1 in both
  orders (NDCG must differ); a quote duplicated across two units counts once; empty gold returns `na`.
- Backward compatibility: a probe file in today's format gives the same `evidence_in_context_pct` as before.
- `all_hit <= any_hit` always; `evidence_recall` is 1.0 exactly when `all_hit` is true.
- Paired bootstrap on the new metrics is deterministic for a fixed seed.

## Not in scope
Judge changes, generation metrics (faithfulness etc.), any change to success thresholds, any change to Hermes' helpers,
page-number grading (we have none), touching production or the Thinkpad.

## Hand-back (counts only)
The diff, the full-suite result, the list of new columns, the exact function signatures Hermes would call, and one
paragraph on what is still unverified (the rank-order assumption in item 3).
