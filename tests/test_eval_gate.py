"""Tests for Packet 15: eval_gate.py gate evaluation logic.

Covers:
- Threshold evaluation: quality lift >= 8%, p95 latency increase <= 20%
- Borderline detection: quality ±2% of threshold, latency ±3% of threshold
- Parse/rubric failure detection: >10% failures
- Pass/fail verdict computation
- Rollback recommendation generation
- Edge cases: no data, division by zero, None values
"""

from __future__ import annotations

import json
import sys
import unittest
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scripts.eval_gate as gate_module


def _make_report(
    total_queries: int = 8,
    baseline_scores: list | None = None,
    candidate_scores: list | None = None,
    baseline_latencies: list | None = None,
    candidate_latencies: list | None = None,
    baseline_errors: int = 0,
    candidate_errors: int = 0,
) -> dict:
    """Build a minimal eval report dict for testing.

    Includes judge_output in each baseline/candidate dict so that
    extract_rubric_scores can find the scores.

    Scores are lists of float rubric scores (0.0–1.0). Each score is
    converted into a judge_output rubric dict with three 0/1 fields.
    None values in the scores list produce no judge_output (parse failure).
    """
    if baseline_scores is None:
        baseline_scores = [0.6667] * total_queries
    if candidate_scores is None:
        candidate_scores = [0.6667] * total_queries
    if baseline_latencies is None:
        baseline_latencies = [1000] * total_queries
    if candidate_latencies is None:
        candidate_latencies = [1000] * total_queries

    def score_to_rubric(score):
        """Convert a float score to a judge_output rubric dict.

        Maps score to nearest valid 0/1 combination:
          0.0 → 0,0,0 (score 0.0)
          0.333 → 1,0,0 (score 0.333)
          0.667 → 1,1,0 (score 0.667)
          1.0 → 1,1,1 (score 1.0)
        """
        if score is None:
            return None
        total = round(score * 3)
        total = max(0, min(3, total))
        return {
            "grounded": 1,
            "fact_consistent": 1 if total >= 2 else 0,
            "complete": 1 if total >= 3 else 0,
        }

    results = []
    for i in range(total_queries):
        bl_score = baseline_scores[i]
        cd_score = candidate_scores[i]

        bl_rubric = score_to_rubric(bl_score)
        cd_rubric = score_to_rubric(cd_score)

        baseline_entry = {
            "latency_ms": baseline_latencies[i],
            "answer": "baseline answer",
            "citation_count": 3,
            "error": None,
        }
        candidate_entry = {
            "latency_ms": candidate_latencies[i],
            "answer": "candidate answer",
            "citation_count": 4,
            "error": None,
        }
        if bl_rubric is not None:
            baseline_entry["judge_output"] = bl_rubric
        if cd_rubric is not None:
            candidate_entry["judge_output"] = cd_rubric

        results.append({
            "query_id": f"q{i+1}",
            "query": f"Test query {i+1}",
            "category": "test",
            "baseline": baseline_entry,
            "candidate": candidate_entry,
        })

    return {
        "run_id": "packet-15",
        "baseline_env": "baseline",
        "candidate_env": "candidate",
        "gateway_url": "http://localhost:8000",
        "total_queries": total_queries,
        "baseline_errors": baseline_errors,
        "candidate_errors": candidate_errors,
        "results": results,
    }


# ── Percentile computation tests ─────────────────────────────────────────────

class TestComputePercentile(unittest.TestCase):
    """Test compute_percentile helper."""

    def test_empty_list(self):
        self.assertIsNone(gate_module.compute_percentile([], 0.5))

    def test_single_value(self):
        self.assertEqual(gate_module.compute_percentile([42], 0.95), 42)

    def test_p50_even(self):
        vals = [10, 20, 30, 40]
        # int(0.50 * 4) = 2, vals[2] = 30
        self.assertEqual(gate_module.compute_percentile(vals, 0.5), 30)

    def test_p95_large(self):
        vals = list(range(1, 101))  # 1..100
        idx = int(0.95 * 100)
        idx = min(idx, 99)
        self.assertEqual(gate_module.compute_percentile(vals, 0.95), vals[idx])


# ── Latency stats tests ──────────────────────────────────────────────────────

