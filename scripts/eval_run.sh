#!/usr/bin/env bash
# scripts/eval_run.sh — A/B evaluation runner entrypoint
#
# Usage:
#   BASELINE_ENV=baseline CANDIDATE_ENV=candidate ./scripts/eval_run.sh
#
# Environment variables:
#   BASELINE_ENV     — Identifier for baseline env config (e.g. "baseline", "packet-14")
#   CANDIDATE_ENV    — Identifier for candidate env config (e.g. "candidate", "packet-15")
#   GATEWAY_URL     — Gateway API base URL (used when BASELINE_URL/CANDIDATE_URL unset)
#   BASELINE_URL    — Baseline API base URL (optional)
#   CANDIDATE_URL   — Candidate API base URL (optional)
#   QUERY_SET       — Path to queries.json (default: eval/datasets/curated/queries.json)
#   PROD_SAMPLES    — Path to anonymized production samples (optional)
#   OUTPUT_DIR      — Directory for run artifacts (default: docs/ops/runs)
#   REQUEST_TIMEOUT — Per-query request timeout in seconds (default: 60)
#   QUERY_TOP_K     — Optional top_k override for eval queries (default: unset)
#
# Produces:
#   docs/ops/runs/packet-15-eval-report.json  — Machine-readable report
#   docs/ops/runs/packet-15-eval-summary.md   — Human-readable summary

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Defaults ──────────────────────────────────────────────────────────────────
BASELINE_ENV="${BASELINE_ENV:-baseline}"
CANDIDATE_ENV="${CANDIDATE_ENV:-candidate}"
GATEWAY_URL="${GATEWAY_URL:-http://localhost:8000}"
BASELINE_URL="${BASELINE_URL:-$GATEWAY_URL}"
CANDIDATE_URL="${CANDIDATE_URL:-$GATEWAY_URL}"
QUERY_SET="${QUERY_SET:-$ROOT_DIR/eval/datasets/curated/queries.json}"
PROD_SAMPLES="${PROD_SAMPLES:-}"
OUTPUT_DIR="${OUTPUT_DIR:-$ROOT_DIR/docs/ops/runs}"
REQUEST_TIMEOUT="${REQUEST_TIMEOUT:-60}"
QUERY_TOP_K="${QUERY_TOP_K:-}"
API_KEY="${RAG_API_KEY:-}"

# ── Validate ──────────────────────────────────────────────────────────────────
if [ ! -f "$QUERY_SET" ]; then
    echo "ERROR: Query set not found: $QUERY_SET" >&2
    exit 1
fi

mkdir -p "$OUTPUT_DIR"

REPORT_FILE="$OUTPUT_DIR/packet-15-eval-report.json"
SUMMARY_FILE="$OUTPUT_DIR/packet-15-eval-summary.md"

echo "=== Packet 15: A/B Evaluation Runner ==="
echo "Baseline env: $BASELINE_ENV"
echo "Candidate env: $CANDIDATE_ENV"
echo "Baseline URL: $BASELINE_URL"
echo "Candidate URL: $CANDIDATE_URL"
echo "Query set: $QUERY_SET"
echo "Output: $REPORT_FILE"
echo ""

if [ "$BASELINE_URL" = "$CANDIDATE_URL" ]; then
    echo "WARNING: BASELINE_URL and CANDIDATE_URL are identical." >&2
    echo "         This is not a true A/B unless endpoint config differs externally." >&2
fi

# ── Load queries ──────────────────────────────────────────────────────────────
# Merge curated + production samples into a single list
ALL_QUERIES="[]"

