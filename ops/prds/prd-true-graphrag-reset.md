## Problem Statement

The current pipeline is not delivering mission-critical GraphRAG behavior. It performs strong hybrid retrieval, but the attempted graph expansion path is not a true graph-native retrieval architecture. Instead of traversing trusted graph structure with provenance, it infers relationships from already retrieved text and re-injects them into the citation path. This creates either no measurable quality lift or a quality regression. For a mission-critical use case, that is not acceptable.

## Solution

Reset the GraphRAG implementation around LightRAG's native graph store and graph-native retrieval APIs. The user should still interact with the same `/query` interface, but internally the system should run two retrieval channels in parallel or in a coordinated way:

1. standard vector/hybrid retrieval for direct evidence
2. graph-native retrieval for entity relationships, process dependencies, exception chains, and multi-hop operational reasoning

The system should fuse these channels only after provenance is preserved, then synthesize answers using structured graph evidence and source-backed citation evidence together.

## User Stories

1. As an operator, I want relationship-heavy logistics questions to use true graph traversal, so that answers can connect evidence across documents rather than relying on paraphrase.
2. As an operator, I want the same `/query` endpoint to support graph-native reasoning, so that I do not need to learn a separate interface.
3. As an operator, I want graph results to show provenance, so that I can trust where each relationship came from.
4. As an operator, I want graph-native retrieval to help specifically on transcript/process questions, so that operational reasoning improves where it matters most.
5. As an operator, I want graph retrieval to fail open, so that the system still answers using standard retrieval if graph access fails.
6. As an operator, I want company scoping and reranking to remain intact, so that graph capability does not weaken tenancy or relevance behavior.
7. As an operator, I want graph evidence to be bounded, so that latency remains predictable.
8. As an evaluator, I want a graph-specific eval slice, so that GraphRAG is judged on questions that actually require graph reasoning.
9. As a maintainer, I want graph evidence and vector evidence kept distinct until fusion, so that debugging is possible.
10. As a maintainer, I want graph-native metadata in responses, so that I can verify whether graph retrieval actually ran and contributed.
11. As a maintainer, I want all major modules tested, so that the reset is reliable before rollout.
12. As a maintainer, I want promotion to depend on repaired eval gates, so that GraphRAG is not enabled based on anecdotal wins.

## Implementation Decisions

1. Graph source of truth is LightRAG's native graph store/API, not a new graph database.
2. The reset replaces pseudo-graph augmentation with a true graph retrieval channel.
3. Retrieval architecture is split into two channels:
   - vector/hybrid retrieval channel
   - graph-native retrieval channel
4. Graph-native retrieval is used for graph-shaped questions such as:
   - process dependencies
   - exception chains
   - dashboard/control relationships
   - workflow/status transitions
   - entity-to-entity operational links
5. Graph evidence must preserve provenance:
   - node identity
   - edge/relationship
   - traversal path
   - backing source chunk/document where available
6. Graph evidence and vector evidence remain separate until explicit fusion.
7. Fusion must be deterministic and provenance-aware.
8. Answer synthesis must consume structured graph evidence explicitly, not treat graph results as plain text citations.
9. Graph capability remains behind a dedicated feature flag during rollout.
10. Fail-open remains mandatory: if graph retrieval fails, the existing retrieval path must still serve the answer.
11. Company scoping and rerank behavior must continue to apply after fusion.
12. Promotion must use the repaired eval harness with graph-specific, domain-matched queries.
13. The graph eval set must focus on questions where graph reasoning should outperform standard retrieval, not generic summarization.

## Testing Decisions

All modules are to be tested heavily.

1. **Gateway query orchestrator**
   - test retrieval-path selection
   - test fail-open behavior
   - test metadata emission
   - test scoping/rerank preservation

2. **Graph retrieval client**
   - test endpoint contract parsing
   - test graph provenance handling
   - test failure and timeout behavior
   - test bounded traversal requests

3. **Graph evidence fusion**
   - test deterministic merge behavior
   - test provenance preservation
   - test dedupe rules across vector and graph evidence
   - test graph/vector separation before fusion

4. **Graph-aware answer synthesis**
   - test structured prompt assembly
   - test explicit inclusion of paths, entities, and supporting chunks
   - test graceful degradation when graph evidence is absent

5. **Graph eval slice**
   - test domain-matched query loading
   - test graph activation diagnostics
   - test minimum sample-size enforcement
   - test promotion decision logic against graph-specific thresholds

Good tests for this reset should validate external behavior and provenance, not just helper internals. The tests should prove that graph-native evidence changes answer behavior only when it is both relevant and grounded.

## Out of Scope

1. Building a second graph database or separate knowledge graph platform.
2. Broad UI redesign in Open WebUI or LightRAG Web UI.
3. Agentic orchestration beyond the graph reset itself.
4. Automatic baseline promotion.
5. Open-ended graph experimentation without repaired evaluation.

## Further Notes

1. The current GraphRAG implementation path should be treated as experimental and non-promotable.
2. The repaired gate demonstrated that GraphRAG, as currently implemented, does not produce the required quality lift.
3. This reset should be treated as a new architecture track, not a tuning pass.
4. The first milestone should prove graph-native provenance and improved quality on graph-shaped logistics questions before any broad rollout.