class TestComputeLatencyStats(unittest.TestCase):
    """Test compute_latency_stats helper."""

    def test_all_valid(self):
        stats = gate_module.compute_latency_stats([100, 200, 300, 400, 500])
        self.assertEqual(stats["count"], 5)
        self.assertIsNotNone(stats["p50"])
        self.assertIsNotNone(stats["p95"])

    def test_with_errors(self):
        stats = gate_module.compute_latency_stats([100, None, 300, -1, 500])
        self.assertEqual(stats["count"], 3)
        self.assertIsNotNone(stats["p50"])

    def test_all_errors(self):
        stats = gate_module.compute_latency_stats([None, None])
        self.assertEqual(stats["count"], 0)
        self.assertIsNone(stats["p50"])
        self.assertIsNone(stats["p95"])


# ── Quality lift computation tests ───────────────────────────────────────────

class TestComputeQualityLift(unittest.TestCase):
    """Test compute_quality_lift helper."""

    def test_perfect_lift(self):
        """Baseline 0.5, candidate 1.0 → 100% lift."""
        result = gate_module.compute_quality_lift([0.5, 0.5], [1.0, 1.0])
        self.assertEqual(result["lift_percent"], 100.0)
        self.assertEqual(result["baseline_mean"], 0.5)
        self.assertEqual(result["candidate_mean"], 1.0)
        self.assertEqual(result["status"], "ok")

    def test_no_lift(self):
        """Same scores → 0% lift."""
        result = gate_module.compute_quality_lift([0.6667, 0.6667], [0.6667, 0.6667])
        self.assertAlmostEqual(result["lift_percent"], 0.0, places=2)

    def test_negative_lift(self):
        """Candidate worse than baseline."""
        result = gate_module.compute_quality_lift([0.8], [0.4])
        self.assertAlmostEqual(result["lift_percent"], -50.0, places=1)

    def test_none_scores_ignored(self):
        """None values in paired lists are excluded."""
        result = gate_module.compute_quality_lift([0.5, None, 0.7], [0.8, 0.9, 0.6])
        self.assertEqual(result["valid_count"], 2)
        self.assertEqual(result["status"], "ok")

    def test_no_valid_data(self):
        """All None → no_data status."""
        result = gate_module.compute_quality_lift([None, None], [None, None])
        self.assertEqual(result["status"], "no_data")

    def test_division_by_zero(self):
        """Baseline mean of 0 → division_by_zero status."""
        result = gate_module.compute_quality_lift([0.0, 0.0], [0.5, 0.5])
        self.assertEqual(result["status"], "division_by_zero")


# ── Latency increase computation tests ───────────────────────────────────────

class TestComputeLatencyIncrease(unittest.TestCase):
    """Test compute_latency_increase helper."""

    def test_latency_increase(self):
        """Baseline p95=1000, candidate p95=1200 → 20% increase."""
        bl_lats = [1000] * 100
        cd_lats = [1200] * 100
        result = gate_module.compute_latency_increase(bl_lats, cd_lats)
        self.assertEqual(result["increase_percent"], 20.0)
        self.assertEqual(result["status"], "ok")

    def test_latency_decrease(self):
        """Candidate faster → negative increase."""
        bl_lats = [2000] * 100
        cd_lats = [1500] * 100
        result = gate_module.compute_latency_increase(bl_lats, cd_lats)
        self.assertAlmostEqual(result["increase_percent"], -25.0, places=1)

    def test_no_valid_data(self):
        """All None latencies → no_data."""
        result = gate_module.compute_latency_increase([None, None], [None, None])
        self.assertEqual(result["status"], "no_data")


# ── Borderline detection tests ───────────────────────────────────────────────

