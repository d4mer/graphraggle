## Summary

This branch resets the GraphRAG effort away from pseudo-graph augmentation and toward a true LightRAG-native graph retrieval architecture.

Implemented slices:

1. Graph-native retrieval tracer bullet through LightRAG label search + graph fetch
2. Graph-native observability and safety metadata
3. Provenance-preserving graph/vector fusion into `combined_evidence`
4. Graph-aware answer synthesis over fused evidence
5. Explicit graph query routing metadata
6. Graph-specific eval slice for relationship-shaped logistics questions

## Key API Changes

`/query` now may return:

- `graph_evidence`
- `combined_evidence`
- `query_scope.graph_native_*`
- `query_scope.graph_synthesis_*`
- `query_scope.graph_native_route`

The baseline `answer` + `citations` contract remains intact.

## Validation

- Full test suite passes with the project Python 3.8 interpreter.
- Graph-native path is fail-open and does not replace baseline retrieval when unavailable.
- Repaired 12-query eval gate still shows that GraphRAG is not yet promotable for production.

## Remaining Risk

Although the reset architecture is now in place, the repaired gate still shows no quality lift sufficient for production promotion. The next work should focus on improving graph-specific quality rather than more pseudo-graph tuning.
