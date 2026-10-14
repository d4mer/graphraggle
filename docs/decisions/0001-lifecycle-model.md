# ADR 0001: Lifecycle Model

## Status
accepted

## Context

The current document status model is too coarse for reliable operator triage and later hardening work. Validation, lifecycle, readiness, and failures need to be represented separately enough to support ingestion controls, retries, and usability improvements.

## Decision

Use:

1. `status` for lifecycle/progress
2. `validation_state` for validation verdict
3. `error_stage` for failure stage
4. `error_code` for classified failure reason

Recommended lifecycle states:

1. `pending`
2. `validating`
3. `accepted`
4. `submitted`
5. `processing`
6. `ingested`
7. `failed`
8. `rejected`
9. `superseded`

Optional later:

1. `quarantined`

## Alternatives Considered

1. Use a single overloaded `status` field for everything.
   This was rejected because validation and lifecycle semantics would become hard to reason about.
2. Defer lifecycle formalization until after upload/worker changes.
   This was rejected because later packets depend on stable state semantics.

## Consequences

1. Later packets can rely on a consistent status contract.
2. APIs and schema need expansion to support the new model.
3. Operator documentation can be clearer and more task-oriented.

## Enforced transition table (what the code actually does)

The table above is now the code's table too. `LEGAL_TRANSITIONS` in
`app/state_store.py` is the single definition, and the two writers
(`update_document_state`, `upsert_document`) enforce it inside the SQL `WHERE`,
so the check is atomic with the write: the row cannot change between the check
and the update. A status is always reachable from itself, so replaying the same
event is a no-op rather than a violation. Read the denials, not the allowances:

1. Any status may return to `pending`. That is what an operator reindex
   (`/ingest/reindex`, including `force=true`) does.
2. `ingested` may go to `rejected`: the file on disk changed and no longer
   validates, so the document really is rejected now. `ingested` may also go back
   to work (`pending`, `validating`, `accepted`, `submitted`) or be superseded.
3. `ingested` never goes to `failed` or `processing` in place. A query-ready
   document is not made un-query-ready by a late or unrelated write. The write
   that used to be able to do that is a stale poll result, and it is stopped by
   the compare-and-set below, not by this table.
4. `ingested` is only reachable from `submitted`, `processing` or `failed`. A row
   that never had an upstream track cannot become query-ready.
5. `rejected` and `superseded` must be re-validated (`pending`, `validating`,
   `accepted`, `submitted`) before they can be `processing` or `ingested` again.
6. An unknown status value written by code is refused outright - that is a typo,
   not a lifecycle state. A row that already holds a status this build does not
   know about is allowed out, so a future status cannot wedge the pipeline.
7. `superseded` is read-derived: nothing writes it as a stored status.
   `annotate_documents()` marks a document superseded at read time when another
   active row names it in `supersedes_document_id`, and `derive_readiness()` also
   tolerates a legacy row that carries the value. It stays in the vocabulary so
   such a row is not wedged; adding a writer would be a policy change.

## Compare-and-set on writes

`update_document_state` and `increment_retry` accept `expect_track_id=` and
`expect_status=`. The write then only applies to a row that still matches what
the caller read a moment earlier. That is what makes the asynchronous parts safe:

1. `worker.poll_track` always passes both. A result for a LightRAG `track_id`
   that has since been replaced matches no row, so it cannot overwrite the newer
   submission with a stale verdict, a wrong status, or `track_id=NULL`.
2. `worker.submit_file` guards its failure write-backs with
   `expect_status="accepted"`: the failure describes the submission this scan
   made, and if an operator reindex moved the row while the request was in
   flight, the operator's newer state wins.
3. The successful submit write is deliberately not guarded. Until it lands, the
   new `track_id` exists only in a local variable, so it cannot corrupt a newer
   submission - and dropping it would strand a live LightRAG track with no row
   pointing at it, which is worse than the stale `warnings_json` overwrite it can
   still carry.
4. `increment_retry` optionally compares, so a retry is never counted against a
   row that has moved to a different track or status; retry budget is only spent
   on submissions that still exist.

## What a refused write looks like

Nothing raises and nothing is silent: the row keeps the newer state and the
`gateway.state_store` logger emits a WARNING naming what was expected and what
the row actually holds, for example

```
write for /data/uploads/6f1a...--report.txt ignored: status=failed
    expect_track_id=trk-8f21 expect_status=processing,
    row now (path=/data/uploads/6f1a...--report.txt status=ingested track_id=trk-bc07)
retry not counted for /data/source_docs/companyA/a.txt:
    expect_track_id=trk-44aa expect_status=failed,
    row now (path=... status=submitted track_id=trk-9c10)
upsert of /data/source_docs/companyA/a.txt as rejected ignored:
    illegal transition from the row as it is now (path=... status=ingested track_id=)
```

If a document is stuck, or shows the wrong `query_ready`, those lines are the
first place to look.

## Affected Packets

1. Packet 01
2. Packet 03
3. Packet 04
4. Packet 05
5. Packet 06

## Open Follow-Ups

1. Decide whether `quarantined` is needed in the first runtime implementation.
2. ~~Finalize transition table in Packet 01 execution.~~ Finalised in code as
   `LEGAL_TRANSITIONS` in `app/state_store.py` (see *Enforced transition table*
   above); the table lives with the enforcement, so if the two ever disagree the
   code is the thing to fix and this ADR to update.
