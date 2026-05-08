# Packet 17 Evidence

## Changed Files

1. `app/config.py`
2. `app/graph_native.py`
3. `app/api.py`
4. `tests/test_graph_native.py`
5. `docs/packets/17-graph-native-retrieval-tracer-bullet.md`
6. `docs/ops/runs/packet-17-evidence.md`
7. `CHANGELOG.md`

## Commands Run

```bash
python3 -m unittest discover -s tests -p "test_*.py"
python3 -m py_compile app/api.py app/lightrag_client.py app/graph_native.py
```

## Notes

This tracer bullet keeps graph-native evidence separate from standard citations and answer synthesis. It is a true graph API path, not LLM-inferred pseudo-graph augmentation.
