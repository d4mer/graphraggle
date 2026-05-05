# Release Readiness Checklist

## Purpose

Use this checklist before considering a set of packet changes stable enough for broader operator use.

## Required

1. All included packets are marked `accepted`.
2. Acceptance evidence exists for each packet.
3. Live stack verification was completed for runtime-affecting packets.
4. Docs were updated to match real behavior.
5. No known severe findings remain unresolved.

## Runtime Confidence

1. Gateway health is good.
2. LightRAG health is good.
3. Known-good upload and retrieval flow works.
4. Query readiness behavior is verified.
5. Any relevant scoping behavior is verified.

## Operator Confidence

1. Quickstart still works.
2. Troubleshooting path is current.
3. UI surface responsibilities are clear.

## Final Check

1. Risks that remain are documented.
2. Rollback path is understood.