class TestBorderlineDetection(unittest.TestCase):
    """Test borderline detection logic."""

    def test_quality_exactly_threshold(self):
        """8% lift → borderline (within ±2 percentage points of 8% threshold = [6%, 10%])."""
        self.assertTrue(gate_module.is_borderline_quality(8.0))

    def test_quality_borderline_below(self):
        """6% lift → borderline (2% below threshold)."""
        self.assertTrue(gate_module.is_borderline_quality(6.0))

    def test_quality_borderline_above(self):
        """10% lift → borderline (2% above threshold)."""
        self.assertTrue(gate_module.is_borderline_quality(10.0))

    def test_quality_borderline_mid(self):
        """8% lift → borderline range 6-10, so 8% IS borderline."""
        # Actually 8% is within [6, 10], so it IS borderline
        self.assertTrue(gate_module.is_borderline_quality(8.0))

    def test_quality_clear_pass(self):
        """15% lift → clearly above borderline."""
        self.assertFalse(gate_module.is_borderline_quality(15.0))

    def test_quality_clear_fail(self):
        """3% lift → clearly below borderline."""
        self.assertFalse(gate_module.is_borderline_quality(3.0))

    def test_latency_borderline_at_threshold(self):
        """20% increase → borderline (within ±3% of 20%)."""
        self.assertTrue(gate_module.is_borderline_latency(20.0))

    def test_latency_borderline_below(self):
        """17% increase → borderline (within ±3 percentage points of 20% threshold = [17%, 23%])."""
        self.assertTrue(gate_module.is_borderline_latency(17.0))

    def test_latency_borderline_above(self):
        """23% increase → borderline (within ±3 percentage points of 20% threshold = [17%, 23%])."""
        self.assertTrue(gate_module.is_borderline_latency(23.0))

    def test_latency_clear_pass(self):
        """10% increase → clearly below threshold."""
        self.assertFalse(gate_module.is_borderline_latency(10.0))

    def test_latency_clear_fail(self):
        """35% increase → clearly above threshold."""
        self.assertFalse(gate_module.is_borderline_latency(35.0))

    def test_none_values(self):
        """None → not borderline."""
        self.assertFalse(gate_module.is_borderline_quality(None))
        self.assertFalse(gate_module.is_borderline_latency(None))


# ── Parse failures tests ─────────────────────────────────────────────────────

class TestParseFailures(unittest.TestCase):
    """Test compute_parse_failures helper."""

    def test_no_failures(self):
        """All queries have scores → 0 failures."""
        results = [
            {
                "scored": {"baseline": {"mean_rubric_score": 0.8}},
            }
            for _ in range(10)
        ]
        result = gate_module.compute_parse_failures(results)
        self.assertEqual(result["failures"], 0)
        self.assertEqual(result["failure_rate"], 0.0)
        self.assertFalse(result["is_borderline"])

    def test_ten_percent_failures(self):
        """1 out of 10 missing → 10% → not borderline (not > 10%)."""
        results = [
            {"scored": {"baseline": {"mean_rubric_score": 0.8}}},
        ] * 9 + [{}]
        result = gate_module.compute_parse_failures(results)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["failure_rate"], 10.0)
        self.assertFalse(result["is_borderline"])

    def test_over_ten_percent_failures(self):
        """2 out of 10 missing → 20% → borderline."""
        results = [
            {"scored": {"baseline": {"mean_rubric_score": 0.8}}},
        ] * 8 + [{}, {}]
        result = gate_module.compute_parse_failures(results)
        self.assertEqual(result["failures"], 2)
        self.assertEqual(result["failure_rate"], 20.0)
        self.assertTrue(result["is_borderline"])

    def test_empty_results(self):
        """Empty results → 0 failures."""
        result = gate_module.compute_parse_failures([])
        self.assertEqual(result["failures"], 0)
        self.assertEqual(result["failure_rate"], 0.0)


# ── Full gate evaluation tests ───────────────────────────────────────────────

