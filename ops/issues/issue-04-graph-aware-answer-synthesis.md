Title: [GraphRAG] Build graph-aware answer synthesis

## Goal

Use structured graph paths and supporting chunks in answer synthesis so graph-native retrieval can improve answers rather than just append extra context.

## Acceptance Criteria

- [ ] The answer prompt explicitly consumes graph paths, entities, and supporting evidence.
- [ ] Graph-shaped questions produce answers that reflect connected reasoning, not just flat citation summarization.
- [ ] If graph evidence is absent, synthesis degrades gracefully to standard retrieval-based answering.

## Scope

- Build a graph-aware prompt assembly step.
- Structure graph evidence for the answer model instead of flattening it into plain text.
- Preserve existing fail-open behavior.
- Out of scope: new UI, graph storage redesign.

## Testing

- Verify answer behavior through `/query` for graph-shaped questions.
- Verify graceful degradation when graph evidence is empty.
- Mock only model and graph boundaries as needed.

## Dependencies

- Depends on `issue-03-graph-evidence-fusion`.
