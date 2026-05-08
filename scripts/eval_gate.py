#!/usr/bin/env python3
"""scripts/eval_gate.py — Gate evaluation for Packet 15 A/B eval runs.

Reads the machine-readable report JSON produced by eval_run.sh (optionally
after being scored by eval_score.py), computes quality lift and p95 latency
increase, evaluates gate thresholds, detects borderline conditions, and
outputs a pass/fail verdict with rollback recommendations.

Usage:
    python3 scripts/eval_gate.py docs/ops/runs/packet-15-eval-report.json

Exit codes:
    0 — All gates pass
    1 — One or more gates fail or borderline
"""

from __future__ import annotations

import json
import sys
import os

# ── Threshold constants (locked decisions) ────────────────────────────────────

QUALITY_LIFT_THRESHOLD = 8.0   # percent — candidate must improve quality by >= 8%
LATENCY_INCREASE_THRESHOLD = 20.0  # percent — candidate p95 latency increase <= 20%

# Borderline margins (locked decisions)
QUALITY_BORDERLINE_MARGIN = 2.0    # ±2% of quality threshold
LATENCY_BORDERLINE_MARGIN = 3.0    # ±3% of latency threshold
PARSE_FAILURE_THRESHOLD = 10.0     # >10% parse/rubric failures → borderline
MIN_SAMPLE_SIZE = 12


def compute_percentile(sorted_values: list[float], percentile: float) -> float | None:
    """Compute the given percentile from a sorted list of values.

    Returns None if the list is empty.
    """
    if not sorted_values:
        return None
    idx = int(percentile * len(sorted_values))
    idx = min(idx, len(sorted_values) - 1)
    return sorted_values[idx]


def compute_latency_stats(latencies: list[int | None]) -> dict:
    """Compute p50 and p95 latency from a list of latencies (None = error).

    Only non-None, non-negative values are included in statistics.
    """
    valid = [l for l in latencies if l is not None and l >= 0]
    if not valid:
        return {"p50": None, "p95": None, "mean": None, "count": 0}
    valid_sorted = sorted(valid)
    return {
        "p50": compute_percentile(valid_sorted, 0.50),
        "p95": compute_percentile(valid_sorted, 0.95),
        "mean": int(sum(valid) / len(valid)),
        "count": len(valid),
    }


def load_report(path: str) -> dict:
    """Load and validate the eval report JSON."""
    if not os.path.isfile(path):
        print(f"ERROR: Report file not found: {path}", file=sys.stderr)
        sys.exit(1)

    with open(path) as f:
        report = json.load(f)

    required_keys = ["baseline_env", "candidate_env", "total_queries", "results"]
    for key in required_keys:
        if key not in report:
            print(f"ERROR: Report missing required key: {key}", file=sys.stderr)
            sys.exit(1)

    return report


def extract_latencies(results: list[dict], env: str) -> list[int | None]:
    """Extract latency values for the given environment from results."""
    return [r[env]["latency_ms"] for r in results]


def extract_rubric_scores(results: list[dict], env: str) -> list[float | None]:
    """Extract per-query rubric scores for the given environment.

    Checks multiple locations for rubric scores:
    1. r["scored"][env]["rubric_score"] (from eval_score.py)
       or r["scored"][env]["mean_rubric_score"] (legacy)
    2. r["judge_output"]["rubric_score"] (top-level judge output)
    3. r[env]["judge_output"] (per-environment judge output in baseline/candidate)

    Falls back to None if not found anywhere.
    """
    scores = []
    for r in results:
        # Check for scored rubric (from eval_score.py)
        scored = r.get("scored", {})
        if env in scored:
            if "rubric_score" in scored[env]:
                scores.append(scored[env]["rubric_score"])
                continue
            if "mean_rubric_score" in scored[env]:
                scores.append(scored[env]["mean_rubric_score"])
                continue
        elif "judge_output" in r:
            # Top-level judge output
            judge = r["judge_output"]
            if isinstance(judge, dict) and "rubric_score" in judge:
                scores.append(judge["rubric_score"])
            else:
                scores.append(None)
        else:
            # Check per-environment (baseline/candidate) judge output
            env_data = r.get(env, {})
            if isinstance(env_data, dict):
                env_judge = env_data.get("judge_output")
                if isinstance(env_judge, dict) and "rubric_score" in env_judge:
                    scores.append(env_judge["rubric_score"])
                elif isinstance(env_judge, dict):
                    # Try to compute rubric_score from 0/1 fields
                    g = env_judge.get("grounded")
                    f = env_judge.get("fact_consistent")
                    c = env_judge.get("complete")
                    if g in (0, 1) and f in (0, 1) and c in (0, 1):
                        scores.append(round((g + f + c) / 3.0, 4))
                    else:
                        scores.append(None)
                else:
                    scores.append(None)
            else:
                scores.append(None)
    return scores


