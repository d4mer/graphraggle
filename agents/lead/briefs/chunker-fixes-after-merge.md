# Worker brief: two chunker bugs after the context-chunking merge

Context: `context-chunking` is merged into `master` (a9138dd, pushed). The old worktrees
`graphraggle-chunking` and `graphraggle-ctx` and their branches are gone. Work in a new
worktree: `git worktree add ../graphraggle-fixes -b worker/chunker-fixes master`, and only there.
You may NOT read corpus, probe contents, staging or LightRAG data. Use synthetic fixtures.
No network, no ssh. Bare-env full suite must pass (use the main checkout's `.venv`).
Do not commit; the lead commits. Do not touch `agents/` or `rig.yaml`.

Intent: both are real defects you flagged in your hand-back. Fix them with the smallest change
that holds, each with a test that fails before the fix.

1. `app/structure_chunker.py` `_is_all_caps_heading` (~line 268) uses `_TERMINAL.match(stripped)`.
   `_TERMINAL` is `[.!?…]$`, so `.match` only succeeds on a one-character string; an all-caps line
   ending in a full stop is not rejected as a heading. Every other use of `_TERMINAL` is `.search`.
   Change it to `.search`, and add tests: `"FREIGHT COSTS."` is not an all-caps heading,
   `"FREIGHT COSTS"` still is. Check whether the change shifts any existing test expectation;
   report which, and do not edit an expectation without saying why.

2. `app/transcript_prep.py` reads a capitalised key/value list (e.g. `Vendor: depot 0.`) as a
   one-voice transcript. Reproduce with a synthetic list of 5-8 such lines, find why the label
   parser accepts it (look at the F3 rules: recurring label, 20% coverage, the `Label:` form),
   and tighten it so a key/value list is left untouched (`applied=false`, a reason naming it)
   while the real dialects in `tests/test_transcript_prep.py` still parse. Report the rule you
   chose and the false-positive/false-negative trade-off; if no rule separates the cases
   cleanly, say so and propose options rather than guessing.

Already done, skip: poppler-utils is already installed in the `Containerfile`; the handbook
LightRAG tag is fixed.

Hand back with: diff summary, suite result (count), the tests that were red before the fix,
and anything you could not verify.
