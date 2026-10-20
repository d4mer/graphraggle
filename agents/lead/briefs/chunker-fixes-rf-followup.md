# Worker follow-up: one bug in the key/value-list fix (same worktree)

I reviewed your diff in `../graphraggle-fixes` (branch `worker/chunker-fixes`) and re-ran the suite:
1185 passed, matching your report. One bug, then hand back again. Same rules as before: that
worktree only, synthetic fixtures only, no corpus, no commit, do not touch `agents/` or `rig.yaml`.

`_FIELD_KEY_LINE` in `app/transcript_prep.py` is a plain raw string (`r"..."`), so `{_TS}` is
never interpolated. `Vendor: depot 0.` and `>> Vendor: x` match, but `[00:01] Vendor: depot 0.`
does not, so a timestamped key/value list gets the generic `no-speaker-labels` reason instead of
`field-key-labels(n/lines)`.

1. Make the pattern interpolate `_TS` (the file's other patterns use `rf"..."`; mind the
   regex braces such as `{1,30}` when you do), so the optional `[hh:mm(:ss)]` prefix matches.
2. Add a test that fails before the fix: a synthetic key/value list whose lines carry a
   `[00:01]`-style timestamp is left untouched with reason `field-key-labels(...)`. Prove it red
   by stashing the app file, as you did before.
3. Check the other regexes you added in this change for the same slip.

Hand back with: the diff of the fix, suite count, the red test name.
