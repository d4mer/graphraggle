# Live Stack Verification Checklist

## Purpose

Use this checklist after each packet that changes runtime behavior on the Mac mini stack.

## Health

1. Verify gateway health.
2. Verify LightRAG health.
3. Verify model routing endpoints still respond.

## Intake

1. Verify upload path behavior if `/upload` changed.
2. Verify `source_docs/` scanning if worker behavior changed.
3. Verify collision handling if upload naming changed.

## Processing

1. Verify lifecycle status progression.
2. Verify validation fields if validation logic changed.
3. Verify retry or failure behavior if worker logic changed.

## Retrieval

1. Verify known-good smoke-test retrieval still works.
2. Verify citations still return as expected.
3. Verify query readiness behavior if changed.

## Scope And Provenance

1. Verify company attribution behavior if changed.
2. Verify scoped query behavior if changed.

## UI Surfaces

1. Verify Open WebUI still routes through gateway.
2. Verify LightRAG Web UI access if relevant to the packet.

## Evidence

Capture:

1. commands run
2. relevant API responses
3. screenshots for UI changes
4. any remaining anomalies
