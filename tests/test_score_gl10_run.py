"""The GL-10 scorer: it must report the scoping truth and refuse to guess it.

Why this exists
---------------
The GL-10 go/no-go rests on four numbers (`other_company_citations`,
`other_company_codes_in_scoped_answers`, cross-company named companies cited, unscoped rows
unchanged). A scorer that quietly joins the wrong log line, or that infers a citation's company from
its path, produces a confident wrong answer to a production decision. So the tests are mostly about
refusals.

What the tests pin
------------------
- the four guard numbers are computed from what the export and the log say, and a planted
  cross-company leak is reported rather than absorbed
- detection is compared to what each probe expected: correct, over-scoped, under-scoped, wrong
- the citations-by-company matrix comes from the gateway's own company labels; when the export
  carries no citations the report says `available: false` and the acceptance line says
  "not computable" instead of inferring a company from a path
- unscoped rows are compared to the baseline run by citation counts and status only, so the new
  `company_scope` keys never read as a change
- refusals on unanswerable rows and correct% come from the judge's verdicts; without that file the
  report says so
- latency median and p95 from the log's `duration_ms`
- exit 2 with no headline numbers when: a run has results but no log capture, a results row has no
  request_id and its question repeats, a request_id repeats in the log, a probe id appears in two
  manifests, or a verdict names a probe that is not in the set
- no question, answer or reference text ever reaches stdout

Fixtures are invented company names, codes, questions and log lines in a temp directory.
"""

from __future__ import annotations

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The scorer is a script, not a package module: load it by path.
import importlib.util

_SPEC = importlib.util.spec_from_file_location(
    "score_gl10_run", Path(__file__).resolve().parents[1] / "scripts" / "score_gl10_run.py")
module = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(module)

def _document(out: str) -> dict:
    return json.loads(out.split("\nacceptance:")[0])


def _first_run(out: str) -> dict:
    return _document(out)["runs"][0]


ALPHA = "Alpha Chemicals"
BETA = "Beta Logistics"
Q_SCOPED = "In Alpha Chemicals, what is recorded about the plant freeze?"
Q_CROSS = "Compare Alpha Chemicals and Beta Logistics on the plant freeze."
Q_UNSCOPED = "What is recorded about the plant freeze?"
Q_UNANSWERABLE = "In Alpha Chemicals, what outcome was recorded under AC-100-NA-01?"


def _probe(probe_id: str, shape: str, question: str, expected: list[str], **extra) -> dict:
    row = {"id": probe_id, "shape": shape, "question": question,
           "expected_companies": sorted(expected),
           "expected_mode": "unscoped" if not expected else ("single" if len(expected) == 1
                                                             else "multi"),
           "run_group": "main"}
    row.update(extra)
    return row


def _manifest(path: Path, probes: dict[str, list[dict]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "format_version": 1, "company": ALPHA, "company_codes": {ALPHA: "AC-100", BETA: "BC-200"},
        "probes": probes,
    }, sort_keys=True), encoding="utf-8")


def _log_line(request_id: str, question: str, detected: list[str], **extra) -> dict:
    line = {"request_id": request_id, "query": question,
            "companies_detected": detected,
            "company_detected_from": "question" if detected else "none",
            "citations_raw": 6, "citations_merged": 5, "citations_scoped": 4, "citations_final": 3,
            "mode_used": "mix", "rerank_applied": False, "grounding_verdict": None,
            "answer_chars": 200, "status": 200, "duration_ms": 1000}
    line.update(extra)
    return line


def _result(request_id: str, probe_id: str, question: str, answer: str,
            citations: list[dict] | None = None) -> dict:
    row = {"id": probe_id, "request_id": request_id, "question": question, "answer": answer,
           "latency_s": 1.2}
    if citations is not None:
        row["citations"] = citations
    return row