def compute_quality_lift(
    baseline_scores: list[float | None],
    candidate_scores: list[float | None],
) -> dict:
    """Compute quality lift percentage between baseline and candidate.

    Only queries with valid scores in both environments contribute.
    Returns dict with lift_percent, baseline_mean, candidate_mean, valid_count.
    """
    paired = [
        (b, c) for b, c in zip(baseline_scores, candidate_scores)
        if b is not None and c is not None
    ]
    if not paired:
        return {
            "lift_percent": None,
            "baseline_mean": None,
            "candidate_mean": None,
            "valid_count": 0,
            "status": "no_data",
        }

    baseline_vals = [b for b, _ in paired]
    candidate_vals = [c for _, c in paired]

    baseline_mean = sum(baseline_vals) / len(baseline_vals)
    candidate_mean = sum(candidate_vals) / len(candidate_vals)

    if baseline_mean == 0:
        return {
            "lift_percent": None,
            "baseline_mean": baseline_mean,
            "candidate_mean": candidate_mean,
            "valid_count": len(paired),
            "status": "division_by_zero",
        }

    lift_percent = ((candidate_mean - baseline_mean) / baseline_mean) * 100.0

    return {
        "lift_percent": lift_percent,
        "baseline_mean": round(baseline_mean, 4),
        "candidate_mean": round(candidate_mean, 4),
        "valid_count": len(paired),
        "status": "ok",
    }


def compute_latency_increase(
    baseline_latencies: list[int | None],
    candidate_latencies: list[int | None],
) -> dict:
    """Compute p95 latency increase percentage between baseline and candidate.

    Returns dict with increase_percent, baseline_p95, candidate_p95, status.
    """
    bl_stats = compute_latency_stats(baseline_latencies)
    cd_stats = compute_latency_stats(candidate_latencies)

    bl_p95 = bl_stats["p95"]
    cd_p95 = cd_stats["p95"]

    if bl_p95 is None or cd_p95 is None:
        return {
            "increase_percent": None,
            "baseline_p95": bl_p95,
            "candidate_p95": cd_p95,
            "baseline_count": bl_stats["count"],
            "candidate_count": cd_stats["count"],
            "status": "no_data",
        }

    increase_percent = ((cd_p95 - bl_p95) / bl_p95) * 100.0

    return {
        "increase_percent": round(increase_percent, 2),
        "baseline_p95": bl_p95,
        "candidate_p95": cd_p95,
        "baseline_count": bl_stats["count"],
        "candidate_count": cd_stats["count"],
        "status": "ok",
    }


def is_borderline_quality(
    lift_percent: float | None,
) -> bool:
    """Check if quality lift is within borderline range.

    Borderline: quality delta within ±2% of the 8% threshold.
    i.e., 6% <= lift <= 10%
    """
    if lift_percent is None:
        return False
    return (QUALITY_LIFT_THRESHOLD - QUALITY_BORDERLINE_MARGIN) <= lift_percent <= (
        QUALITY_LIFT_THRESHOLD + QUALITY_BORDERLINE_MARGIN
    )


def is_borderline_latency(
    increase_percent: float | None,
) -> bool:
    """Check if p95 latency increase is within borderline range.

    Borderline: latency within ±3% of the 20% threshold.
    i.e., 14% <= increase <= 26%
    """
    if increase_percent is None:
        return False
    return (LATENCY_INCREASE_THRESHOLD - LATENCY_BORDERLINE_MARGIN) <= increase_percent <= (
        LATENCY_INCREASE_THRESHOLD + LATENCY_BORDERLINE_MARGIN
    )


