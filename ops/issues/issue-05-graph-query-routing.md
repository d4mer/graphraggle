Title: [GraphRAG] Route graph-shaped questions to graph retrieval

## Goal

Send only graph-shaped questions through graph-native retrieval so the system uses graph reasoning where it helps and avoids unnecessary graph cost elsewhere.

## Acceptance Criteria

- [ ] Graph-shaped queries activate graph-native retrieval.
- [ ] Non-graph-shaped queries remain on the standard retrieval path.
- [ ] Routing decisions are observable and testable.

## Scope

- Add graph query classification/routing logic.
- Wire routing into the gateway orchestrator.
- Expose route decision metadata.
- Out of scope: broad policy tuning across all domains.

## Testing

- Verify routing outcomes through `/query` metadata and response behavior.
- Verify graph-native retrieval is not used for non-graph-shaped queries.
- Mock only external retrieval boundaries.

## Dependencies

- Depends on `issue-01-graph-native-retrieval-path`.
