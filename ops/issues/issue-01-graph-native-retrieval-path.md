Title: [GraphRAG] Graph-native retrieval tracer bullet

## Goal

Establish one true graph-native retrieval path through LightRAG so a graph-shaped query can return provenance-bearing graph evidence through the existing `/query` interface.

## Acceptance Criteria

- [ ] A graph-shaped query exercises a graph-native retrieval path, not the current pseudo-graph augmentation path.
- [ ] The `/query` response includes graph-derived evidence with explicit provenance (node/edge/path/source backing where available).
- [ ] If graph-native retrieval fails, the request fails open to the standard retrieval path.

## Scope

- Build a graph retrieval client against LightRAG native graph capabilities.
- Add one end-to-end graph retrieval execution path inside the gateway for a narrow graph-shaped question class.
- Surface graph-native evidence in the `/query` response data structure.
- Out of scope: fusion, answer synthesis redesign, broad query routing, eval gating.

## Testing

- Verify through `/query` that graph-native evidence is returned for the targeted question class.
- Verify fail-open behavior when graph retrieval is unavailable.
- Mock only the LightRAG graph boundary.

## Dependencies

None — this is the tracer bullet.