class TestGateEvaluation(unittest.TestCase):
    """Test full evaluate_gates pipeline."""

    def test_clear_pass(self):
        """Quality lift well above 8%, latency well below 20% → PASS."""
        # Baseline: score=0.333 (rubric 1,0,0 → score 0.333), p95=1000ms
        # Candidate: score=0.667 (rubric 1,1,0 → score 0.667), p95=1100ms
        # Lift = (0.667-0.333)/0.333 ≈ 100%, latency increase = 10%
        report = _make_report(
            total_queries=12,
            baseline_scores=[0.333] * 12,
            candidate_scores=[0.667] * 12,
            baseline_latencies=[1000] * 12,
            candidate_latencies=[1100] * 12,
        )
        verdict = gate_module.evaluate_gates(report)
        self.assertTrue(verdict["verdict"]["overall_pass"])
        self.assertFalse(verdict["verdict"]["is_borderline"])
        self.assertEqual(verdict["verdict"]["failed_criteria"], [])

    def test_quality_fail(self):
        """Quality lift below 8% → FAIL on quality."""
        report = _make_report(
            total_queries=10,
            baseline_scores=[0.5] * 10,
            candidate_scores=[0.52] * 10,  # ~4% lift
            baseline_latencies=[1000] * 10,
            candidate_latencies=[1050] * 10,
        )
        verdict = gate_module.evaluate_gates(report)
        self.assertFalse(verdict["verdict"]["overall_pass"])
        self.assertIn("quality_lift", verdict["verdict"]["failed_criteria"])
        self.assertFalse(verdict["criteria"]["quality_lift"]["pass"])

    def test_latency_fail(self):
        """p95 latency increase > 20% → FAIL on latency."""
        report = _make_report(
            total_queries=10,
            baseline_scores=[0.5] * 10,
            candidate_scores=[0.8] * 10,  # Good quality
            baseline_latencies=[1000] * 10,
            candidate_latencies=[1300] * 10,  # 30% increase
        )
        verdict = gate_module.evaluate_gates(report)
        self.assertFalse(verdict["verdict"]["overall_pass"])
        self.assertIn("p95_latency", verdict["verdict"]["failed_criteria"])
        self.assertFalse(verdict["criteria"]["p95_latency"]["pass"])

    def test_both_fail(self):
        """Both quality and latency fail → FAIL with rollback recommendation."""
        results = []
        for i in range(10):
            results.append({
                "query_id": f"q{i+1}",
                "query": f"Test query {i+1}",
                "category": "test",
                "baseline": {
                    "latency_ms": 1000,
                    "answer": "baseline answer",
                    "citation_count": 3,
                    "error": None,
                    # score 0.333 (rubric 1,0,0)
                    "judge_output": {"grounded": 1, "fact_consistent": 0, "complete": 0},
                },
                "candidate": {
                    "latency_ms": 1300,
                    "answer": "candidate answer",
                    "citation_count": 3,
                    "error": None,
                    # score 0.333 (rubric 1,0,0) — same as baseline, 0% lift → fail
                    "judge_output": {"grounded": 1, "fact_consistent": 0, "complete": 0},
                },
            })
        report = {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 10,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }
        verdict = gate_module.evaluate_gates(report)
        self.assertFalse(verdict["verdict"]["overall_pass"])
        self.assertIn("quality_lift", verdict["verdict"]["failed_criteria"])
        self.assertIn("p95_latency", verdict["verdict"]["failed_criteria"])
        self.assertIn("Baseline NOT updated", verdict["verdict"]["rollback_recommendation"])

    def test_quality_borderline(self):
        """Quality lift in borderline range → BORDERLINE."""
        # Construct report directly to get precise borderline values.
        # baseline: 8 at 0.667 (rubric 1,1,0), 2 at 0.0 (rubric 0,0,0) → mean 0.5336
        # candidate: 8 at 0.667, 1 at 0.0, 1 at 0.333 (rubric 1,0,0) → mean 0.5669
        # lift = 6.24% → within [6%, 10%] borderline range
        results = []
        for i in range(10):
            if i < 8:
                bl_judge = {"grounded": 1, "fact_consistent": 1, "complete": 0}  # 0.667
                cd_judge = {"grounded": 1, "fact_consistent": 1, "complete": 0}  # 0.667
            elif i == 8:
                bl_judge = {"grounded": 0, "fact_consistent": 0, "complete": 0}  # 0.0
                cd_judge = {"grounded": 0, "fact_consistent": 0, "complete": 0}  # 0.0
            else:
                bl_judge = {"grounded": 0, "fact_consistent": 0, "complete": 0}  # 0.0
                cd_judge = {"grounded": 1, "fact_consistent": 0, "complete": 0}  # 0.333

            results.append({
                "query_id": f"q{i+1}",
                "query": f"Test query {i+1}",
                "category": "test",
                "baseline": {
                    "latency_ms": 1000,
                    "answer": "baseline answer",
                    "citation_count": 3,
                    "error": None,
                    "judge_output": bl_judge,
                },
                "candidate": {
                    "latency_ms": 1050,
                    "answer": "candidate answer",
                    "citation_count": 3,
                    "error": None,
                    "judge_output": cd_judge,
                },
            })
        report = {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 10,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }
        verdict = gate_module.evaluate_gates(report)
        self.assertTrue(verdict["verdict"]["is_borderline"])
        self.assertTrue(verdict["criteria"]["quality_lift"]["borderline"])

    def test_latency_borderline(self):
        """p95 latency at borderline range → BORDERLINE."""
        # Use scores that map to valid rubrics
        # baseline: 0.333 (rubric 1,0,0), candidate: 0.667 (rubric 1,1,0)
        # latency: baseline p95=1000, candidate p95=2000 → 100% increase → clearly fail
        # But we want borderline. Need p95 in [17%, 23%] range.
        # candidate p95=1200 → 20% increase → borderline
        results = []
        for i in range(10):
            results.append({
                "query_id": f"q{i+1}",
                "query": f"Test query {i+1}",
                "category": "test",
                "baseline": {
                    "latency_ms": 1000,
                    "answer": "baseline answer",
                    "citation_count": 3,
                    "error": None,
                    "judge_output": {"grounded": 1, "fact_consistent": 0, "complete": 0},
                },
                "candidate": {
                    "latency_ms": 1200,
                    "answer": "candidate answer",
                    "citation_count": 4,
                    "error": None,
                    "judge_output": {"grounded": 1, "fact_consistent": 1, "complete": 0},
                },
            })
        report = {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 10,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }
        verdict = gate_module.evaluate_gates(report)
        self.assertTrue(verdict["verdict"]["is_borderline"])
        self.assertTrue(verdict["criteria"]["p95_latency"]["borderline"])

    def test_parse_failures_borderline(self):
        """>10% parse failures → borderline."""
        results = []
        for i in range(10):
            if i < 7:
                # 70% have scores
                results.append({
                    "scored": {"baseline": {"mean_rubric_score": 0.5}},
                    "query_id": f"q{i+1}",
                    "query": f"q{i+1}",
                    "category": "test",
                    "baseline": {"latency_ms": 1000, "answer": "a", "citation_count": 0, "error": None},
                    "candidate": {"latency_ms": 1000, "answer": "a", "citation_count": 0, "error": None},
                })
            else:
                # 30% have no scores
                results.append({
                    "query_id": f"q{i+1}",
                    "query": f"q{i+1}",
                    "category": "test",
                    "baseline": {"latency_ms": 1000, "answer": "a", "citation_count": 0, "error": None},
                    "candidate": {"latency_ms": 1000, "answer": "a", "citation_count": 0, "error": None},
                })

        report = {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 10,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }
        verdict = gate_module.evaluate_gates(report)
        self.assertTrue(verdict["verdict"]["is_borderline"])
        self.assertIn("parse_failures", verdict["verdict"]["failed_criteria"])

    def test_no_data_status(self):
        """No valid score data → overall fail."""
        report = _make_report(
            total_queries=10,
            baseline_scores=[None] * 10,
            candidate_scores=[None] * 10,
        )
        verdict = gate_module.evaluate_gates(report)
        self.assertFalse(verdict["verdict"]["overall_pass"])
        self.assertIn("quality_lift", verdict["verdict"]["failed_criteria"])

    def test_minimum_sample_size_blocks_promotion(self):
        report = _make_report(total_queries=4)
        verdict = gate_module.evaluate_gates(report)
        self.assertFalse(verdict["criteria"]["minimum_sample_size"]["pass"])
        self.assertTrue(verdict["criteria"]["minimum_sample_size"]["borderline"])
        self.assertIn("minimum_sample_size", verdict["verdict"]["failed_criteria"])

    def test_graph_activation_metrics_present(self):
        report = _make_report(total_queries=12)
        for row in report["results"]:
            row["candidate"]["query_scope"] = {
                "graph_expansion_applied": True,
                "graph_neighbor_count": 5,
                "graph_hops_used": 1,
            }
        verdict = gate_module.evaluate_gates(report)
        metrics = verdict["metrics"]["graph_activation"]
        self.assertEqual(metrics["graph_applied_rate"], 100.0)
        self.assertEqual(metrics["average_graph_neighbor_count"], 5.0)
        self.assertEqual(metrics["average_graph_hops_used"], 1.0)

    def test_verdict_structure(self):
        """Verdict contains all required fields."""
        report = _make_report(total_queries=5)
        verdict = gate_module.evaluate_gates(report)

        self.assertIn("run_id", verdict)
        self.assertIn("baseline_env", verdict)
        self.assertIn("candidate_env", verdict)
        self.assertIn("total_queries", verdict)
        self.assertIn("thresholds", verdict)
        self.assertIn("metrics", verdict)
        self.assertIn("criteria", verdict)
        self.assertIn("verdict", verdict)

        # Criteria structure
        for crit_name in ["quality_lift", "p95_latency", "parse_failures"]:
            self.assertIn(crit_name, verdict["criteria"])
            self.assertIn("pass", verdict["criteria"][crit_name])
            self.assertIn("borderline", verdict["criteria"][crit_name])
            self.assertIn("detail", verdict["criteria"][crit_name])

        # Verdict structure
        v = verdict["verdict"]
        self.assertIn("overall_pass", v)
        self.assertIn("is_borderline", v)
        self.assertIn("failed_criteria", v)
        self.assertIn("rollback_recommendation", v)


