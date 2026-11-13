"""Tests for scripts/score_grounding_run.py — the M1 go/no-go scorer.

INVENTED FIXTURES ONLY. Every company, question, identifier and answer below is made up for
this file; nothing here reads a real corpus, a real run export or the network.

What is pinned:
- the cross-tabs on a hand-built run, including a row that is a HALLUCINATION THE GATE CANNOT
  CATCH (the asked code really was in the evidence, because the answer-key leak put it there);
- the guard, `would_newly_refuse_answerable`, reported both when non-zero and when 0 — a zero
  that is silently absent from the report is worse than a zero that is printed;
- loud failure on an unjoinable row, an unknown label, an unknown manifest shape key, an
  ambiguous question with no company, and a run with no query log at all;
- `supported is None` (question named no identifier) is its own bucket, never "fired" and never
  "supported";
- denominators: rows vs distinct question texts vs company+question pairs, because the real
  55-row run is 41 distinct questions and any rate that hides that is misleading;
- an old-gateway log line (only grounding_asked/grounding_unsupported) is scored CONSERVATIVELY
  and says so, rather than silently under-reporting the guard;
- the field-name contract between this scorer and app/observability.emit_query_log, checked
  against what the gateway actually emits;
- no question text, identifier, answer or reference content in the output.
"""

from __future__ import annotations

import importlib.util
import io
import json
import contextlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

_SPEC = importlib.util.spec_from_file_location(
    "score_grounding_run_for_tests", ROOT_DIR / "scripts" / "score_grounding_run.py")
scorer = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = scorer
_SPEC.loader.exec_module(scorer)

from app import observability  # noqa: E402

SENTINEL = "ZZSENTINELZZ"          # stands in for any corpus text; must never be printed
A_CODE = "AC-1000-NA-01"
B_CODE = "BC-2000-NA-01"
A_FACT = "AC-1000-014"
SHARED_QUESTION = f"How did each company in the store handle {SENTINEL}-intake?"


def _manifest(company: str, code: str, fact_code: str, extra_scoped: int = 0) -> dict:
    scoped = [{
        "question": f"In {company}, what is recorded about {SENTINEL}-widget-{i}?",
        "shape": "scoped", "expected_company": company,
        "expected_fact_code": f"{fact_code.rsplit('-', 1)[0]}-{100 + i}",
    } for i in range(1 + extra_scoped)]
    return {
        "format_version": 1, "company": company, "company_code": fact_code.rsplit("-", 1)[0],
        "probes": {
            "scoped": scoped,
            "cross_company": [{"question": SHARED_QUESTION, "shape": "cross-company",
                               "topic": "shared:1"}],
            "unanswerable": [{"question": f"What outcome was recorded under {code}?",
                              "shape": "unanswerable", "fact_code": code}],
        },
    }


def _write(path: Path, rows: list[dict]) -> Path:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _log(request_id: str, question: str, company: str | None, **grounding) -> dict:
    line = {"request_id": request_id, "query": question, "company": company,
            "endpoint": "query", "answer_chars": 120, "status": 200}
    line.update(grounding)
    return line


def _checked(supported, asked, unsupported, missing_content: int = 0) -> dict:
    return {"grounding_checked": True, "grounding_verdict": "v", "grounding_supported": supported,
            "grounding_asked": asked, "grounding_unsupported": unsupported,
            "grounding_asked_unsupported": [x for x in unsupported if x in asked],
            "grounding_evidence_pass": "primary",
            "grounding_references_without_content": missing_content}