# Add curated queries
if [ -f "$QUERY_SET" ]; then
    ALL_QUERIES=$(python3 -c "
import json, sys
with open('$QUERY_SET') as f:
    curated = json.load(f)
print(json.dumps(curated))
")
fi

# Add production samples if provided
if [ -n "$PROD_SAMPLES" ] && [ -f "$PROD_SAMPLES" ]; then
    ALL_QUERIES=$(python3 -c "
import json
with open('$QUERY_SET') as f:
    curated = json.load(f)
with open('$PROD_SAMPLES') as f:
    samples = json.load(f)
merged = curated + samples
print(json.dumps(merged))
")
fi

QUERY_COUNT=$(echo "$ALL_QUERIES" | python3 -c "import json, sys; print(len(json.load(sys.stdin)))")
echo "Total queries: $QUERY_COUNT"
echo ""

# ── Run A/B comparison ────────────────────────────────────────────────────────
echo "Running A/B comparison..."
echo ""

python3 - "$ALL_QUERIES" "$BASELINE_ENV" "$CANDIDATE_ENV" "$BASELINE_URL" "$CANDIDATE_URL" "$REQUEST_TIMEOUT" "$QUERY_TOP_K" "$API_KEY" "$REPORT_FILE" <<'PYEOF'
import json
import sys
import time
import urllib.request
import urllib.error

queries = json.loads(sys.argv[1])
baseline_env = sys.argv[2]
candidate_env = sys.argv[3]
baseline_url = sys.argv[4]
candidate_url = sys.argv[5]
timeout = int(sys.argv[6])
query_top_k = sys.argv[7].strip()
api_key = sys.argv[8]
output_path = sys.argv[9]

results = []
baseline_errors = 0
candidate_errors = 0

for i, q in enumerate(queries):
    qid = q.get("id", f"q{i+1}")
    query_text = q.get("query", "")
    category = q.get("category", "unknown")
    expected_topics = q.get("expected_topics", [])

    print(f"  [{i+1}/{len(queries)}] {qid}: {query_text[:60]}...")

    # Baseline run
    bl_latency_ms = None
    bl_answer = None
    bl_citations = []
    bl_graph_evidence = []
    bl_combined_evidence = []
    bl_query_scope = {}
    bl_error = None
    try:
        payload = {"query": query_text}
        if query_top_k:
            payload["top_k"] = int(query_top_k)
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{baseline_url}/query",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            bl_latency_ms = int((time.time() - t0) * 1000)
            bl_answer = data.get("data", {}).get("answer", "")
            bl_citations = data.get("data", {}).get("citations", [])
            bl_graph_evidence = data.get("data", {}).get("graph_evidence", [])
            bl_combined_evidence = data.get("data", {}).get("combined_evidence", [])
            bl_query_scope = data.get("data", {}).get("query_scope", {})
    except Exception as e:
        bl_error = str(e)
        baseline_errors += 1
        bl_latency_ms = -1

    # Candidate run
    cd_latency_ms = None
    cd_answer = None
    cd_citations = []
    cd_graph_evidence = []
    cd_combined_evidence = []
    cd_query_scope = {}
    cd_error = None
    try:
        payload = {"query": query_text}
        if query_top_k:
            payload["top_k"] = int(query_top_k)
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"{candidate_url}/query",
            data=body,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
            cd_latency_ms = int((time.time() - t0) * 1000)
            cd_answer = data.get("data", {}).get("answer", "")
            cd_citations = data.get("data", {}).get("citations", [])
            cd_graph_evidence = data.get("data", {}).get("graph_evidence", [])
            cd_combined_evidence = data.get("data", {}).get("combined_evidence", [])
            cd_query_scope = data.get("data", {}).get("query_scope", {})
    except Exception as e:
        cd_error = str(e)
        candidate_errors += 1
        cd_latency_ms = -1

    results.append({
        "query_id": qid,
        "query": query_text,
        "category": category,
        "expected_topics": expected_topics,
        "baseline": {
            "latency_ms": bl_latency_ms,
            "answer": bl_answer,
            "citations": bl_citations,
            "graph_evidence": bl_graph_evidence,
            "combined_evidence": bl_combined_evidence,
            "citation_count": len(bl_citations),
            "query_scope": bl_query_scope,
            "error": bl_error,
        },
        "candidate": {
            "latency_ms": cd_latency_ms,
            "answer": cd_answer,
            "citations": cd_citations,
            "graph_evidence": cd_graph_evidence,
            "combined_evidence": cd_combined_evidence,
            "citation_count": len(cd_citations),
            "query_scope": cd_query_scope,
            "error": cd_error,
        },
    })

report = {
    "run_id": "packet-15",
    "baseline_env": baseline_env,
    "candidate_env": candidate_env,
    "baseline_url": baseline_url,
    "candidate_url": candidate_url,
    "total_queries": len(queries),
    "baseline_errors": baseline_errors,
    "candidate_errors": candidate_errors,
    "results": results,
}

with open(output_path, "w") as f:
    json.dump(report, f, indent=2)

print(f"\nBaseline errors: {baseline_errors}")
print(f"Candidate errors: {candidate_errors}")
print(f"Report written to: {output_path}")
PYEOF

echo ""
echo "=== A/B Run Complete ==="
echo "Report: $REPORT_FILE"

# ── Generate human-readable summary ───────────────────────────────────────────
python3 - "$REPORT_FILE" "$SUMMARY_FILE" <<'PYEOF'
import json
import sys

report_path = sys.argv[1]
summary_path = sys.argv[2]

with open(report_path) as f:
    report = json.load(f)

lines = []
lines.append(f"# Packet 15: A/B Evaluation Summary")
lines.append("")
lines.append(f"- **Baseline env**: {report['baseline_env']}")
lines.append(f"- **Candidate env**: {report['candidate_env']}")
lines.append(f"- **Baseline URL**: {report.get('baseline_url', 'n/a')}")
lines.append(f"- **Candidate URL**: {report.get('candidate_url', 'n/a')}")
lines.append(f"- **Total queries**: {report['total_queries']}")
lines.append(f"- **Baseline errors**: {report['baseline_errors']}")
lines.append(f"- **Candidate errors**: {report['candidate_errors']}")
lines.append("")
lines.append("## Per-Query Results")
lines.append("")
lines.append("| Query ID | Category | Baseline Latency (ms) | Candidate Latency (ms) | Baseline Citations | Candidate Citations | Baseline Error | Candidate Error |")
lines.append("|----------|----------|----------------------|----------------------|-------------------|-------------------|---------------|----------------|")

for r in report["results"]:
    bl_lat = r["baseline"]["latency_ms"] if r["baseline"]["latency_ms"] and r["baseline"]["latency_ms"] >= 0 else "ERROR"
    cd_lat = r["candidate"]["latency_ms"] if r["candidate"]["latency_ms"] and r["candidate"]["latency_ms"] >= 0 else "ERROR"
    bl_err = r["baseline"]["error"] or "none"
    cd_err = r["candidate"]["error"] or "none"
    lines.append(f"| {r['query_id']} | {r['category']} | {bl_lat} | {cd_lat} | {r['baseline']['citation_count']} | {r['candidate']['citation_count']} | {bl_err} | {cd_err} |")

lines.append("")

# Latency stats
def compute_stats(latencies):
    valid = [l for l in latencies if l and l >= 0]
    if not valid:
        return {"p50": "N/A", "p95": "N/A", "mean": "N/A", "count": 0}
    valid.sort()
    n = len(valid)
    p50 = valid[int(0.50 * n)]
    p95 = valid[min(int(0.95 * n), n - 1)]
    mean = int(sum(valid) / n)
    return {"p50": p50, "p95": p95, "mean": mean, "count": n}

bl_latencies = [r["baseline"]["latency_ms"] for r in report["results"]]
cd_latencies = [r["candidate"]["latency_ms"] for r in report["results"]]
bl_stats = compute_stats(bl_latencies)
cd_stats = compute_stats(cd_latencies)

lines.append("## Latency Summary")
lines.append("")
lines.append(f"| Metric | Baseline | Candidate |")
lines.append(f"|--------|----------|-----------|")
lines.append(f"| p50 (ms) | {bl_stats['p50']} | {cd_stats['p50']} |")
lines.append(f"| p95 (ms) | {bl_stats['p95']} | {cd_stats['p95']} |")
lines.append(f"| Mean (ms) | {bl_stats['mean']} | {cd_stats['mean']} |")
lines.append(f"| Valid runs | {bl_stats['count']} | {cd_stats['count']} |")

lines.append("")
lines.append("## Next Steps")
lines.append("")
lines.append("1. Score results: `python3 scripts/eval_score.py $REPORT_FILE`")
lines.append("2. Evaluate gates: `python3 scripts/eval_gate.py $REPORT_FILE`")
lines.append("")

with open(summary_path, "w") as f:
    f.write("\n".join(lines))

print(f"Summary written to: {summary_path}")
PYEOF

echo "Summary: $SUMMARY_FILE"
echo ""
echo "Done. Run scoring and gate evaluation next."