def compute_parse_failures(results: list[dict]) -> dict:
    """Compute the fraction of queries with missing/invalid rubric scores."""
    total = len(results)
    if total == 0:
        return {"failures": 0, "failure_rate": 0.0, "is_borderline": False}

    failures = 0
    for r in results:
        scored = r.get("scored", {})
        has_score = False
        for env_key in ["baseline", "candidate"]:
            if env_key in scored:
                if scored[env_key].get("rubric_score") is not None:
                    has_score = True
                    break
                if scored[env_key].get("mean_rubric_score") is not None:
                    has_score = True
                    break
        if not has_score:
            # Check top-level judge_output
            if "judge_output" in r:
                has_score = True
            else:
                # Check per-environment judge_output
                for env_key in ["baseline", "candidate"]:
                    env_data = r.get(env_key, {})
                    if isinstance(env_data, dict) and "judge_output" in env_data:
                        has_score = True
                        break
        if not has_score:
            failures += 1

    failure_rate = (failures / total) * 100.0
    return {
        "failures": failures,
        "failure_rate": round(failure_rate, 2),
        "is_borderline": failure_rate > PARSE_FAILURE_THRESHOLD,
    }


def compute_graph_activation_metrics(results: list[dict]) -> dict:
    total = len(results)
    if total == 0:
        return {
            "graph_applied_rate": 0.0,
            "average_graph_neighbor_count": 0.0,
            "average_graph_hops_used": 0.0,
        }
    applied = 0
    neighbor_total = 0
    hops_total = 0
    for r in results:
        qs = r.get("candidate", {}).get("query_scope", {}) or {}
        if qs.get("graph_expansion_applied"):
            applied += 1
        neighbor_total += int(qs.get("graph_neighbor_count") or 0)
        hops_total += int(qs.get("graph_hops_used") or 0)
    return {
        "graph_applied_rate": round((applied / total) * 100.0, 2),
        "average_graph_neighbor_count": round(neighbor_total / total, 2),
        "average_graph_hops_used": round(hops_total / total, 2),
    }


