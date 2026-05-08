# Packet 16.2 Evidence

## Scope

Roll back GraphRAG precision risk by narrowing when graph expansion runs and tightening what graph evidence can enter the answer path.

## Changed Files

1. `app/api.py`
2. `app/graphrag.py`
3. `tests/test_graphrag.py`
4. `CHANGELOG.md`

## Commands Run

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile app/api.py app/graphrag.py
```

## Observed Outputs

1. Full test suite passes after GraphRAG precision rollback changes.
2. GraphRAG now skips non-transcript/process/logistics queries with explicit metadata.
3. Filtered graph neighbors are capped to 3 and require stronger relevance.

## Risks Remaining

1. GraphRAG may still regress quality on transcript/process queries; gate rerun is required.
2. The no-improvement guard uses deterministic term-overlap proxy, which is conservative but not semantic.
3. One-hop-only rollout may reduce potential upside while stabilizing quality.

## Rollback

1. Set `GRAPH_EXPANSION_ENABLED=false` in production.
2. Rebuild the gateway service.
3. Revert `app/api.py` and `app/graphrag.py` if needed.
