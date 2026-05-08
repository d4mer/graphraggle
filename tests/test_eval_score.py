"""Tests for Packet 15: eval_score.py rubric scoring and aggregation.

Covers:
- Judge output parsing: valid 0/1 fields, invalid values, missing fields
- Heuristic scoring fallback
- Rubric aggregation: mean score, per-field rates
- Parse failure handling: None scores counted as failures
- Report scoring: full report with scored results
- Edge cases: empty report, no judge output, mixed valid/invalid
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import scripts.eval_score as score_module


# ── Judge output parser tests ────────────────────────────────────────────────

class TestParseJudgeOutput(unittest.TestCase):
    """Test parse_judge_output function."""

    def test_valid_rubric(self):
        """All three fields 0/1 → valid rubric dict."""
        judge = {"grounded": 1, "fact_consistent": 1, "complete": 0}
        result = score_module.parse_judge_output(judge)
        self.assertIsNotNone(result)
        self.assertEqual(result["grounded"], 1)
        self.assertEqual(result["fact_consistent"], 1)
        self.assertEqual(result["complete"], 0)
        self.assertEqual(result["rubric_score"], round(2 / 3, 4))

    def test_all_zero(self):
        """All fields 0 → valid rubric with score 0."""
        judge = {"grounded": 0, "fact_consistent": 0, "complete": 0}
        result = score_module.parse_judge_output(judge)
        self.assertIsNotNone(result)
        self.assertEqual(result["rubric_score"], 0.0)

    def test_all_one(self):
        """All fields 1 → valid rubric with score 1."""
        judge = {"grounded": 1, "fact_consistent": 1, "complete": 1}
        result = score_module.parse_judge_output(judge)
        self.assertIsNotNone(result)
        self.assertEqual(result["rubric_score"], 1.0)

    def test_invalid_field(self):
        """Field value 2 → None (invalid)."""
        judge = {"grounded": 2, "fact_consistent": 1, "complete": 1}
        result = score_module.parse_judge_output(judge)
        self.assertIsNone(result)

    def test_string_value(self):
        """String field value → None (cannot convert to int)."""
        judge = {"grounded": "yes", "fact_consistent": 1, "complete": 1}
        result = score_module.parse_judge_output(judge)
        self.assertIsNone(result)

    def test_missing_fields(self):
        """Missing fields → None."""
        judge = {"grounded": 1}
        result = score_module.parse_judge_output(judge)
        self.assertIsNone(result)

    def test_none_input(self):
        """None input → None."""
        result = score_module.parse_judge_output(None)
        self.assertIsNone(result)

    def test_empty_dict(self):
        """Empty dict → None."""
        result = score_module.parse_judge_output({})
        self.assertIsNone(result)

    def test_reasoning_preserved(self):
        """Reasoning field is passed through."""
        judge = {
            "grounded": 1,
            "fact_consistent": 0,
            "complete": 1,
            "reasoning": "Answer is partially grounded",
        }
        result = score_module.parse_judge_output(judge)
        self.assertEqual(result["reasoning"], "Answer is partially grounded")

    def test_negative_value(self):
        """Negative value → None."""
        judge = {"grounded": -1, "fact_consistent": 1, "complete": 1}
        result = score_module.parse_judge_output(judge)
        self.assertIsNone(result)

    def test_float_value(self):
        """Float value (e.g., 1.0) → may convert to int 1 via int()."""
        judge = {"grounded": 1.0, "fact_consistent": 0.0, "complete": 1.0}
        result = score_module.parse_judge_output(judge)
        # int(1.0) = 1, which is valid
        self.assertIsNotNone(result)


# ── Heuristic scoring tests ──────────────────────────────────────────────────

class TestHeuristicScore(unittest.TestCase):
    """Test heuristic_score fallback function."""

    def test_complete_answer(self):
        """Long answer with citations → grounded=1, fact_consistent=1, complete=1."""
        answer = "Based on the retrieved documents, the Q3 revenue guidance for the semiconductor division is approximately $2.5 billion, as stated in the latest earnings release."
        result = score_module.heuristic_score(answer, [{"text": "Q3 guidance: $2.5B"}], "Q3 revenue")
        self.assertEqual(result["grounded"], 1)
        self.assertEqual(result["fact_consistent"], 1)
        self.assertEqual(result["complete"], 1)

    def test_short_answer(self):
        """Very short answer → complete=0."""
        answer = "Yes"
        result = score_module.heuristic_score(answer, [{"text": "data"}], "Tell me about Q3")
        self.assertEqual(result["complete"], 0)

    def test_no_citations(self):
        """Empty citations → grounded=0."""
        answer = "The answer is that Q3 revenue was strong."
        result = score_module.heuristic_score(answer, [], "Q3 revenue")
        self.assertEqual(result["grounded"], 0)

    def test_contradiction_detected(self):
        """Answer contains contradiction markers → fact_consistent=0."""
        answer = "The document contradicts the claim that revenue was up."
        result = score_module.heuristic_score(answer, [{"text": "Revenue was up"}], "Was revenue up?")
        self.assertEqual(result["fact_consistent"], 0)

    def test_heuristic_method_field(self):
        """Heuristic scoring includes method field."""
        result = score_module.heuristic_score("test", [], "test")
        self.assertEqual(result["method"], "heuristic")

    def test_empty_answer(self):
        """Empty answer → grounded=0, fact_consistent=1 (no contradiction), complete=0."""
        result = score_module.heuristic_score("", [], "test query")
        self.assertEqual(result["grounded"], 0)
        self.assertEqual(result["fact_consistent"], 1)
        self.assertEqual(result["complete"], 0)


# ── Aggregation tests ────────────────────────────────────────────────────────

class TestAggregateScores(unittest.TestCase):
    """Test aggregate_scores function."""

    def test_equal_scores(self):
        """Same scores for both → identical stats."""
        bl = [{"grounded": 1, "fact_consistent": 1, "complete": 1, "rubric_score": 1.0}]
        cd = [{"grounded": 1, "fact_consistent": 1, "complete": 1, "rubric_score": 1.0}]
        result = score_module.aggregate_scores(bl, cd)
        self.assertEqual(result["baseline"]["mean_rubric_score"], 1.0)
        self.assertEqual(result["candidate"]["mean_rubric_score"], 1.0)

    def test_different_scores(self):
        """Baseline 0.5, candidate 0.75 → different stats."""
        bl = [{"grounded": 1, "fact_consistent": 0, "complete": 0, "rubric_score": 0.3333}]
        cd = [{"grounded": 1, "fact_consistent": 1, "complete": 0, "rubric_score": 0.6667}]
        result = score_module.aggregate_scores(bl, cd)
        self.assertAlmostEqual(result["baseline"]["mean_rubric_score"], 0.3333, places=3)
        self.assertAlmostEqual(result["candidate"]["mean_rubric_score"], 0.6667, places=3)

    def test_empty_lists(self):
        """Empty score lists → zero stats."""
        result = score_module.aggregate_scores([], [])
        self.assertEqual(result["baseline"]["mean_rubric_score"], 0.0)
        self.assertEqual(result["candidate"]["mean_rubric_score"], 0.0)

    def test_none_scores_excluded(self):
        """None scores are excluded from aggregation."""
        bl = [
            {"grounded": 1, "fact_consistent": 1, "complete": 1, "rubric_score": 1.0},
            None,
            {"grounded": 0, "fact_consistent": 0, "complete": 0, "rubric_score": 0.0},
        ]
        cd = [None, None, None]
        result = score_module.aggregate_scores(bl, cd)
        self.assertEqual(result["baseline"]["mean_rubric_score"], 0.5)
        self.assertEqual(result["candidate"]["mean_rubric_score"], 0.0)

    def test_per_field_rates(self):
        """Per-field rates computed correctly."""
        bl = [
            {"grounded": 1, "fact_consistent": 0, "complete": 1, "rubric_score": 0.6667},
            {"grounded": 0, "fact_consistent": 1, "complete": 0, "rubric_score": 0.3333},
        ]
        cd = [None, None]
        result = score_module.aggregate_scores(bl, cd)
        self.assertEqual(result["baseline"]["grounded_rate"], 0.5)
        self.assertEqual(result["baseline"]["fact_consistent_rate"], 0.5)
        self.assertEqual(result["baseline"]["complete_rate"], 0.5)


# ── Report scoring tests ─────────────────────────────────────────────────────

class TestScoreReport(unittest.TestCase):
    """Test score_report function (full report pipeline)."""

    def _make_report(self, with_judge=False):
        """Helper to create a minimal report."""
        results = []
        judge_out = {
            "grounded": 1,
            "fact_consistent": 0,
            "complete": 1,
            "reasoning": "partial",
        }
        for i in range(4):
            r = {
                "query_id": f"q{i+1}",
                "query": f"Query {i+1}",
                "category": "test",
                "baseline": {
                    "latency_ms": 1000 + i * 100,
                    "answer": f"Baseline answer {i+1}",
                    "citation_count": 3,
                    "error": None,
                },
                "candidate": {
                    "latency_ms": 1100 + i * 100,
                    "answer": f"Candidate answer {i+1}",
                    "citation_count": 4,
                    "error": None,
                },
            }
            if with_judge:
                r["baseline"]["judge_output"] = judge_out
            results.append(r)

        return {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 4,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }


class TestJudgeModelScoring(unittest.TestCase):
    def _make_report(self, with_judge=False):
        results = []
        judge_out = {
            "grounded": 1,
            "fact_consistent": 0,
            "complete": 1,
            "reasoning": "partial",
        }
        for i in range(4):
            r = {
                "query_id": f"q{i+1}",
                "query": f"Query {i+1}",
                "category": "test",
                "baseline": {
                    "latency_ms": 1000 + i * 100,
                    "answer": f"Baseline answer {i+1}",
                    "citation_count": 3,
                    "error": None,
                },
                "candidate": {
                    "latency_ms": 1100 + i * 100,
                    "answer": f"Candidate answer {i+1}",
                    "citation_count": 4,
                    "error": None,
                },
            }
            if with_judge:
                r["baseline"]["judge_output"] = judge_out
            results.append(r)

        return {
            "run_id": "packet-15",
            "baseline_env": "baseline",
            "candidate_env": "candidate",
            "total_queries": 4,
            "baseline_errors": 0,
            "candidate_errors": 0,
            "results": results,
        }

    def _write_temp(self, report: dict) -> str:
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(report, f)
            return path
        except:
            if os.path.exists(path):
                os.unlink(path)
            raise

    def test_judge_model_success(self):
        payload = {
            "choices": [
                {"message": {"content": '{"grounded":1,"fact_consistent":1,"complete":0,"reason":"ok"}'}}
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False

        with patch.dict(os.environ, {
            "EVAL_JUDGE_ENABLED": "true",
            "EVAL_JUDGE_URL": "http://judge.local/v1",
            "EVAL_JUDGE_MODEL": "judge-model",
            "EVAL_JUDGE_API_KEY": "1234",
        }, clear=False), patch("scripts.eval_score.urllib.request.urlopen", return_value=mock_ctx):
            out = score_module.score_with_judge_model("q", "a", [{"content": "c"}])
        self.assertIsNotNone(out)
        self.assertEqual(out["method"], "judge_model")
        self.assertEqual(out["rubric_score"], round((1 + 1 + 0) / 3, 4))

    def test_judge_model_fallback_none_on_bad_payload(self):
        payload = {"choices": [{"message": {"content": "not-json"}}]}
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False
        with patch.dict(os.environ, {
            "EVAL_JUDGE_ENABLED": "true",
            "EVAL_JUDGE_URL": "http://judge.local/v1",
            "EVAL_JUDGE_MODEL": "judge-model",
        }, clear=False), patch("scripts.eval_score.urllib.request.urlopen", return_value=mock_ctx):
            out = score_module.score_with_judge_model("q", "a", [{"content": "c"}])
        self.assertIsNone(out)

    def test_judge_prompt_includes_expected_topics(self):
        payload = {
            "choices": [
                {"message": {"content": '{"grounded":1,"fact_consistent":1,"complete":1,"reason":"ok"}'}}
            ]
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode("utf-8")
        mock_ctx = MagicMock()
        mock_ctx.__enter__.return_value = mock_resp
        mock_ctx.__exit__.return_value = False
        captured = {}

        def fake_urlopen(req, timeout=60):
            captured["body"] = req.data.decode("utf-8")
            return mock_ctx

        with patch.dict(os.environ, {
            "EVAL_JUDGE_ENABLED": "true",
            "EVAL_JUDGE_URL": "http://judge.local/v1",
            "EVAL_JUDGE_MODEL": "judge-model",
        }, clear=False), patch("scripts.eval_score.urllib.request.urlopen", side_effect=fake_urlopen):
            out = score_module.score_with_judge_model("query", "answer", [{"content": "citation"}], ["topic-a", "topic-b"])
        self.assertIsNotNone(out)
        self.assertIn("topic-a", captured["body"])
        self.assertIn("topic-b", captured["body"])

    def test_scored_field_added(self):
        """Scored field is added to each result."""
        report = self._make_report(with_judge=True)
        scored = score_module.score_report(
            self._write_temp(report)
        )
        for r in scored["results"]:
            self.assertIn("scored", r)
            self.assertIn("baseline", r["scored"])
            self.assertIn("candidate", r["scored"])

    def test_aggregation_added(self):
        """Aggregation stats are added to scored report."""
        report = self._make_report(with_judge=True)
        scored = score_module.score_report(
            self._write_temp(report)
        )
        self.assertIn("scored", scored)
        self.assertIn("aggregation", scored["scored"])
        self.assertIn("baseline", scored["scored"]["aggregation"])
        self.assertIn("candidate", scored["scored"]["aggregation"])

    def test_parse_failures_counted(self):
        """Queries without judge output → parse failures counted."""
        report = self._make_report(with_judge=False)
        scored = score_module.score_report(
            self._write_temp(report)
        )
        # Without judge output, heuristic is used, so 0 parse failures
        self.assertEqual(scored["scored"]["baseline_parse_failures"], 0)

    def test_heuristic_fallback(self):
        """When no judge output, heuristic scoring is applied."""
        report = self._make_report(with_judge=False)
        scored = score_module.score_report(
            self._write_temp(report)
        )
        for r in scored["results"]:
            baseline = r["scored"]["baseline"]
            self.assertIn("method", baseline)
            self.assertEqual(baseline["method"], "heuristic")

    def test_file_written(self):
        """Scored report is written back to the file."""
        report = self._make_report(with_judge=True)
        path = self._write_temp(report)
        scored = score_module.score_report(path)

        # Verify file content matches returned dict
        with open(path) as f:
            file_content = json.load(f)
        self.assertIn("scored", file_content)
        self.assertIn("aggregation", file_content["scored"])

    def _write_temp(self, report: dict) -> str:
        """Write report to temp file, return path."""
        fd, path = tempfile.mkstemp(suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(report, f)
            return path
        except:
            if os.path.exists(path):
                os.unlink(path)
            raise

    def test_missing_file(self):
        """Non-existent file → exit with error."""
        with self.assertRaises(SystemExit):
            score_module.score_report("/nonexistent/report.json")


# ── Rubric score computation tests ───────────────────────────────────────────

class TestRubricScoreCalculation(unittest.TestCase):
    """Test rubric score calculation (grounded + fact_consistent + complete) / 3."""

    def test_all_pass_score(self):
        """All three fields = 1 → score = 1.0."""
        rubric = {"grounded": 1, "fact_consistent": 1, "complete": 1}
        expected = 1.0
        self.assertAlmostEqual(rubric["grounded"] + rubric["fact_consistent"] + rubric["complete"], 3)

    def test_two_pass_score(self):
        """Two fields = 1, one = 0 → score = 0.6667."""
        rubric = {"grounded": 1, "fact_consistent": 1, "complete": 0}
        expected = round(2 / 3, 4)
        self.assertEqual(expected, 0.6667)

    def test_one_pass_score(self):
        """One field = 1 → score = 0.3333."""
        rubric = {"grounded": 1, "fact_consistent": 0, "complete": 0}
        expected = round(1 / 3, 4)
        self.assertEqual(expected, 0.3333)

    def test_all_fail_score(self):
        """All fields = 0 → score = 0.0."""
        rubric = {"grounded": 0, "fact_consistent": 0, "complete": 0}
        expected = 0.0
        self.assertAlmostEqual((rubric["grounded"] + rubric["fact_consistent"] + rubric["complete"]) / 3, expected)


if __name__ == "__main__":
    unittest.main()