def evaluate_gates(report: dict) -> dict:
    """Main gate evaluation logic.

    Computes all metrics, thresholds, borderline status, and verdict.
    """
    results = report["results"]
    total_queries = report["total_queries"]

    # Extract latencies
    bl_latencies = extract_latencies(results, "baseline")
    cd_latencies = extract_latencies(results, "candidate")

    # Extract rubric scores
    bl_scores = extract_rubric_scores(results, "baseline")
    cd_scores = extract_rubric_scores(results, "candidate")

    # Compute quality lift
    quality = compute_quality_lift(bl_scores, cd_scores)

    # Compute latency increase
    latency = compute_latency_increase(bl_latencies, cd_latencies)

    # Compute parse failures
    parse_failures = compute_parse_failures(results)
    graph_metrics = compute_graph_activation_metrics(results)

    sample_size_pass = total_queries >= MIN_SAMPLE_SIZE
    sample_size_detail = (
        f"PASS: sample size {total_queries} >= {MIN_SAMPLE_SIZE}"
        if sample_size_pass else
        f"BORDERLINE: sample size {total_queries} < {MIN_SAMPLE_SIZE}; results are not promotion-grade"
    )

    # Evaluate quality gate
    quality_pass = False
    quality_borderline = False
    quality_detail = ""

    if quality["status"] == "ok" and quality["lift_percent"] is not None:
        if quality["lift_percent"] >= QUALITY_LIFT_THRESHOLD:
            quality_pass = True
            # Also check borderline even when passing
            if is_borderline_quality(quality["lift_percent"]):
                quality_borderline = True
                quality_detail = (
                    f"BORDERLINE: quality lift {quality['lift_percent']:.2f}% "
                    f"(within ±{QUALITY_BORDERLINE_MARGIN}% of {QUALITY_LIFT_THRESHOLD}% threshold)"
                )
            else:
                quality_detail = f"PASS: quality lift {quality['lift_percent']:.2f}% >= {QUALITY_LIFT_THRESHOLD}%"
        elif is_borderline_quality(quality["lift_percent"]):
            quality_borderline = True
            quality_detail = (
                f"BORDERLINE: quality lift {quality['lift_percent']:.2f}% "
                f"(within ±{QUALITY_BORDERLINE_MARGIN}% of {QUALITY_LIFT_THRESHOLD}% threshold)"
            )
        else:
            quality_detail = (
                f"FAIL: quality lift {quality['lift_percent']:.2f}% < {QUALITY_LIFT_THRESHOLD}%"
            )
    else:
        quality_detail = f"FAIL: {quality['status']} (no valid score data)"

    # Evaluate latency gate
    latency_pass = False
    latency_borderline = False
    latency_detail = ""

    if latency["status"] == "ok" and latency["increase_percent"] is not None:
        if latency["increase_percent"] <= LATENCY_INCREASE_THRESHOLD:
            latency_pass = True
            # Also check borderline even when passing
            if is_borderline_latency(latency["increase_percent"]):
                latency_borderline = True
                latency_detail = (
                    f"BORDERLINE: p95 latency increase {latency['increase_percent']:.2f}% "
                    f"(within ±{LATENCY_BORDERLINE_MARGIN}% of {LATENCY_INCREASE_THRESHOLD}% threshold)"
                )
            else:
                latency_detail = (
                    f"PASS: p95 latency increase {latency['increase_percent']:.2f}% <= "
                    f"{LATENCY_INCREASE_THRESHOLD}%"
                )
        elif is_borderline_latency(latency["increase_percent"]):
            latency_borderline = True
            latency_detail = (
                f"BORDERLINE: p95 latency increase {latency['increase_percent']:.2f}% "
                f"(within ±{LATENCY_BORDERLINE_MARGIN}% of {LATENCY_INCREASE_THRESHOLD}% threshold)"
            )
        else:
            latency_detail = (
                f"FAIL: p95 latency increase {latency['increase_percent']:.2f}% > "
                f"{LATENCY_INCREASE_THRESHOLD}%"
            )
    else:
        latency_detail = f"FAIL: {latency['status']} (no valid latency data)"

    # Parse failures borderline
    parse_borderline = parse_failures["is_borderline"]
    parse_detail = (
        f"BORDERLINE: {parse_failures['failure_rate']:.2f}% parse/rubric failures > "
        f"{PARSE_FAILURE_THRESHOLD}%"
    )
    if not parse_borderline:
        parse_detail = (
            f"OK: {parse_failures['failure_rate']:.2f}% parse/rubric failures <= "
            f"{PARSE_FAILURE_THRESHOLD}%"
        )

    # Determine overall verdict
    failed_criteria = []
    if not sample_size_pass:
        failed_criteria.append("minimum_sample_size")
    if not quality_pass:
        failed_criteria.append("quality_lift")
    if not latency_pass:
        failed_criteria.append("p95_latency")
    if parse_borderline:
        failed_criteria.append("parse_failures")

    overall_pass = len(failed_criteria) == 0 and not quality_borderline and not latency_borderline
    is_borderline = quality_borderline or latency_borderline or parse_borderline or (not sample_size_pass)

    # Generate rollback recommendation
    rollback = generate_rollback_recommendation(
        failed_criteria, quality, latency, parse_failures
    )

    verdict = {
        "run_id": report.get("run_id", "packet-15"),
        "baseline_env": report["baseline_env"],
        "candidate_env": report["candidate_env"],
        "total_queries": total_queries,
        "thresholds": {
            "quality_lift_percent": QUALITY_LIFT_THRESHOLD,
            "p95_latency_increase_percent": LATENCY_INCREASE_THRESHOLD,
            "parse_failure_percent": PARSE_FAILURE_THRESHOLD,
        },
        "metrics": {
            "quality_lift": quality,
            "latency_increase": latency,
            "parse_failures": parse_failures,
            "graph_activation": graph_metrics,
        },
        "criteria": {
            "minimum_sample_size": {
                "pass": sample_size_pass,
                "borderline": not sample_size_pass,
                "detail": sample_size_detail,
            },
            "quality_lift": {
                "pass": quality_pass,
                "borderline": quality_borderline,
                "detail": quality_detail,
            },
            "p95_latency": {
                "pass": latency_pass,
                "borderline": latency_borderline,
                "detail": latency_detail,
            },
            "parse_failures": {
                "pass": not parse_borderline,
                "borderline": parse_borderline,
                "detail": parse_detail,
            },
        },
        "verdict": {
            "overall_pass": overall_pass,
            "is_borderline": is_borderline,
            "failed_criteria": failed_criteria,
            "rollback_recommendation": rollback,
        },
    }

    return verdict


