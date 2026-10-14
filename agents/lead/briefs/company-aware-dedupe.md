# Brief: company-aware duplicate selection (found in TODO #3 review)

## Precondition: do NOT start until the lifecycle-cleanup item is committed
It edits app/worker.py and app/state_store.py, which this change also touches.
Start only when `git status --short` shows no modified tracked files AND
`git fetch && git rev-list --left-right --count HEAD...origin/master` is `0 0`.
If the tree is dirty with that work, do not touch it: tell lead you are waiting.
Work in /home/maverick/work/graphraggle on `master`; leave the change uncommitted.

## The problem (your own finding, I agree it is a real bug)
Duplicate selection is company-blind in both doors: `pick_duplicate_candidate`
(app/worker.py) matches on sha256 + ACTIVE status only, and `/upload` now calls
the same function. So a company-B upload of content that already exists under
company A (or unscoped) is marked `duplicate_content`, `accepted`,
`query_ready=False`, and never submitted. A company-scoped query for B only
returns citations whose persisted `company` equals B (docs/06-api-reference.md,
ADR 0004), so B can never retrieve a document it uploaded. Silent data loss for
B, caused by A's earlier upload.

## Intent
A document must never be skipped as a duplicate of a row the owner of the new
document cannot see. Duplicates are fine inside one company's view.

## Proposed rule (validate it, don't assume it)
A candidate row may suppress a new document only if
`candidate.company == new.company`, OR the new document is unscoped (company
NULL: unscoped queries return citations from every company, so the content is
still reachable). A scoped new document is never suppressed by a row of another
company or by an unscoped row.
Check this against ADR 0004 ("unscoped documents remain visible") and say if you
think the rule should differ.

## Open question you must answer BEFORE changing behaviour
If the same content is submitted a second time under another company, what does
LightRAG do with it? Its document id is content-derived, so a second submit may
come back as "duplicated"/already-processed with no new indexing, or be attached
to the existing doc, or fail. That decides whether "just submit it" is even
possible. You cannot run live services: answer from the client code
(app/lightrag_client.py), any recorded fixtures or docs in this repo, and the
LightRAG version pinned in compose.yml. If it cannot be determined offline, say
so plainly and stop after the investigation write-up (see below). Do not guess.

## What I want
1. First, a written finding (in your hand-back, and a short note in the queue
   row): current behaviour with a concrete worked example, what upstream does on
   a same-content second submit (or "unknown, here is what I checked"), and which
   of these you recommend:
   a. company-aware candidates only (the rule above) and submit normally,
   b. keep a single indexed copy but make the duplicate serve both companies
      (needs a retrieval/attribution change; say what it would take),
   c. something else.
2. If (a) is viable and low-risk: implement it in the ONE shared place
   (`pick_duplicate_candidate` takes the new document's company), with both
   callers passing it. Keep `/upload` and `/documents` response shapes unchanged.
3. If it is not clear that (a) is safe, STOP after the write-up and wait for me;
   do not ship a partial behaviour change.

## Constraints
- Offline only; no corpus or LightRAG content; no schema change.
- All current tests stay green and unedited unless one encodes the old
  company-blind behaviour: then stop and tell me.
- Company comparison uses `normalize_company` (state_store) so "Acme Corp" and
  "acme corp" agree, same as query scoping does.
- Existing duplicate rows already in state.db are not rewritten by this change;
  say in the hand-back what happens to a row that is currently `duplicate_content`
  across companies when the worker next scans it (does it self-heal on rescan or
  stay stuck?). That determines whether Hermes needs a reindex after deploy.

## Done when
- bare-env `pytest tests/ -q` green.
- New tests: B-upload of A's content is not suppressed; unscoped new doc still is;
  same-company duplicate still suppressed; case/whitespace variants of a company
  name agree. Reproduction tests fail on pre-change code (show it).
- Hand-back: the finding, the rule as implemented, the stuck-row answer, and what
  you chose not to change.
