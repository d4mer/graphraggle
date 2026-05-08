Title: [GraphRAG] Add graph observability and safety controls

## Goal

Make graph-native retrieval safe to operate by exposing clear metadata, preserving fail-open behavior, and allowing rapid rollback.

## Acceptance Criteria

- [ ] `/query` exposes graph-native metadata showing whether graph retrieval ran and contributed.
- [ ] Fail-open behavior is visible in metadata when graph retrieval errors or is skipped.
- [ ] Graph behavior remains controllable via a dedicated feature flag/rollout control.

## Scope

- Add graph-specific `query_scope` metadata fields.
- Preserve and document fail-open semantics.
- Ensure operational rollback can disable graph-native retrieval without code changes.
- Out of scope: retrieval quality improvements, fusion logic, synthesis changes.

## Testing

- Verify metadata through `/query` responses only.
- Verify disabled/failed/active graph states are distinguishable.
- Mock only external graph boundaries.

## Dependencies

- Depends on `issue-01-graph-native-retrieval-path`.