def generate_rollback_recommendation(
    failed_criteria: list[str],
    quality: dict,
    latency: dict,
    parse_failures: dict,
) -> str:
    """Generate a human-readable rollback recommendation.

    On fail: explicit failed criteria, no baseline update, rollback recommendation.
    """
    if not failed_criteria:
        return "No rollback needed. All gates passed."

    if quality["status"] == "no_data" or latency["status"] == "no_data":
        return (
            "Rollback recommended: Insufficient data to evaluate gates. "
            "Check gateway availability and retry the eval run."
        )

    parts = []
    parts.append("Baseline NOT updated. Rollback recommended.")

    if "quality_lift" in failed_criteria:
        lift = quality.get("lift_percent", "N/A")
        parts.append(
            f"  - Quality lift ({lift}%) did not meet threshold "
            f"({QUALITY_LIFT_THRESHOLD}%). The candidate configuration "
            f"does not improve retrieval quality. Revert to baseline config."
        )

    if "p95_latency" in failed_criteria:
        inc = latency.get("increase_percent", "N/A")
        parts.append(
            f"  - p95 latency increase ({inc}%) exceeded threshold "
            f"({LATENCY_INCREASE_THRESHOLD}%). The candidate configuration "
            f"adds unacceptable latency. Revert to baseline config."
        )

    if "parse_failures" in failed_criteria:
        rate = parse_failures.get("failure_rate", "N/A")
        parts.append(
            f"  - Parse/rubric failure rate ({rate}%) exceeds threshold "
            f"({PARSE_FAILURE_THRESHOLD}%). Review query scoring pipeline."
        )

    parts.append(
        "Action: Do not promote candidate baseline. Investigate failed criteria "
        "before retrying the eval run."
    )

    return "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/eval_gate.py <report.json>", file=sys.stderr)
        sys.exit(1)

    report_path = sys.argv[1]
    report = load_report(report_path)

    print("=== Packet 15: Gate Evaluation ===")
    print(f"Baseline: {report['baseline_env']}")
    print(f"Candidate: {report['candidate_env']}")
    print(f"Queries: {report['total_queries']}")
    print()

    verdict = evaluate_gates(report)

    # Print summary to stdout
    print("--- Criteria ---")
    for name, crit in verdict["criteria"].items():
        status = "PASS" if crit["pass"] and not crit["borderline"] else (
            "BORDERLINE" if crit["borderline"] else "FAIL"
        )
        print(f"  {name}: {status}")
        print(f"    {crit['detail']}")
    print()

    v = verdict["verdict"]
    if v["overall_pass"]:
        print(f"VERDICT: PASS")
        print(f"  All gates passed. Candidate baseline may be promoted.")
    else:
        print(f"VERDICT: {'BORDERLINE' if v['is_borderline'] else 'FAIL'}")
        if v["is_borderline"]:
            print(f"  Borderline conditions detected. Human spot-review recommended.")
        if v["failed_criteria"]:
            print(f"  Failed criteria: {', '.join(v['failed_criteria'])}")
        print(f"  {verdict['verdict']['rollback_recommendation']}")

    # Write verdict JSON to file
    output_path = report_path.replace(".json", "-gate-verdict.json")
    with open(output_path, "w") as f:
        json.dump(verdict, f, indent=2)
    print(f"\nVerdict written to: {output_path}")

    # Exit code: 0 for pass, 1 for fail/borderline
    sys.exit(0 if v["overall_pass"] else 1)


if __name__ == "__main__":
    main()
