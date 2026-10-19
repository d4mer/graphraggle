# Worker brief: merge near-empty stub pieces (small, one fix)

Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking` (tip 583d7e1). Do not commit/push.
Hermes' real run found doc01 (a 2-turn monolith) ends in a 28-token LAST piece (a near-empty trailing turn
label line) next to a ~3000-token piece. `_split_oversize`'s weld rule never sees it because it is a standalone
turn, not a split tail. Fix: after the pieces are built, any piece below the merge floor (min_piece_tokens,
150) that is not the document's only piece merges into its previous (else next) neighbour, for every kind,
including a trailing standalone turn; the merged piece must still fit the window (if not, leave it and warn).
Also make `_split_oversize` produce balanced pieces (a 13.5k/13.5k/3.7k-char split of a monolithic turn
should be roughly equal thirds). Tests with invented text: trailing label-only turn, leading stub, a stub
between two big pieces, a stub whose merge would exceed the window. Full suite green in a bare env; control
unchanged. IMPORTANT: this is NOT part of the stack2 test build (chunkrun7); it is a follow-up, so it must
not change behaviour on documents that have no stub (report piece counts for the existing synthetic shapes
before and after).