class _Fixtures(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="grg-gl10score-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def write(self, name: str, rows: list[dict]) -> str:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return str(path)


class TestScoring(_Fixtures):
    def setUp(self):
        super().setUp()
        self.probe_manifest = self.root / "probes" / "manifest.json"
        _manifest(self.probe_manifest, {
            "scoped": [_probe("gl10-alpha-scop01", "scoped", Q_SCOPED, [ALPHA],
                              expected_fact_code="AC-100-T01-01", expected_outcome="answer")],
            "cross_company": [_probe("gl10-alpha-cros01", "cross_company", Q_CROSS,
                                     [ALPHA, BETA], expected_outcome="answer")],
            "unscoped": [_probe("gl10-alpha-unsc01", "unscoped", Q_UNSCOPED, [])],
            "unanswerable": [_probe("gl10-alpha-unan01", "unanswerable", Q_UNANSWERABLE, [ALPHA],
                                    expected_fact_code="AC-100-NA-01",
                                    expected_outcome="refusal")],
        })

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_clean_run_reports_zero_guards_and_the_matrix(self):
        results = self.write("r0.results.jsonl", [
            _result("q1", "gl10-alpha-scop01", Q_SCOPED, "AC-100-T01-01 ran 5 hours.",
                    [{"file_path": "a/one.txt", "company": ALPHA}]),
            _result("q2", "gl10-alpha-cros01", Q_CROSS,
                    "Alpha AC-100-T02-02 and Beta BC-200-T03-03.",
                    [{"company": ALPHA}, {"company": BETA}]),
            _result("q3", "gl10-alpha-unsc01", Q_UNSCOPED, "both AC-100-T04-04 and BC-200-T05-05",
                    [{"company": ALPHA}, {"company": BETA}]),
            _result("q4", "gl10-alpha-unan01", Q_UNANSWERABLE, "not in the documents",
                    [{"company": ALPHA}]),
        ])
        log = self.write("r0.log.jsonl", [
            _log_line("q1", Q_SCOPED, [ALPHA], scoped_pass={"references_kept": 3,
                                                            "references_dropped_other_company": 4,
                                                            "references_unattributable": 1,
                                                            "references_dropped_by_context_cap": 0,
                                                            "second_model_call": True}),
            _log_line("q2", Q_CROSS, [ALPHA, BETA], duration_ms=2000),
            _log_line("q3", Q_UNSCOPED, [], duration_ms=3000),
            _log_line("q4", Q_UNANSWERABLE, [ALPHA], duration_ms=4000),
        ])
        verdicts = self.write("r0.verdicts.jsonl", [
            {"id": "gl10-alpha-scop01", "verdict": "correct"},
            {"id": "gl10-alpha-cros01", "verdict": "correct"},
            {"id": "gl10-alpha-unsc01", "verdict": "correct"},
            {"id": "gl10-alpha-unan01", "verdict": "refused_ok"},
        ])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}",
                                    "--verdicts", f"R0={verdicts}"])
        self.assertEqual(code, 0, err)
        report = _first_run(out)
        self.assertEqual(report["guards"].get("other_company_citations", 0), 0)
        self.assertEqual(report["guards"]["other_company_codes_in_scoped_answers"], 0)
        self.assertEqual(report["guards"]["asked_company_code_present"], 2,
                         "the scoped row and the cross-company row each carry an asked-for code; "
                         "the unscoped row is not code-checked, by design")
        self.assertEqual(report["guards"]["cross_company_all_named_cited"], 1)
        self.assertEqual(report["detection_vs_expectation"], {"correct": 4})
        self.assertEqual(report["rows_by_detected_mode"], {"multi": 1, "single": 2, "unscoped": 1})
        self.assertEqual(report["scoped_pass"]["references_dropped_other_company"], 4)
        self.assertEqual(report["latency_ms"]["median"], 2500)
        self.assertEqual(report["verdicts"]["unanswerable_refused"], "1/1")
        # asked Alpha -> cited Alpha appears for the scoped, cross-company and unanswerable rows;
        # asked Alpha -> cited Beta is the cross-company row, which is allowed by expectation.
        self.assertEqual(report["citations_by_company"]["matrix"][f"{ALPHA} -> {ALPHA}"], 3)
        self.assertEqual(report["citations_by_company"]["matrix"][f"{ALPHA} -> {BETA}"], 1)
        self.assertEqual(report["citations_by_company"]["matrix"]["(unscoped) -> Beta Logistics"], 1)
        self.assertIn("other_company_citations=0 (target 0) PASS", out)
        self.assertIn("scoped_correct=2/2", out)

    def test_a_leaked_other_company_citation_and_code_are_reported(self):
        results = self.write("r2.results.jsonl", [
            _result("q1", "gl10-alpha-scop01", Q_SCOPED,
                    "Alpha AC-100-T01-01 and also Beta BC-200-T09-09.",
                    [{"company": ALPHA}, {"company": BETA}]),
        ])
        log = self.write("r2.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R2={results}", "--log", f"R2={log}"])
        self.assertEqual(code, 0, err)
        report = _first_run(out)
        self.assertEqual(report["guards"]["other_company_citations"], 1)
        self.assertEqual(report["guards"]["other_company_codes_in_scoped_answers"], 1)
        self.assertIn("other_company_citations=1 (target 0) FAIL", out)
        self.assertIn("other_company_codes_in_scoped_answers=1 (target 0) FAIL", out)

    def test_detection_wrongness_is_classified_not_average(self):
        results = self.write("r1.results.jsonl", [
            _result("q1", "gl10-alpha-scop01", Q_SCOPED, "answer", [{"company": ALPHA}]),
            _result("q2", "gl10-alpha-cros01", Q_CROSS, "answer", [{"company": ALPHA}]),
            _result("q3", "gl10-alpha-unsc01", Q_UNSCOPED, "answer", []),
            _result("q4", "gl10-alpha-unan01", Q_UNANSWERABLE, "answer", []),
        ])
        log = self.write("r1.log.jsonl", [
            _log_line("q1", Q_SCOPED, [ALPHA, BETA]),      # over-scoped
            _log_line("q2", Q_CROSS, [ALPHA]),              # under-scoped
            _log_line("q3", Q_UNSCOPED, [BETA]),            # named a company that was not asked
            _log_line("q4", Q_UNANSWERABLE, [BETA]),        # wrong company entirely
        ])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R1={results}", "--log", f"R1={log}"])
        self.assertEqual(code, 0, err)
        report = _first_run(out)
        self.assertEqual(report["detection_vs_expectation"],
                         {"over_scoped": 2, "under_scoped": 1, "wrong": 1},
                         "over/under/wrong are kept apart: they have different fixes")
        self.assertEqual(report["guards"]["cross_company_missing_named"], 1)

    def test_no_citations_in_the_export_means_the_matrix_is_declared_unavailable(self):
        results = self.write("r0n.results.jsonl", [
            _result("q1", "gl10-alpha-scop01", Q_SCOPED, "AC-100-T01-01", None)])
        log = self.write("r0n.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}"])
        self.assertEqual(code, 0, err)
        report = _first_run(out)
        self.assertEqual(report["citations_by_company"], {"available": False})
        self.assertIn("cross_company_named_companies_cited=not computable", out)

    def test_unscoped_rows_are_compared_by_counts_not_by_the_new_keys(self):
        base_results = self.write("base.results.jsonl", [
            _result("u1", "gl10-alpha-unsc01", Q_UNSCOPED, "AC-100-T04-04", [{"company": ALPHA}])])
        base_log = self.write("base.log.jsonl", [_log_line("u1", Q_UNSCOPED, [])])
        # The only difference is the company_scope keys the flag adds by design.
        other_results = self.write("other.results.jsonl", [
            _result("u1", "gl10-alpha-unsc01", Q_UNSCOPED, "AC-100-T04-04", [{"company": ALPHA}])])
        other_log = self.write("other.log.jsonl", [
            _log_line("u1", Q_UNSCOPED, [], company_scope={"companies": []},
                      company_detected_from="none")])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={base_results}", "--run", f"R1={other_results}",
                                    "--log", f"R0={base_log}", "--log", f"R1={other_log}"])
        self.assertEqual(code, 0, err)
        reports = json.loads(out.split("\nacceptance:")[0])["runs"]
        self.assertTrue(all(report["unscoped_rows_changed_vs_baseline"] == 0
                            for report in reports), out)

    def test_a_real_change_in_an_unscoped_row_is_counted(self):
        base_results = self.write("b2.results.jsonl", [
            _result("u1", "gl10-alpha-unsc01", Q_UNSCOPED, "AC-100-T04-04", [{"company": ALPHA}])])
        base_log = self.write("b2.log.jsonl", [_log_line("u1", Q_UNSCOPED, [])])
        other_results = self.write("o2.results.jsonl", [
            _result("u1", "gl10-alpha-unsc01", Q_UNSCOPED, "shorter", [{"company": ALPHA}])])
        other_log = self.write("o2.log.jsonl",
                               [_log_line("u1", Q_UNSCOPED, [], citations_final=1)])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={base_results}", "--run", f"R1={other_results}",
                                    "--log", f"R0={base_log}", "--log", f"R1={other_log}"])
        self.assertEqual(code, 0, err)
        self.assertIn("R1: unscoped_rows_changed_vs_baseline=1 (target 0) FAIL", out)

    def test_no_question_answer_or_reference_text_reaches_stdout(self):
        results = self.write("quiet.results.jsonl", [
            _result("q1", "gl10-alpha-scop01", Q_SCOPED,
                    "The plant freeze window ran 5 hours 55 minutes with 32 blockers.",
                    [{"file_path": "transcripts/private-title.txt", "company": ALPHA}])])
        log = self.write("quiet.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}"])
        self.assertEqual(code, 0, err)
        for text in (Q_SCOPED, "5 hours 55 minutes", "32 blockers", "private-title.txt",
                     "references"):
            if text == "references":
                continue
            self.assertNotIn(text, out, f"{text!r} must not be printed")
        self.assertNotIn("file_path", out)


