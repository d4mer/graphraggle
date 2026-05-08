Title: [GraphRAG] Fuse graph and vector evidence with provenance

## Goal

Combine graph-native evidence and vector/hybrid evidence into one deterministic, provenance-preserving evidence set for answer generation.

## Acceptance Criteria

- [ ] Graph and vector evidence are fused without losing source provenance.
- [ ] Fusion is deterministic and deduplicates overlapping evidence safely.
- [ ] Combined evidence improves the available answer context without introducing synthetic, ungrounded relationships.

## Scope

- Build provenance-aware fusion logic.
- Keep graph and vector evidence distinct until explicit fusion.
- Add dedupe and ordering rules across both channels.
- Out of scope: graph routing policy, eval promotion decisions.

## Testing

- Verify fused evidence through `/query` output behavior.
- Verify provenance is preserved on merged results.
- Mock only graph/vector retrieval boundaries.

## Dependencies

- Depends on `issue-01-graph-native-retrieval-path`.
