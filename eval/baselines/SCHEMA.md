# Baseline Schema — `eval/baselines/packet-NN.json`

Each baseline file is a JSON document with the following structure:

```json
{
  "packet_id": "packet-13",
  "version": 1,
  "timestamp": "2026-05-06T12:00:00Z",
  "author": "operator",
  "config": {
    "LIGHTRAG_BASE_URL": "http://lightrag-server:9621",
    "RERANK_ENABLED": false,
    "RERANK_BINDING_HOST": null,
    "MULTI_QUERY_ENABLED": false,
    "MULTI_QUERY_REWRITE_COUNT": 2,
    "REQUEST_TIMEOUT_SECONDS": 300
  },
  "metrics": {
    "mean_rubric_score": 0.0,
    "p50_latency_ms": 0,
    "p95_latency_ms": 0,
    "grounded_rate": 0.0,
    "fact_consistent_rate": 0.0,
    "complete_rate": 0.0,
    "total_queries": 0,
    "parse_failures": 0
  }
}
```

## Field Descriptions

| Field | Type | Description |
|-------|------|-------------|
| `packet_id` | string | Packet identifier, e.g. `"packet-13"` |
| `version` | int | Monotonic version number for this packet's baseline |
| `timestamp` | string | ISO 8601 timestamp of baseline creation |
| `author` | string | Operator who created the baseline |
| `config` | object | Snapshot of all relevant `app/config.py` settings at time of run |
| `metrics` | object | Aggregated metrics from the eval run (see below) |

### `metrics` object fields

| Field | Type | Description |
|-------|------|-------------|
| `mean_rubric_score` | float | Mean of (grounded + fact_consistent + complete) / 3 across all queries |
| `p50_latency_ms` | int | 50th percentile latency in milliseconds |
| `p95_latency_ms` | int | 95th percentile latency in milliseconds |
| `grounded_rate` | float | Fraction of queries scoring 1 on `grounded` |
| `fact_consistent_rate` | float | Fraction of queries scoring 1 on `fact_consistent` |
| `complete_rate` | float | Fraction of queries scoring 1 on `complete` |
| `total_queries` | int | Total number of queries in the eval set |
| `parse_failures` | int | Number of queries with missing/invalid rubric scores |

## Baseline File Naming

- Format: `packet-{NN}.json` or `packet-{NN}.{N}.json` for minor versions.
- Examples: `packet-13.json`, `packet-14.json`, `packet-13.1.json`
- Each promotion creates a new file; old files are never overwritten.

## Promotion Process

1. Run the eval harness with candidate config against the latest baseline.
2. If gates pass, operator creates a new baseline file with incremented version.
3. Only the operator (human) can promote; no automated promotion.
4. The new baseline file is committed to git along with the run evidence.
