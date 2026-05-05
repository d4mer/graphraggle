# ADR 0004: Company Attribution Policy

## Status
accepted

## Context

Company-aware provenance exists conceptually but is not enforced meaningfully in the current live flow. The stack needs a first-pass policy that improves safety without forcing full hard isolation immediately.

## Decision

Use this precedence for company attribution:

1. explicit company value provided by request or operator input
2. filesystem path inference
3. null / unscoped

Recommended first filesystem rule:

1. first directory under `source_docs/` is the company key

Recommended initial retrieval policy:

1. company-scoped queries should prefer matching company only
2. unscoped documents remain visible and operator-inspectable
3. unscoped document behavior must be documented explicitly

## Alternatives Considered

1. Require company for every ingest immediately.
   Rejected because it would slow adoption before the rest of the lifecycle model is in place.
2. Ignore company until a future hard-isolation redesign.
   Rejected because provenance and scoping are already known limitations.

## Consequences

1. Upload and worker flows need company attribution support.
2. Status and docs should expose company source clearly.
3. Packet 07 must finalize concrete API and query behavior.

## Affected Packets

1. Packet 04
2. Packet 07
3. Packet 09

## Open Follow-Ups

1. Finalize exact company-scoped query behavior in Packet 07.
2. Decide whether `company_source` is required in persistent state.