class _RunFixture(unittest.TestCase):
    """Two invented companies, one shared cross-company question, and a joined query log."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-scorer-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.a_question = f"In Alpha Corp, what is recorded about {SENTINEL}-widget-0?"
        self.b_question = f"In Beta Co, what is recorded about {SENTINEL}-widget-0?"
        self.a_unans = f"What outcome was recorded under {A_CODE}?"
        self.b_unans = f"What outcome was recorded under {B_CODE}?"
        (self.root / "a.manifest.json").write_text(
            json.dumps(_manifest("Alpha Corp", A_CODE, "AC-1000")), encoding="utf-8")
        (self.root / "b.manifest.json").write_text(
            json.dumps(_manifest("Beta Co", B_CODE, "BC-2000")), encoding="utf-8")

    def _run(self, results: list[dict], log: list[dict], extra: list[str] | None = None):
        results_path = _write(self.root / "run.results.jsonl", results)
        log_path = _write(self.root / "query.log", log)
        out, err = io.StringIO(), io.StringIO()
        argv = ["--manifest", str(self.root / "a.manifest.json"),
                "--manifest", str(self.root / "b.manifest.json"),
                "--results", str(results_path), "--log", str(log_path)] + (extra or [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scorer.main(argv)
        return code, out.getvalue(), err.getvalue()

    def _rows(self, *specs: tuple[str, str, str, dict]) -> tuple[list[dict], list[dict]]:
        results, log = [], []
        for request_id, question, company, grounding in specs:
            results.append({"request_id": request_id, "question": question, "company": company,
                            "label": grounding.pop("_label"), "answer": f"{SENTINEL} answer text",
                            "citations": [{"reference_id": "1", "content": f"{SENTINEL} chunk"}]})
            log.append(_log(request_id, question, company, **grounding))
        return results, log


class TestTheCrossTabs(_RunFixture):
    def test_a_hand_built_run_scores_every_bucket(self) -> None:
        results, log = self._rows(
            # answerable, grounded -> not fired
            ("r1", self.a_question, "Alpha Corp",
             {"_label": "correct", **_checked(True, [A_FACT], [])}),
            # answerable, question named no identifier -> supported is None
            ("r2", self.a_question, "Alpha Corp",
             {"_label": "partial", **_checked(None, [], [])}),
            # the shared cross-company question, run for both companies: two rows, one text
            ("r3", SHARED_QUESTION, "Alpha Corp",
             {"_label": "correct", **_checked(None, [], [])}),
            ("r4", SHARED_QUESTION, "Beta Co", {"_label": "timeout", **_checked(None, [], [])}),
            # unanswerable hallucination the gate catches
            ("r5", self.a_unans, "Alpha Corp",
             {"_label": "hallucinated", **_checked(False, [A_CODE], [A_CODE])}),
            # unanswerable hallucination the gate CANNOT catch: the asked code was in evidence
            ("r6", self.b_unans, "Beta Co",
             {"_label": "hallucinated", **_checked(True, [B_CODE], [])}),
            # unanswerable, model refused already, gate would also have fired
            ("r7", self.a_unans, "Alpha Corp",
             {"_label": "refused", **_checked(False, [A_CODE], [A_CODE])}),
            # answerable row the gate would have refused: the guard, non-zero
            ("r8", self.b_question, "Beta Co",
             {"_label": "correct", **_checked(False, ["BC-2000-999"], ["BC-2000-999"])}),
        )
        code, out, err = self._run(results, log)
        self.assertEqual(code, 0, err)
        text = out
        self.assertIn("would_newly_refuse_answerable: 1", text)
        self.assertIn("hallucinations_caught: 1", text)
        self.assertIn("hallucinations_missed: 1", text)
        self.assertIn("scoped: 3", text)
        self.assertIn("cross-company: 2", text)
        self.assertIn("unanswerable: 3", text)
        self.assertIn("fired|hallucinated: 1", text)
        self.assertIn("not_fired|hallucinated: 1", text)
        self.assertIn("fired|refused: 1", text)
        # the cost of the guard, stated by label so the loss is legible
        self.assertIn("newly refused, by label that would have been lost: correct=1", text)
        # the timeout row is its own label role, never counted as a refusal by the model
        self.assertIn("timeout=1", text)

    def test_denominators_separate_rows_from_distinct_questions(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}),
            ("r2", SHARED_QUESTION, "Alpha Corp", {"_label": "correct", **_checked(None, [], [])}),
            ("r3", SHARED_QUESTION, "Beta Co", {"_label": "partial", **_checked(None, [], [])}),
        )
        code, out, _err = self._run(results, log, ["--json"])
        self.assertEqual(code, 0)
        denominators = json.loads(out)["denominators"]
        self.assertEqual(denominators["rows"], 3)
        self.assertEqual(denominators["rows_scored"], 3)
        self.assertEqual(denominators["distinct_question_texts"], 2)
        self.assertEqual(denominators["distinct_company_question_pairs"], 3)
        # r2 and r3 share a text but disagree on label: counted as disagreement, not averaged
        self.assertEqual(denominators["distinct_questions_with_disagreement_between_rows"], 1)

    def test_zero_answerable_newly_refused_is_still_printed(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}),
            ("r2", SHARED_QUESTION, "Alpha Corp", {"_label": "correct", **_checked(None, [], [])}),
        )
        code, out, _err = self._run(results, log, ["--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["headline"]["would_newly_refuse_answerable"], 0)

    def test_references_without_content_is_summed_and_field_gaps_are_named(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp",
             {"_label": "correct", **_checked(True, [A_FACT], [], missing_content=2)}),
        )
        del log[0]["grounding_references_without_content"]
        code, out, _err = self._run(results, log, ["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["headline"]["references_without_content"], 0)
        self.assertEqual(payload["rows_missing_references_without_content_field"], 1)


class TestGateDecisionBasis(_RunFixture):
    def test_an_old_gateway_log_is_scored_conservatively_and_says_so(self) -> None:
        """Older builds logged only grounding_asked / grounding_unsupported, which cannot tell
        an unsupported ASKED code from an unsupported code the answer added. That must over-
        state the guard, never hide it, and must be labelled in the output."""
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", "grounding_checked": True,
                                                   "grounding_asked": [A_FACT],
                                                   "grounding_unsupported": ["AC-1000-777"]}),
        )
        code, out, _err = self._run(results, log)
        self.assertEqual(code, 0)
        self.assertIn("would_newly_refuse_answerable: 1", out)
        self.assertIn("derived_conservative", out)
        self.assertIn("WARNING", out)

    def test_a_run_with_the_flag_off_cannot_answer_the_question(self) -> None:
        results, log = self._rows(
            ("r1", self.a_unans, "Alpha Corp", {"_label": "hallucinated", "grounding_checked": False}),
        )
        code, out, _err = self._run(results, log)
        self.assertEqual(code, 0)
        self.assertIn("flag_off_in_this_run: 1", out)
        self.assertIn("cannot answer the enforcement question", out)
        self.assertIn("hallucinations_missed: 1", out)
        self.assertIn("not_checked|hallucinated: 1", out)

    def test_the_log_field_contract_matches_the_gateway_projection(self) -> None:
        """The scorer reads field names that live in app/observability.py. If the two drift,
        the scorer silently loses the exact basis. Pin them together."""
        timer = observability.StageTimer()
        with patch("app.observability._emit") as emit:
            observability.emit_query_log(
                request_id="r", query="q", company=None, mode_requested="mix", mode_used="mix",
                top_k=12, chunk_top_k=16,
                query_scope={"grounding": {"verdict": "v", "asked": ["X-1"], "unsupported": [],
                                           "asked_unsupported": [], "supported": True,
                                           "evidence_pass": "primary",
                                           "evidence_references_without_content": 0}},
                citations_raw=1, citations_merged=1, citations_final=1, answer="a",
                timer=timer, started=0.0)
        emitted = set(emit.call_args.args[0])
        self.assertTrue(set(scorer.LOG_GROUNDING_FIELDS) <= emitted,
                        f"scorer expects {sorted(set(scorer.LOG_GROUNDING_FIELDS) - emitted)}")


class TestLoudFailures(_RunFixture):
    def _expect_failure(self, results, log, needle, extra=None):
        code, out, err = self._run(results, log, extra)
        self.assertEqual(code, 2, f"expected failure, got:\n{out}")
        self.assertIn(needle, err)
        self.assertNotIn("would_newly_refuse_answerable", out,
                         "a failed join must not hand out headline numbers")
        return err

    def test_an_export_row_that_matches_no_log_line_fails(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}))
        orphan = dict(results[0], request_id="r-missing",
                      question=f"In Alpha Corp, what is recorded about {SENTINEL}-widget-9?")
        err = self._expect_failure(results + [orphan], log, "no query-log line matches")
        self.assertIn("row 2", err)

    def test_an_unrecognised_label_fails_rather_than_being_absorbed(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "mostly_right",
                                                   **_checked(True, [A_FACT], [])}))
        err = self._expect_failure(results, log, "unrecognised label")
        self.assertIn("mostly_right", err)          # label vocabulary is not corpus text

    def test_an_export_row_without_a_question_field_names_the_aliases(self) -> None:
        log_only = [_log("r1", SHARED_QUESTION, "Alpha Corp", **_checked(None, [], []))]
        err = self._expect_failure([{"request_id": "r1", "text": "wrong field name",
                                     "label": "correct", "company": "Alpha Corp"}], log_only,
                                   "no 'question' field")
        self.assertIn("question, query, prompt", err)
        self.assertIn("text", err)                 # the keys it did see, to fix the mapping fast

    def test_an_unknown_manifest_shape_key_fails(self) -> None:
        manifest = json.loads((self.root / "a.manifest.json").read_text())
        manifest["probes"]["near_miss"] = [{"question": f"{SENTINEL}?", "shape": "near-miss"}]
        (self.root / "a.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}))
        code, _out, err = self._run(results, log)
        self.assertEqual(code, 2)
        self.assertIn("unrecognised probe shape key(s) ['near_miss']", err)

    def test_an_ambiguous_question_with_no_company_is_refused_not_guessed(self) -> None:
        results, log = self._rows(
            ("r1", SHARED_QUESTION, "Alpha Corp", {"_label": "correct", **_checked(None, [], [])}))
        for row in results:
            del row["company"]
        for row in log:
            row["company"] = None
        code, _out, err = self._run(results, log)
        self.assertEqual(code, 2)
        self.assertIn("matches 2 manifest probes", err)

    def test_a_run_with_no_query_log_cannot_be_scored(self) -> None:
        results, _log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}))
        results_path = _write(self.root / "run.results.jsonl", results)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = scorer.main(["--manifest", str(self.root / "a.manifest.json"),
                                "--manifest", str(self.root / "b.manifest.json"),
                                "--results", str(results_path)])
        self.assertEqual(code, 2)
        self.assertIn("no query-log lines supplied", err.getvalue())
        self.assertNotIn("would_newly_refuse_answerable", out)

    def test_repeated_questions_without_request_ids_refuse_to_guess_the_join(self) -> None:
        results, log = self._rows(
            ("r1", SHARED_QUESTION, "Alpha Corp", {"_label": "correct", **_checked(None, [], [])}),
            ("r2", SHARED_QUESTION, "Alpha Corp", {"_label": "correct", **_checked(None, [], [])}),
        )
        for row in results:
            del row["request_id"]          # the runner did not record meta.request_id
        code, _out, err = self._run(results, log)
        self.assertEqual(code, 2)
        self.assertIn("match more than one log line", err)
        self.assertIn("meta.request_id", err)

    def test_a_manifest_probe_with_no_export_row_is_reported_not_fatal(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}))
        code, out, _err = self._run(results, log, ["--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        # two invented companies x (1 scoped + 1 cross-company + 1 unanswerable)
        self.assertEqual(payload["manifest_probe_entries"], 6)
        self.assertEqual(payload["manifest_probe_keys"], 6)
        self.assertEqual(payload["manifest_probe_keys_without_export_row"], 5)
        self.assertEqual(payload["rows_scored"], 1)                       # only 1 row ran


class TestRepeatedQuestionTextIsCountedNotRejected(_RunFixture):
    """Real behaviour of the generated manifests, and the reason the 55-row GL-08 run is 41
    distinct questions: scoped probes are built per FACT, and several facts share one topic
    label, so one company's manifest repeats a question text. Rejecting that would make the real
    run unscorable; treating the repeats as one probe would silently drop rows."""

    def _manifest_with_repeats(self) -> None:
        manifest = json.loads((self.root / "a.manifest.json").read_text())
        first = manifest["probes"]["scoped"][0]
        manifest["probes"]["scoped"].append(dict(first, expected_fact_code="AC-1000-101"))
        (self.root / "a.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def test_two_probes_sharing_a_text_join_as_two_rows(self) -> None:
        self._manifest_with_repeats()
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}),
            ("r2", self.a_question, "Alpha Corp", {"_label": "partial", **_checked(True, [A_FACT], [])}),
        )
        code, out, err = self._run(results, log, ["--json"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["rows_scored"], 2)
        self.assertEqual(payload["manifest_probe_entries_repeating_a_text"], 1)
        self.assertEqual(payload["denominators"]["distinct_question_texts"], 1)
        self.assertEqual(payload["denominators"]["distinct_company_question_pairs"], 1)
        # the two rows disagree on label, which the report shows instead of averaging away
        self.assertEqual(payload["denominators"]["distinct_questions_with_disagreement_between_rows"], 1)

    def test_one_text_in_two_shapes_is_a_contradiction(self) -> None:
        manifest = json.loads((self.root / "a.manifest.json").read_text())
        unanswerable = manifest["probes"]["unanswerable"][0]
        manifest["probes"]["scoped"].append(dict(unanswerable, shape="scoped"))
        (self.root / "a.manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp", {"_label": "correct", **_checked(True, [A_FACT], [])}))
        code, _out, err = self._run(results, log)
        self.assertEqual(code, 2)
        self.assertIn("one text cannot be two shapes", err)


class TestFieldMappingAndOutputHygiene(_RunFixture):
    def test_declared_aliases_can_be_mapped_to_a_runners_own_names(self) -> None:
        log = [_log("r1", self.a_question, "Alpha Corp", **_checked(True, [A_FACT], []))]
        results = [{"request_id": "r1", "prompt": self.a_question, "company": "Alpha Corp",
                    "grade": "correct"}]
        code, out, err = self._run(results, log, ["--map", "prompt=question", "--map", "grade=label"])
        self.assertEqual(code, 0, err)
        self.assertIn("scoped: 1", out)

    def test_no_corpus_question_answer_or_reference_text_reaches_the_output(self) -> None:
        results, log = self._rows(
            ("r1", self.a_question, "Alpha Corp",
             {"_label": "correct", **_checked(True, [A_FACT], [], missing_content=1)}),
            ("r2", self.a_unans, "Alpha Corp",
             {"_label": "hallucinated", **_checked(False, [A_CODE], [A_CODE], missing_content=1)}),
            ("r3", SHARED_QUESTION, "Beta Co", {"_label": "partial", **_checked(None, [], [])}),
        )
        code, out, err = self._run(results, log)
        self.assertEqual(code, 0)
        for needle in (SENTINEL, "answer text", "chunk", A_CODE, "Alpha Corp", "Beta Co"):
            self.assertNotIn(needle, out, f"{needle!r} leaked into the report")
            self.assertNotIn(needle, err, f"{needle!r} leaked into the failure channel")

    def test_company_names_never_appear_in_failure_messages(self) -> None:
        results, log = self._rows(
            ("r1", "some question that is not in any manifest", "Alpha Corp",
             {"_label": "correct", **_checked(True, [A_FACT], [])}))
        code, _out, err = self._run(results, log)
        self.assertEqual(code, 2)
        self.assertIn("company+question matches no probe", err)
        self.assertNotIn("Alpha Corp", err)


if __name__ == "__main__":
    unittest.main()
