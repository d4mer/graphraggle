# Packet 17: Graph-Native Retrieval Tracer Bullet

## Status
accepted

## Objective

Establish one true graph-native retrieval path through LightRAG's native graph APIs for a graph-shaped query class, and surface provenance-bearing graph evidence through `/query` without changing the baseline answer path.

## Result

Implemented a minimal graph-native retrieval client using LightRAG label search and graph fetch endpoints. `/query` now returns a separate `graph_evidence` field and `graph_native_*` metadata for targeted graph-shaped queries, with fail-open behavior when graph retrieval is unavailable.