# ── Rollback recommendation tests ────────────────────────────────────────────

class TestRollbackRecommendation(unittest.TestCase):
    """Test rollback recommendation generation."""

    def test_no_failures(self):
        """No failed criteria → no rollback needed."""
        rec = gate_module.generate_rollback_recommendation(
            failed_criteria=[],
            quality={"status": "ok", "lift_percent": 10.0},
            latency={"status": "ok", "increase_percent": 10.0},
            parse_failures={"failure_rate": 0.0, "is_borderline": False},
        )
        self.assertIn("No rollback needed", rec)

    def test_quality_failure(self):
        """Quality failure → rollback recommendation mentions quality."""
        rec = gate_module.generate_rollback_recommendation(
            failed_criteria=["quality_lift"],
            quality={"status": "ok", "lift_percent": 3.0},
            latency={"status": "ok", "increase_percent": 10.0},
            parse_failures={"failure_rate": 0.0, "is_borderline": False},
        )
        self.assertIn("Baseline NOT updated", rec)
        self.assertIn("Quality lift", rec)
        self.assertIn("Revert to baseline", rec)

    def test_latency_failure(self):
        """Latency failure → rollback mentions latency."""
        rec = gate_module.generate_rollback_recommendation(
            failed_criteria=["p95_latency"],
            quality={"status": "ok", "lift_percent": 15.0},
            latency={"status": "ok", "increase_percent": 35.0},
            parse_failures={"failure_rate": 0.0, "is_borderline": False},
        )
        self.assertIn("p95 latency increase", rec)
        self.assertIn("Revert to baseline", rec)

    def test_insufficient_data(self):
        """No data → rollback with data check."""
        rec = gate_module.generate_rollback_recommendation(
            failed_criteria=["quality_lift"],
            quality={"status": "no_data"},
            latency={"status": "ok", "increase_percent": 5.0},
            parse_failures={"failure_rate": 0.0, "is_borderline": False},
        )
        self.assertIn("Insufficient data", rec)
        self.assertIn("Check gateway", rec)


# ── Threshold constant tests ─────────────────────────────────────────────────

class TestThresholdConstants(unittest.TestCase):
    """Verify locked threshold constants."""

    def test_quality_lift_threshold(self):
        self.assertEqual(gate_module.QUALITY_LIFT_THRESHOLD, 8.0)

    def test_latency_increase_threshold(self):
        self.assertEqual(gate_module.LATENCY_INCREASE_THRESHOLD, 20.0)

    def test_quality_borderline_margin(self):
        self.assertEqual(gate_module.QUALITY_BORDERLINE_MARGIN, 2.0)

    def test_latency_borderline_margin(self):
        self.assertEqual(gate_module.LATENCY_BORDERLINE_MARGIN, 3.0)

    def test_parse_failure_threshold(self):
        self.assertEqual(gate_module.PARSE_FAILURE_THRESHOLD, 10.0)


if __name__ == "__main__":
    unittest.main()
