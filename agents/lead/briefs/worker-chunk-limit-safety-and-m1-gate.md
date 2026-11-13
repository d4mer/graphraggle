# Worker brief — (1) relative safety factor on chunk_token_size (option B), (2) M1 asked-identifier gate, flag-only

Base: branch from the shared repo's local `master` at b8a3457 or later (it contains your harness commit; it is not pushed yet, so `origin/master` is 1 behind — that is expected, do not
treat it as a divergence). Own branch/worktree, no push. No corpus or LightRAG content; invented fixtures only. Bare full suite must pass:
`.venv/bin/python -m pytest tests/ -q`. TWO SEPARATE COMMITS on the branch (one per part), each leaving the suite green, so the lead can take them independently.
Hand back via `rig queue create --destination orch-lead@graphraggle --summary ...` with branch, per-commit SHA, test counts.

## Part 1 — option B for the PDF chunk-token overflow (your report on qitem-20261113072024-08aab654, fix 3)
Operator decision: B now, A (count in LightRAG's tokenizer family) later. Hermes confirmed (2026-11-13) `STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=/opt/bgem3-tok` is set and the files
exist on production and stack2, so the embedder-window check is not on the weak counter and B does not widen that window.
- Make the per-document `chunk_token_size` the worker sends to LightRAG `ceil(largest_piece_count * SAFETY) + FIT_MARGIN`, with `SAFETY` a new setting (env alias, default chosen
  from your measurements: you found up to 3x under-count on number-run/table/JSON shapes and 1.21 worst on stack2; pick and justify a default, and cap so it never
  exceeds what LightRAG/the embedder can use). It must change NO piece boundary: the 8 transcript + OrderBook.docx byte-exact results must hold (assert with your fixtures).
- Invert `TestTheGapIsStillOpen` as you described: no submitted segment's true (lower-bound) count exceeds the limit we ask for, on the adversarial fixtures.
- Record the factor used in the row's structure_chunking metadata so a later failure is diagnosable. Document the setting in `docs/05-known-good-config.md` and the change log, and
  note that A remains the proper fix (leave a pointer, do not implement it).

## Part 2 — M1 asked-identifier support gate, FLAG ONLY (your report on qitem-20261113080212-83603a70)
No answer may change. Implement `support_gate` in the gateway query path as you specified: identifiers named in the question must appear (token match, not substring) in the primary
pass's reference content (`all_citations` before scoping/truncation), question sanitised with `sanitize_like_lightrag` first. Emit the result as `query_scope["grounding"]`
(e.g. {asked: [...], supported: bool, unsupported: [...]}); behind a setting, default off, in which case the response and the upstream payload are byte-identical to today
(assert it). When on, it must still never alter the answer, citations or status code. Include the traps you listed as tests. A question with no identifier yields `asked == []`.
Reuse your harness's function rather than a second copy. Also add the log line/field needed so a later run can compute discrimination counts per probe without storing reference text.
If you find `content` is not present on references in the real response shape, say so and stop that part rather than guessing.
