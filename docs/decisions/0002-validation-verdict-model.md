# ADR 0002: Validation Verdict Model

## Status
accepted

## Context

The stack needs a clear pre-ingest validation model so bad inputs do not silently enter LightRAG and large risky inputs do not fail in opaque ways.

## Decision

Use `validation_state` with these values:

1. `not_run`
2. `accept`
3. `warn`
4. `reject`
5. `auto_split`

Interpretation:

1. `accept` means safe to proceed normally.
2. `warn` means proceed but surface warnings to operators.
3. `reject` means do not submit to LightRAG.
4. `auto_split` means do not submit whole as-is; operator or later automation must split first.

## Alternatives Considered

1. Represent `warn` as top-level lifecycle `status`.
   Rejected because warning is a validation verdict, not a lifecycle stage.
2. Only use binary accept/reject outcomes.
   Rejected because it cannot represent workable-but-risky files or split candidates.

## Consequences

1. Validation results can be shown clearly to operators.
2. Worker logic can block or route high-risk inputs intentionally.
3. Later packets must persist validation metadata and warnings.

## Affected Packets

1. Packet 02
2. Packet 03
3. Packet 04
4. Packet 05

## Open Follow-Ups

1. Lock initial size and page thresholds during Packet 02 execution.
2. Decide exact duplicate-content warning semantics during Packet 02 execution.
