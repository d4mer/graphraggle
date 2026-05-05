# ADR 0003: Query Readiness Definition

## Status
accepted

## Context

Operators currently have to infer whether a document is truly searchable. The system needs an explicit concept of readiness so status APIs and later UX docs can answer that directly.

## Decision

A document is query-ready when:

1. `status == ingested`
2. it is not superseded by a newer active logical version
3. it is not blocked by validation or split-required state

Readiness should be exposed via:

1. `query_ready`
2. optional human-readable `readiness_reason`

## Alternatives Considered

1. Infer readiness only from operator intuition and raw status strings.
   Rejected because it is too opaque.
2. Require post-ingest manual verification before any document is considered ready.
   Rejected for now as too operationally heavy for the initial model.

## Consequences

1. Status APIs need to expose readiness explicitly.
2. Reindex behavior can be defined against non-ready states more safely.
3. Operator docs can distinguish uploaded, processing, failed, and queryable documents clearly.

## Affected Packets

1. Packet 01
2. Packet 03
3. Packet 06
4. Packet 09

## Open Follow-Ups

1. Decide whether `query_ready` is derived or stored in Packet 03 execution.
