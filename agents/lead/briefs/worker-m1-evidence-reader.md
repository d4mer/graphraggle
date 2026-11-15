# Worker brief — M1 evidence reader: shape-tolerant and self-reporting (flag stays OFF, no enforcement)

Base: current `origin/master` or local `master` (d962e48 or later; origin may be behind, expected). Own branch/worktree, no push, one commit. Synthetic data only. Bare full suite must pass. Hand back via `rig queue create`.
This is the "small worker brief" from your own investigation (qitem-20261115182714). Do it AFTER the GL-13b row, if that is already in your queue.

1. Widen `app/grounding.py:evidence_texts` to the key set `app/rerank.py:33-41` already accepts (`content`, `text`, `chunk_text`, `body`) and accept list-valued content (join), as `app/generation.py` already assumes. Share ONE helper between the three readers if that is clean;
   otherwise keep the gate's own and add the contract test below. Do not change any default, flag or answer/citation/status behaviour; with the flag off the envelope stays byte-identical (keep the existing test).
2. Add `grounding_references_with_content` and `grounding_references_content_shapes` (counts per shape: string / list / empty / absent) to the query-log projection in `app/observability.py`, so a run reports what the evidence looked like without storing reference text.
3. Tests pinning string / list / empty-list / absent citations, and a contract test tying the gate's key list to `app/rerank.py`'s so the readers cannot drift apart.
4. Do NOT implement `reconstruct_if_known`, case-folding, company-code handling or enforcement; they wait for the Hermes reference-shape facts (brief hermes-m1-reference-shape.md) and the lead's go. If the Hermes facts show LightRAG sends no content at all, this change will not by itself unblind the gate; say so in the hand-back as a one-liner, do not widen scope.
