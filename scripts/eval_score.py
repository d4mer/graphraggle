#!/usr/bin/env python3
"""scripts/eval_score.py — Rubric scoring for Packet 15 A/B eval reports.

Reads the machine-readable report JSON produced by eval_run.sh and scores
each query result against the evaluation rubric.

Rubric fields (per query, per environment):
    grounded:       0 or 1 — answer is supported by retrieved citations
    fact_consistent: 0 or 1 — answer does not contradict citation facts
    complete:        0 or 1 — answer addresses all query aspects

Scoring modes:
    1. Judge output: If each result has a 'judge_output' field with structured
       JSON from a model-judge, parse rubric scores from it.
    2. Heuristic fallback: If no judge output, apply keyword-based heuristics
       as a deterministic fallback (for CI without model-judge access).

Usage:
    python3 scripts/eval_score.py <report.json>

Output:
    Same JSON file with 'scored' field added per result.
"""

from __future__ import annotations

import json
import sys
import os
import re
import urllib.request
import urllib.error


# ── Judge output parser ──────────────────────────────────────────────────────

def parse_judge_output(judge_output: dict) -> dict | None:
    """Parse rubric scores from model-judge structured JSON output.

    Expected judge_output format:
        {
            "grounded": 0 or 1,
            "fact_consistent": 0 or 1,
            "complete": 0 or 1,
            "reasoning": "optional explanation"
        }

    Returns the rubric dict, or None if parsing fails.
    """
    if not isinstance(judge_output, dict):
        return None

    try:
        grounded = int(judge_output.get("grounded", -1))
        fact_consistent = int(judge_output.get("fact_consistent", -1))
        complete = int(judge_output.get("complete", -1))

        # Validate: all must be 0 or 1
        if grounded not in (0, 1) or fact_consistent not in (0, 1) or complete not in (0, 1):
            return None

        rubric_score = (grounded + fact_consistent + complete) / 3.0

        return {
            "grounded": grounded,
            "fact_consistent": fact_consistent,
            "complete": complete,
            "rubric_score": round(rubric_score, 4),
            "reasoning": judge_output.get("reasoning", judge_output.get("reason", "")),
        }
    except (ValueError, TypeError):
        return None


# ── Heuristic scorer (deterministic fallback) ────────────────────────────────

def heuristic_score(
    answer: str,
    citations: list,
    query: str,
    graph_evidence: list | None = None,
    combined_evidence: list | None = None,
) -> dict:
    """Apply heuristic scoring when no judge output is available.

    This is a deterministic, keyword-based fallback for CI environments
    without access to a model-judge. It is not a substitute for actual
    rubric scoring but provides a baseline metric.
    """
    answer_lower = answer.lower() if answer else ""
    query_lower = query.lower() if query else ""
    has_evidence = bool(citations) or bool(graph_evidence) or bool(combined_evidence)

    # Grounded: answer references something in citations (has citations + non-empty)
    grounded = 1 if has_evidence and len(answer_lower) > 10 else 0

    # Fact consistent: no obvious contradiction markers
    contradiction_markers = [
        "contradicts", "is not", "does not match", "incorrect", "wrong",
        "disagree", "no evidence", "no mention"
    ]
    fact_consistent = 0 if any(m in answer_lower for m in contradiction_markers) else 1

    # Complete: answer has substantial length relative to query
    # Heuristic: answer length >= 2x query length suggests completeness
    complete = 1 if len(answer_lower) >= len(query_lower) * 2 and len(answer_lower) > 50 else 0

    rubric_score = (grounded + fact_consistent + complete) / 3.0

    return {
        "grounded": grounded,
        "fact_consistent": fact_consistent,
        "complete": complete,
        "rubric_score": round(rubric_score, 4),
        "method": "heuristic",
    }


