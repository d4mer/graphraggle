## Summary

This branch replaces the earlier pseudo-graph augmentation path with a true LightRAG-native graph retrieval flow for relationship-heavy GSK logistics questions.

The reset keeps the existing `/query` contract intact while adding a separate graph-native retrieval channel, provenance-preserving fusion, graph-aware synthesis, and graph-specific evaluation.

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

Graph-native behavior is still fail-open: if graph retrieval or graph synthesis underperforms, the standard answer path continues to work.

## Validation

- Full test suite passes with the project Python 3.8 interpreter.
- Graph-native path is fail-open and does not replace baseline retrieval when unavailable.
- Candidate health is verified on `http://macmini.local:8001/health`.
- Eval harness now captures `graph_evidence` and `combined_evidence`, so scoring reflects the graph-backed evidence the candidate actually used.
- Final graph-specific 12-query slice shows measurable quality lift on relationship-heavy GSK logistics questions.
- Best review-ready graph-slice run on `macmini.local` reached:
  - quality lift: `+10.53%`
  - p95 latency increase: `21.56%`
  - sample size: `12/12`
- Operator preference for this reset is quality over latency, so the borderline p95 miss is accepted for review even though it remains above the default `20%` gate.

## Remaining Risk

The main remaining risk is graph-path latency on the promoted graph slice. Quality is now measurably better on the chosen graph-native eval set, but p95 remains slightly above the default threshold and should continue to be reduced before any broader rollout beyond this reset-branch decision.
