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

## Affected Packets

1. Packet 01
2. Packet 03
3. Packet 04
4. Packet 05
5. Packet 06

## Open Follow-Ups

1. Decide whether `quarantined` is needed in the first runtime implementation.
2. Finalize transition table in Packet 01 execution.
