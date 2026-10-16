# Worker brief: fix the instrument before any more chunking experiments (branch `chunking`)

Context: adversarial review `docs/research/graphraggle-adversarial-review.md` (read F1-F3 and
sections on the eval harness) and `docs/research/chunking-adversarial-review.md`. The pilot
could not show a difference: P1 skipped one of two docs, the corpus (~16 chunks) was smaller
than top_k, and the judge ignores wrong-speaker attribution. Work only in
/home/maverick/work/graphraggle-chunking. You may NOT read corpus, probe contents, staging or
any LightRAG data. Use synthetic fixtures. Bare-env full suite must pass. No network, no ssh.

1. `app/transcript_prep.py` F2: a `(Label)` line failing label validation is appended to the
   previous speaker's turn. Make an unparseable-label line either its own turn with a
   `unknown` speaker or leave the file untouched (`applied=false`, reason
   `unparseable-labels(n)`); never merge into the previous speaker. Test with `(Dr. Lee)`,
   `(Ana, Acme)`.
2. F3: widen the label parser to the real dialect while keeping false positives low: labels
   with periods, commas, nested parens, digits, up to 8 words, an optional timestamp before
   `(`; a minimum of 2 recurring labels or a label covering >=20% of lines. Result must
   expose counts of parsed/rejected label lines (no text). Tests with synthetic variants of
   each; add a fixture whose labels all differ only by role suffix.
3. Judge/attribution in `scripts/overnight_loop.py` (~201-243, rule 3): add a separate
   `attribution` verdict (right speaker / wrong speaker / n.a.) reported as its own column;
   keep `correct` as is. Report refusals separately: `ok%` must not count correct refusals as
   accuracy; add `answerable_correct%` and `unanswerable_refused%`.
4. Retrieval-recall metric: per probe, whether the gold evidence string/turn id appears in the
   context LightRAG returned (use `only_need_context`/the references the query API provides;
   check LightRAG 1.5.7's OpenAPI in the repo docs, do not guess); report `evidence_in_context%`
   per arm. Probe files may gain an optional `evidence` field; code must tolerate absence.
5. Statistics: support `--draws N` per probe (default 1), report mean and paired difference with
   a bootstrap CI between two named arms in results.md; a flag `--second-judge-model`
   (optional, off by default) recording agreement with the primary judge.
6. Add an experiments file `docs/ops/loop/experiments-retrieval-sweep.json`: arms with NO
   re-ingest (`reingest:false`) varying only gateway/LightRAG query-side settings: rerank
   candidate depth (the actual LightRAG parameter name: look it up, e.g. top_k/chunk_top_k),
   token budgets (MAX_ENTITY_TOKENS, MAX_RELATION_TOKENS), chunk_top_k 20/50/100. Use only
   settings the code or LightRAG docs prove exist.
7. Tests for all of the above; update docs/ops/loop README or the module docstring.
Hand back with: diff summary, suite result, anything you could not verify. Do not commit
context.md; the lead commits.