class TestRefusals(_Fixtures):
    def setUp(self):
        super().setUp()
        self.probe_manifest = self.root / "probes" / "manifest.json"
        _manifest(self.probe_manifest, {
            "scoped": [_probe("gl10-alpha-scop01", "scoped", Q_SCOPED, [ALPHA])]})

    def _run(self, argv: list[str]) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = module.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_a_run_without_a_log_capture_refuses(self):
        results = self.write("r.results.jsonl",
                             [_result("q1", "gl10-alpha-scop01", Q_SCOPED, "x")])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}"])
        self.assertEqual(code, 2)
        self.assertIn("no --log capture", err)
        self.assertNotIn("guards", out)

    def test_a_repeated_question_without_request_ids_refuses_rather_than_guesses(self):
        row = {"id": "gl10-alpha-scop01", "question": Q_SCOPED, "answer": "one"}
        row_two = {"id": "gl10-alpha-scop01", "question": Q_SCOPED, "answer": "two"}
        results = self.write("rep.results.jsonl", [row, row_two])
        log = self.write("rep.log.jsonl", [_log_line("x1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}"])
        self.assertEqual(code, 2)
        self.assertIn("refusing to guess", err)

    def test_a_duplicated_request_id_in_the_log_refuses(self):
        results = self.write("dup.results.jsonl",
                             [_result("q1", "gl10-alpha-scop01", Q_SCOPED, "x")])
        log = self.write("dup.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA]),
                                           _log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}"])
        self.assertEqual(code, 2)
        self.assertIn("appears twice in the log", err)

    def test_a_probe_id_in_two_manifests_refuses(self):
        second = self.root / "probes2" / "manifest.json"
        _manifest(second, {"scoped": [_probe("gl10-alpha-scop01", "scoped", Q_SCOPED, [ALPHA])]})
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--probe-manifest", str(second), "--run", "R0=/nope"])
        self.assertEqual(code, 2)
        self.assertIn("appears in more than one manifest", err)

    def test_a_verdict_for_an_unknown_probe_refuses(self):
        results = self.write("v.results.jsonl",
                             [_result("q1", "gl10-alpha-scop01", Q_SCOPED, "x")])
        log = self.write("v.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        verdicts = self.write("v.verdicts.jsonl", [{"id": "gl10-zulu-scop99", "verdict": "correct"}])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}",
                                    "--verdicts", f"R0={verdicts}"])
        self.assertEqual(code, 2)
        self.assertIn("not a probe in this run", err)

    def test_a_results_row_with_no_id_refuses(self):
        results = self.write("nid.results.jsonl", [{"request_id": "q1", "question": Q_SCOPED}])
        log = self.write("nid.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}"])
        self.assertEqual(code, 2)
        self.assertIn("no probe id", err)

    def test_a_field_alias_makes_a_differently_named_export_scoreable(self):
        results = self.write("alias.results.jsonl", [{
            "probe_id": "gl10-alpha-scop01", "trace_id": "q1", "query": Q_SCOPED,
            "response": "AC-100-T01-01", "source_companies": [ALPHA]}])
        log = self.write("alias.log.jsonl", [_log_line("q1", Q_SCOPED, [ALPHA])])
        code, out, err = self._run(["--probe-manifest", str(self.probe_manifest),
                                    "--run", f"R0={results}", "--log", f"R0={log}",
                                    "--map", "trace_id=request_id"])
        self.assertEqual(code, 0, err)
        report = _first_run(out)
        self.assertEqual(report["guards"]["asked_company_code_present"], 1)


if __name__ == "__main__":
    unittest.main()
