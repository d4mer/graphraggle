# Worker brief: fix the two documents that still fall back (from Hermes' chunkrun6 rerun)

Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking` (tip 63c9cc3). Do not commit/push.
Synthetic text only. Everything below is described by shape; no corpus text.

Hermes' real-document run: 8 of 10 documents apply. Two fall back to LightRAG's default chunker because
the structure path emits ONE piece over the 7500/8192 limit. Fix both so no document falls back for size.

1. **Flat path cannot split an over-window unit.** For non-transcript flat text (one very long paragraph,
   ~9000 tokens) the topic path returns "single-unit, no split". Add the same guard the transcript kind
   has: any unit larger than the max piece is split INSIDE itself, in this order: topic-shift cuts (when the
   embedder is reachable), paragraph boundary, sentence boundary near the middle of the allowed range
   (balanced: aim for equal pieces, not a full piece plus a stub), never mid-word. Applies to every kind.
   Invariant (assert in code and test): the structure path never returns a piece over max_piece_tokens or
   the embedder window; if it still would (e.g. one 12k-token sentence), hard-split at whitespace and
   record a warning. Tests: a 9000-token single paragraph, a 20000-token single line, a 12000-token
   sentence-less blob.
2. **Monolithic two-turn transcript not detected.** Shape: the file is 2 lines. Each line starts with a
   paren label `(Word) ` where Word is a pronoun/function word, and the rest of the line is a ~4500-9000
   token turn. The two labels differ and each occurs ONCE, so min_occurrences=4 rejects them. Rule: when
   ALL non-empty lines of a document start with a paren label of the same shape and the document has at
   least 2 lines, treat them as turns (speaker set from line-start labels), even at low counts, as long as
   the labels are not stage markers. Keep false-positive guards (a document of 2 lines that is a heading
   plus a paragraph in parens must not classify). Then each turn goes through item 1 if over the limit.
3. **Mixed name-plus-pronoun rotation not rescued.** Shape: 239 lines, ~half start with `(<ProperName>) `
   (parses already), ~half with `(<Pronoun>) `, strictly alternating, plus ~2 stage-marker lines like
   `(Cross-talk)`. Currently speaker_set_from_alternation rejects it because there is one parsed
   distinct label and the pronoun label is rejected before the alternation check. Rescue: accept the
   pronoun label as the second speaker when the parsed speaker and the pronoun label alternate (gap 2)
   across the document with tolerance for stage-marker lines between them. Tests with invented text.
4. Report, for each of the three synthetic shapes, kind detected, piece count and max piece size, so Hermes
   can compare to its real run. Full suite green in a bare env; control unchanged; counts reported. Say what
   you could not verify.
