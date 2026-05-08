# Packet 18: Graph Observability And Safety

## Status
accepted

## Objective

Make the graph-native tracer bullet safe to operate by exposing explicit graph-native metadata, preserving fail-open behavior, and keeping rollback as a flag-only operation.

## Result

Added a formal graph-native metadata shape for `/query` responses and verified graph-native states are distinguishable when enabled, applied, skipped, or failed.
