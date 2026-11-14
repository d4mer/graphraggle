"""Tests for scripts/make_identifier_probes.py — identifier-bearing answerable probes.

Fixtures are invented in a temp directory (a small fake company: four planted facts in plain
.txt documents, two unanswerable lookalike codes). One optional test runs the real GL-07
generated corpora when they are present on disk, because that is where the counts the lead
briefs Hermes come from; it is skipped otherwise, so the suite stays offline and portable.

What is pinned:
- determinism: same inputs, byte-identical output;
- every code the probes ask about is planted, verbatim in its document, AND a standalone token in
  at least one piece the repo chunker cuts — the gate reads evidence pieces, not files;
- no question contains a planted answer sentence (a probe that quotes its own answer is worthless);
- edge variants are labelled by the GATE'S OWN RULE, not by hand: lowercase -> the gate cannot see
  the identifier at all; one hyphen opened to a space -> the gate fires on an answerable question;
  trailing comma -> harmless;
- the generated manifest is accepted by the scorer's own loader (the contract between the two);
- validation refuses to write when a code was tampered with, and when a supposed unanswerable
  control is actually present in the corpus.
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

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

SPEC = importlib.util.spec_from_file_location(
    "make_identifier_probes_for_tests", ROOT_DIR / "scripts" / "make_identifier_probes.py")
generator = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = generator
SPEC.loader.exec_module(generator)

SCORER_SPEC = importlib.util.spec_from_file_location(
    "score_grounding_run_for_probe_tests", ROOT_DIR / "scripts" / "score_grounding_run.py")
scorer = importlib.util.module_from_spec(SCORER_SPEC)
assert SCORER_SPEC and SCORER_SPEC.loader
sys.modules[SCORER_SPEC.name] = scorer
SCORER_SPEC.loader.exec_module(scorer)

REAL_CORPORA = Path("/home/maverick/work/synthetic-co")
FACT_CODE = "FC-1000-T01-01"


def _fake_company(root: Path, *, code: str = FACT_CODE, control_code: str = "FC-1000-NA-01") -> Path:
    company = root / "Fakeco"
    (company / "transcripts").mkdir(parents=True)
    facts = [
        {"code": code, "fact_id": "FC-1000-001", "document": "transcripts/one.txt",
         "text": f"Entry {code}: the window ran 5 hours 55 minutes with 32 blockers.",
         "topic": "order_to_cash"},
        {"code": "FC-1000-T02-02", "fact_id": "FC-1000-002", "document": "transcripts/one.txt",
         "text": "Entry FC-1000-T02-02: the archive was retained for 7 years.",
         "topic": "retention"},
        {"code": "FC-1000-T03-03", "fact_id": "FC-1000-003", "document": "transcripts/two.txt",
         "text": "Entry FC-1000-T03-03: the freeze window was reduced to 48 hours.",
         "topic": "cutover"},
        {"code": "FC-1000-T04-04", "fact_id": "FC-1000-004", "document": "transcripts/two.txt",
         "text": "Entry FC-1000-T04-04: the interface batch failed twice.",
         "topic": "interfaces"},
    ]
    (company / "transcripts/one.txt").write_text(
        "\n".join(fact["text"] for fact in facts[:2]), encoding="utf-8")
    (company / "transcripts/two.txt").write_text(
        "\n".join(fact["text"] for fact in facts[2:]), encoding="utf-8")
    manifest = {
        "format_version": 1, "company": "Fakeco", "company_code": "FC-1000",
        "safety": "Invented data.",
        "facts": facts,
        "probes": {
            "scoped": [],
            "cross_company": [],
            "unanswerable": [{"question": f"What outcome was recorded under {control_code}?",
                              "shape": "unanswerable", "fact_code": control_code,
                              "expected": "refusal: the code exists in no document"}],
        },
    }
    (company / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return company


def _generate(corpora_root: Path, out_root: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    argv = ["--corpora-root", str(corpora_root), "--company", "Fakeco", "--out", str(out_root)]
    argv += list(extra)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = generator.main(argv)
    return code, out.getvalue(), err.getvalue()


class TestGeneratedProbeSet(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-idprobes-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.corpora = self.root / "corpora"
        self.corpora.mkdir()
        _fake_company(self.corpora)
        self.out = self.root / "probes"
        code, out, err = _generate(self.corpora, self.out, "--two-code-count", "2",
                                   "--edge-rate", "1.0")
        self.assertEqual(code, 0, err)
        self.manifest_path = self.out / "Fakeco" / "manifest.json"
        self.document = json.loads(self.manifest_path.read_text())
        self.rows = [row for bucket in self.document["probes"].values() for row in bucket]

    def rows_of(self, kind: str) -> list[dict]:
        return [row for row in self.rows if row["probe_kind"] == kind]

    def test_every_planted_code_becomes_one_identifier_probe(self) -> None:
        self.assertEqual(len(self.rows_of("identifier_answerable")), 4)
        codes = sorted(row["expected_fact_code"] for row in self.rows_of("identifier_answerable"))
        self.assertEqual(codes, ["FC-1000-T01-01", "FC-1000-T02-02", "FC-1000-T03-03",
                                 "FC-1000-T04-04"])

    def test_each_code_survives_the_chunker_as_a_standalone_token(self) -> None:
        from app import grounding
        company_dir = self.corpora / "Fakeco"
        for row in self.rows_of("identifier_answerable"):
            text = generator.document_text(company_dir / row["expected_document"])
            pieces = generator.document_pieces(text)
            self.assertIn(row["expected_fact_code"], text, row["id"])
            self.assertTrue(any(grounding.verbatim_support(row["expected_fact_code"], piece)
                                for piece in pieces),
                            f"{row['id']}: code is not token-findable in any evidence piece")

    def test_questions_carry_the_code_and_never_the_answer(self) -> None:
        answers = [fact["text"] for fact in
                   json.loads((self.corpora / "Fakeco/manifest.json").read_text())["facts"]]
        for row in self.rows_of("identifier_answerable"):
            self.assertIn(row["expected_fact_code"], row["question"])
        for row in self.rows:
            for answer in answers:
                self.assertNotIn(answer, row["question"],
                                 f"{row['id']} quotes a planted answer sentence")

    def test_phrasing_variety_is_deterministic_and_position_varied(self) -> None:
        questions = [row["question"] for row in self.rows_of("identifier_answerable")]
        self.assertEqual(len(set(questions)), len(questions), "templates did not vary")
        # at least one question starts with the code and one ends with it
        starts = sum(1 for row in self.rows_of("identifier_answerable")
                     if row["question"].startswith(row["expected_fact_code"]))
        ends = sum(1 for row in self.rows_of("identifier_answerable")
                   if row["question"].rstrip().endswith(row["expected_fact_code"] + "?"))
        self.assertGreater(starts, 0)
        self.assertGreater(ends, 0)

    def test_ids_are_unique_and_namespaced_away_from_gl13_probe_ids(self) -> None:
        ids = [row["id"] for row in self.rows]
        self.assertEqual(len(ids), len(set(ids)))
        for probe_id in ids:
            self.assertTrue(probe_id.startswith("idn-"), probe_id)
            self.assertIsNone(generator.GL13_ID_RE.match(probe_id), probe_id)

    def test_edge_expectations_come_from_the_gates_own_rule(self) -> None:
        """Not hand-labelled: the expectation is what app/grounding.py actually decides."""
        lowercase = self.rows_of("edge_lowercase_code")
        hyphen = self.rows_of("edge_hyphen_as_space")
        comma = self.rows_of("edge_trailing_comma")
        self.assertTrue(lowercase and hyphen and comma)
        for row in lowercase:
            self.assertEqual(row["expected_gate"], "no_identifier")
            self.assertEqual(row["expected_gate_asked"], [],
                             "a lowercase code is invisible to the identifier rule")
        for row in hyphen:
            self.assertEqual(row["expected_gate"], "fire",
                             "an answerable question whose code was opened at a hyphen fires")
            self.assertTrue(row["edge_expected_fire"])
        for row in comma:
            self.assertEqual(row["expected_gate"], "not_fire")
            self.assertEqual(row["expected_gate_asked"], [row["expected_fact_code"]])

    def test_two_code_rows_are_labelled_and_the_absent_one_is_expected_to_fire(self) -> None:
        both = self.rows_of("two_code_both_present")
        one_absent = self.rows_of("two_code_one_absent")
        self.assertTrue(both and one_absent)
        for row in both:
            self.assertEqual(len(row["codes_in_question"]), 2)
            self.assertEqual(row["expected_gate"], "not_fire",
                             "both codes planted and supported -> the gate must not fire")
        for row in one_absent:
            self.assertEqual(row["expected_gate"], "fire")
            self.assertEqual(len(row["codes_planted"]), 1)

    def test_controls_keep_their_original_fields_and_are_expected_to_fire(self) -> None:
        controls = self.rows_of("control_unanswerable")
        self.assertEqual(len(controls), 1)
        source = json.loads((self.corpora / "Fakeco/manifest.json").read_text())["probes"]["unanswerable"][0]
        for key, value in source.items():
            self.assertEqual(controls[0][key], value, f"control field {key} changed")
        self.assertEqual(controls[0]["expected_gate"], "fire")

    def test_the_scorer_loads_what_the_generator_writes(self) -> None:
        problems: list[str] = []
        probes = scorer.load_manifests([self.manifest_path], problems)
        self.assertEqual(problems, [])
        self.assertEqual(len(probes), len(self.rows))
        shapes = {probe["shape"] for probe in probes.values()}
        self.assertTrue(shapes <= {"scoped", "cross-company", "unanswerable"})

    def test_output_is_byte_identical_across_runs(self) -> None:
        second = self.root / "probes2"
        code, _out, err = _generate(self.corpora, second, "--two-code-count", "2",
                                    "--edge-rate", "1.0")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.manifest_path.read_bytes(), (second / "Fakeco/manifest.json").read_bytes())
        self.assertEqual((self.out / "Fakeco/edge-variants.json").read_bytes(),
                         (second / "Fakeco/edge-variants.json").read_bytes())

    def test_a_tampered_code_stops_the_run(self) -> None:
        path = self.corpora / "Fakeco/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["facts"][0]["code"] = "FC-1000-T99-99"          # planted, but not in any document
        path.write_text(json.dumps(manifest), encoding="utf-8")
        code, _out, err = _generate(self.corpora, self.root / "out2", "--two-code-count", "2",
                                    "--edge-rate", "1.0")
        self.assertEqual(code, 2)
        self.assertIn("is not verbatim in", err)

    def test_a_control_that_exists_in_the_corpus_is_not_a_control(self) -> None:
        path = self.corpora / "Fakeco/manifest.json"
        manifest = json.loads(path.read_text())
        manifest["probes"]["unanswerable"][0]["fact_code"] = "FC-1000-T01-01"
        manifest["probes"]["unanswerable"][0]["question"] = "What outcome was recorded under FC-1000-T01-01?"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        code, _out, err = _generate(self.corpora, self.root / "out3", "--two-code-count", "2",
                                    "--edge-rate", "1.0")
        self.assertEqual(code, 2)
        self.assertIn("not expected to fire", err)

    def test_a_missing_company_manifest_is_reported_not_invented(self) -> None:
        captured, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(err):
            code = generator.main(["--corpora-root", str(self.root / "nowhere"),
                                   "--company", "Fakeco", "--out", str(self.root / "out4")])
        self.assertEqual(code, 2)
        self.assertIn("company manifest not found", err.getvalue())
        self.assertEqual(captured.getvalue(), "", "a failed run must not print a summary")


@unittest.skipUnless((REAL_CORPORA / "Northwind Chemicals/manifest.json").exists(),
                     "the generated GL-07 corpora are not on this machine")
class TestAgainstTheGeneratedGl07Corpora(unittest.TestCase):
    """Where the numbers the lead briefs Hermes come from: the two companies and seed Hermes
    ingested on stack2. Counts are asserted so a silent change in the generator shows up."""

    def test_counts_per_company(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="grg-idprobes-real-"))
        captured, warnings = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(warnings):
            code = generator.main(["--corpora-root", str(REAL_CORPORA), "--out", str(out), "--json"])
        self.assertEqual(code, 0, warnings.getvalue())
        summary = json.loads(captured.getvalue())
        self.assertEqual(sorted(summary["companies"]), ["Contoso Pharma", "Northwind Chemicals"])
        for company, counts in summary["companies"].items():
            self.assertEqual(counts["by_probe_kind"]["identifier_answerable"], 39, company)
            self.assertEqual(counts["by_probe_kind"]["control_unanswerable"], 6, company)
            self.assertEqual(counts["by_probe_kind"]["edge_hyphen_as_space"], 3, company)
            self.assertEqual(counts["by_expected_gate"]["not_fire"], 43, company)
            self.assertEqual(counts["by_expected_gate"]["no_identifier"], 3, company)
            manifest = json.loads((out / company / "manifest.json").read_text())
            problems: list[str] = []
            probes = scorer.load_manifests([out / company / "manifest.json"], problems)
            self.assertEqual(problems, [], company)
            self.assertEqual(len(probes), manifest["counts"]["rows"], company)


if __name__ == "__main__":
    unittest.main()