def score_with_judge_model(
    query: str,
    answer: str,
    citations: list,
    expected_topics: list | None = None,
    graph_evidence: list | None = None,
    combined_evidence: list | None = None,
) -> dict | None:
    enabled = os.getenv("EVAL_JUDGE_ENABLED", "false").lower() == "true"
    if not enabled:
        return None

    judge_url = os.getenv("EVAL_JUDGE_URL", "").strip()
    judge_model = os.getenv("EVAL_JUDGE_MODEL", "").strip()
    judge_key = os.getenv("EVAL_JUDGE_API_KEY", "").strip()
    if not judge_url or not judge_model:
        return None

    prompt = (
        "You are a strict evaluator for a retrieval-augmented answer. Return ONLY JSON with keys grounded, fact_consistent, complete, reason. "
        "Each score must be 0 or 1. Grounded means the answer is directly supported by the provided evidence. "
        "Complete means the answer covers the expected topics when they are provided.\n\n"
        f"Query: {query}\n"
        f"Expected topics: {json.dumps(expected_topics or [], ensure_ascii=True)}\n"
        f"Answer: {answer}\n"
        f"Citations: {json.dumps(citations, ensure_ascii=True)[:8000]}\n"
        f"Graph evidence: {json.dumps(graph_evidence or [], ensure_ascii=True)[:8000]}\n"
        f"Combined evidence: {json.dumps(combined_evidence or [], ensure_ascii=True)[:8000]}\n"
    )

    payload = {
        "model": judge_model,
        "messages": [
            {"role": "system", "content": "Return strict JSON only."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }

    headers = {"Content-Type": "application/json"}
    if judge_key:
        headers["Authorization"] = f"Bearer {judge_key}"

    try:
        req = urllib.request.Request(
            judge_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None

    try:
        content = data["choices"][0]["message"]["content"]
    except Exception:
        return None

    if not isinstance(content, str) or not content.strip():
        return None

    text = content.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None

    try:
        parsed = json.loads(text[start:end + 1])
    except Exception:
        return None

    rubric = parse_judge_output(parsed)
    if rubric is None:
        return None
    rubric["method"] = "judge_model"
    return rubric


# ── Aggregation ───────────────────────────────────────────────────────────────

def aggregate_scores(
    baseline_scores: list[dict | None],
    candidate_scores: list[dict | None],
) -> dict:
    """Aggregate per-query rubric scores into summary statistics.

    Returns a dict with mean_rubric_score, per-field rates, total queries,
    and parse failures count.
    """
    valid_baseline = [s for s in baseline_scores if s is not None]
    valid_candidate = [s for s in candidate_scores if s is not None]

    def compute_stats(scores: list[dict]) -> dict:
        if not scores:
            return {
                "mean_rubric_score": 0.0,
                "grounded_rate": 0.0,
                "fact_consistent_rate": 0.0,
                "complete_rate": 0.0,
            }

        n = len(scores)
        mean_score = sum(s["rubric_score"] for s in scores) / n
        grounded_rate = sum(s["grounded"] for s in scores) / n
        fact_rate = sum(s["fact_consistent"] for s in scores) / n
        complete_rate = sum(s["complete"] for s in scores) / n

        return {
            "mean_rubric_score": round(mean_score, 4),
            "grounded_rate": round(grounded_rate, 4),
            "fact_consistent_rate": round(fact_rate, 4),
            "complete_rate": round(complete_rate, 4),
        }

    return {
        "baseline": compute_stats(valid_baseline),
        "candidate": compute_stats(valid_candidate),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def score_report(report_path: str) -> dict:
    """Score all queries in the report and return the updated report."""
    if not os.path.isfile(report_path):
        print(f"ERROR: Report file not found: {report_path}", file=sys.stderr)
        sys.exit(1)

    with open(report_path, "r", encoding="utf-8") as f:
        report = json.load(f)

    results = report.get("results", [])
    baseline_scores = []
    candidate_scores = []

    scored_results = []

    for r in results:
        query_text = r.get("query", "")
        expected_topics = r.get("expected_topics", [])
        bl = r.get("baseline", {})
        cd = r.get("candidate", {})
        citations_bl = bl.get("citations", [])
        citations_cd = cd.get("citations", [])
        graph_bl = bl.get("graph_evidence", [])
        graph_cd = cd.get("graph_evidence", [])
        combined_bl = bl.get("combined_evidence", [])
        combined_cd = cd.get("combined_evidence", [])

        scored = {}

        # Score baseline
        bl_judge = r.get("judge_output", {})
        # Check per-environment judge output
        bl_judge = bl.get("judge_output", {}) if isinstance(bl, dict) else {}
        cd_judge = cd.get("judge_output", {}) if isinstance(cd, dict) else {}

        # Try judge output first, fall back to heuristic
        bl_rubric = parse_judge_output(bl_judge)
        if bl_rubric is None:
            bl_rubric = score_with_judge_model(
                query_text,
                bl.get("answer", ""),
                citations_bl,
                expected_topics,
                graph_bl,
                combined_bl,
            )
        if bl_rubric is None:
            bl_rubric = heuristic_score(
                bl.get("answer", ""), citations_bl, query_text, graph_bl, combined_bl
            )
        baseline_scores.append(bl_rubric)
        scored["baseline"] = bl_rubric

        cd_rubric = parse_judge_output(cd_judge)
        if cd_rubric is None:
            cd_rubric = score_with_judge_model(
                query_text,
                cd.get("answer", ""),
                citations_cd,
                expected_topics,
                graph_cd,
                combined_cd,
            )
        if cd_rubric is None:
            cd_rubric = heuristic_score(
                cd.get("answer", ""), citations_cd, query_text, graph_cd, combined_cd
            )
        candidate_scores.append(cd_rubric)
        scored["candidate"] = cd_rubric

        scored_results.append({**r, "scored": scored})

    # Aggregate
    aggregation = aggregate_scores(baseline_scores, candidate_scores)
    total = len(results)
    bl_parse_failures = sum(1 for s in baseline_scores if s is None)
    cd_parse_failures = sum(1 for s in candidate_scores if s is None)

    report["scored"] = {
        "aggregation": aggregation,
        "baseline_parse_failures": bl_parse_failures,
        "candidate_parse_failures": cd_parse_failures,
    }
    report["results"] = scored_results

    # Write back to the same file (in-place update)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    return report


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 scripts/eval_score.py <report.json>", file=sys.stderr)
        sys.exit(1)

    report_path = sys.argv[1]
    scored_report = score_report(report_path)

    agg = scored_report["scored"]["aggregation"]
    print("=== Packet 15: Rubric Scoring ===")
    print(f"Total queries: {scored_report['total_queries']}")
    print()

    print("Baseline:")
    for k, v in agg["baseline"].items():
        print(f"  {k}: {v}")
    print()

    print("Candidate:")
    for k, v in agg["candidate"].items():
        print(f"  {k}: {v}")
    print()

    print(f"Baseline parse failures: {scored_report['scored']['baseline_parse_failures']}")
    print(f"Candidate parse failures: {scored_report['scored']['candidate_parse_failures']}")
    print(f"\nScored report written to: {report_path}")


if __name__ == "__main__":
    main()
