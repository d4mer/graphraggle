Title: [GraphRAG] Add graph-specific evaluation slice and promotion checks

## Goal

Evaluate GraphRAG on graph-shaped, domain-matched questions so promotion decisions reflect whether graph-native retrieval actually improves mission-critical reasoning.

## Acceptance Criteria

- [ ] A graph-specific eval slice exists with domain-matched graph-shaped questions.
- [ ] Eval reports capture graph activation diagnostics alongside quality and latency.
- [ ] Promotion checks can distinguish between graph helping, graph being neutral, and graph harming quality.

## Scope

- Add graph-specific eval dataset and expected topics.
- Extend repaired eval harness outputs with graph activation diagnostics as needed.
- Document promotion criteria for graph-native retrieval.
- Out of scope: automatic promotion.

## Testing

- Verify eval slice loads and runs through public scripts.
- Verify graph activation metrics appear in the gate/report outputs.
- Mock only judge/model boundaries where needed.

## Dependencies

- Depends on `issue-01-graph-native-retrieval-path`, but can progress in parallel once graph-native interfaces are stable.
