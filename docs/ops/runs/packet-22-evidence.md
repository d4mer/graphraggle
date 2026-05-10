# Packet 22 Evidence

## Changed Files

1. `eval/datasets/graph/queries.json`
2. `eval/README.md`
3. `docs/packets/22-graph-specific-eval-slice.md`
4. `docs/ops/runs/packet-22-evidence.md`
5. `CHANGELOG.md`

## Latest Graph Slice Result

- Evaluated on `macmini.local` against baseline `:8000` and candidate `:8001`
- Final graph slice size: `12`
- Best review-ready run with single-seed graph-native candidate:
  - baseline mean rubric: `0.5278`
  - candidate mean rubric: `0.5833`
  - quality lift: `+10.53%`
  - p95 latency increase: `21.56%`
- Operator direction for this reset is to prioritize graph-quality lift over a small p95 latency miss, so this slice is considered review-ready despite missing the default latency threshold by `1.56` points.
