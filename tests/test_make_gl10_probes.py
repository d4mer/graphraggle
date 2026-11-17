"""The GL-10 probe builder: it may only write a set it has already proved about itself.

Why this exists
---------------
The scorer's numbers are only as good as the expectations baked into the probe set, and Hermes
should not be improvising rows on a stack. So the builder validates before it writes: that every
planted code it references exists, that every question scopes to exactly the companies it claims
(judged by the shipped detector, not by hand), that no question copies a sentence, that shared-file
rows point at a real shared document, and that ids cannot collide with the GL-13 set. A failure is a
refusal, and the refusal names ids and rules, never question text.

What the tests pin
------------------
- the set is built from a generated corpus and matches the planned shape counts, per company
- rerunning is byte-identical: same corpora, same bytes, so the runbook can quote a sha256
- written files are mode 600, off-repo, and the builder refuses an output path inside the repo
- every emitted question scopes to exactly its `expected_companies` under the shipped detector, and
  the near-miss/unknown-org shapes scope to nothing
- no emitted question contains four consecutive words from any fact sentence in either corpus
- unanswerable rows reference codes that exist in `unanswerable_facts` and in no document
- each validation rule fires when the input is tampered with, and writes nothing when it does
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import stat
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.company_scope import detect_companies  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "make_gl10_probes", REPO_ROOT / "scripts" / "make_gl10_probes.py")
builder = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(builder)

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from make_synthetic_company import generate_company  # noqa: E402

COMPANIES = ("Northwind Chemicals", "Contoso Pharma")


def _build(corpora_root: Path, out: Path) -> tuple[int, str]:
    out_stream = StringIO()
    with redirect_stdout(out_stream), redirect_stderr(out_stream):
        code = builder.main(["--corpora-root", str(corpora_root), "--out", str(out),
                             "--companies", *COMPANIES, "--quiet"])
    return code, out_stream.getvalue()


class _Corpus(unittest.TestCase):
    """Two generated companies in a temp directory: invented data, never the real corpus."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="grg-gl10probes-")
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.corpora = self.root / "corpora"
        self.corpora.mkdir()
        try:
            for company in COMPANIES:
                generate_company(company, 7, self.corpora)
        except Exception as exc:                        # the generator needs python-pptx
            self.skipTest(f"GL-07 generator unavailable: {exc}")
        self.out = self.root / "probes"

    def manifests(self) -> dict[str, dict]:
        return {company: json.loads((self.out / company / "manifest.json").read_text())
                for company in COMPANIES}

    def rows(self) -> list[dict]:
        rows = []
        for manifest in self.manifests().values():
            for bucket in manifest["probes"].values():
                rows.extend(bucket)
        return rows


class TestBuiltSet(_Corpus):
    def test_the_set_is_written_with_the_planned_shape_counts(self):
        code, _ = _build(self.corpora, self.out)
        self.assertEqual(code, 0)
        for company, manifest in self.manifests().items():
            self.assertEqual(manifest["counts"]["by_shape"],
                             dict(sorted(builder.SHAPE_COUNTS.items())), company)
            self.assertEqual(manifest["counts"]["rows"], sum(builder.SHAPE_COUNTS.values()))
            self.assertEqual(manifest["counts"]["by_run_group"],
                             {"main": sum(builder.SHAPE_COUNTS.values())
                              - builder.SHAPE_COUNTS["upload_door"],
                              "upload_door": builder.SHAPE_COUNTS["upload_door"]})

    def test_rerunning_produces_the_same_bytes(self):
        first = self.root / "first"
        second = self.root / "second"
        self.assertEqual(_build(self.corpora, first)[0], 0)
        self.assertEqual(_build(self.corpora, second)[0], 0)
        for company in COMPANIES:
            self.assertEqual((first / company / "manifest.json").read_bytes(),
                             (second / company / "manifest.json").read_bytes(),
                             "the runbook quotes a sha256, so the build must be reproducible")

    def test_written_files_are_private_and_off_repo(self):
        _build(self.corpora, self.out)
        for company in COMPANIES:
            path = self.out / company / "manifest.json"
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode, 0o600, f"{path} mode {oct(mode)}")

    def test_it_refuses_to_write_inside_the_repo(self):
        out_stream = StringIO()
        with redirect_stdout(out_stream), redirect_stderr(out_stream):
            code = builder.main(["--corpora-root", str(self.corpora),
                                 "--out", str(REPO_ROOT / "probes_out")])
        self.assertEqual(code, 2)
        self.assertIn("refusing to write probes inside the repo", out_stream.getvalue())
        self.assertFalse((REPO_ROOT / "probes_out").exists())

    def test_every_question_scopes_to_exactly_its_expected_companies(self):
        _build(self.corpora, self.out)
        manifests = self.manifests()
        known = list(COMPANIES)
        aliases = manifests[COMPANIES[0]]["aliases"]
        for company, manifest in manifests.items():
            for bucket in manifest["probes"].values():
                for row in bucket:
                    detected = detect_companies(row["question"], known, aliases=aliases).companies
                    self.assertEqual(sorted(detected), sorted(row["expected_companies"]),
                                     f"{row['id']} scopes to {sorted(detected)}, "
                                     f"expected {sorted(row['expected_companies'])}")

    def test_the_near_miss_and_unknown_org_shapes_scope_to_nothing(self):
        _build(self.corpora, self.out)
        for row in self.rows():
            if row["shape"] in ("near_miss_word", "unknown_org", "unscoped"):
                self.assertEqual(row["expected_companies"], [], row["id"])

    def test_no_question_copies_a_sentence_from_the_corpus(self):
        _build(self.corpora, self.out)
        corpora = {company: json.loads((self.corpora / company / "manifest.json").read_text())
                   for company in COMPANIES}
        forbidden = builder._forbidden_word_runs(corpora)
        for row in self.rows():
            tokens = [token for token in re.split(r"\W+", row["question"].lower()) if token]
            windows = set(builder._windows(tokens, builder.MIN_ANSWER_TOKEN_RUN))
            self.assertFalse(windows & forbidden, f"{row['id']} carries corpus sentence words")

    def test_unanswerable_rows_use_codes_that_exist_in_no_document(self):
        _build(self.corpora, self.out)
        for company, manifest in self.manifests().items():
            corpus = json.loads((self.corpora / company / "manifest.json").read_text())
            na_codes = {fact["code"] for fact in corpus["unanswerable_facts"]}
            fact_codes = {fact["code"] for fact in corpus["facts"]}
            for row in manifest["probes"]["unanswerable"]:
                self.assertIn(row["expected_fact_code"], na_codes, row["id"])
                self.assertNotIn(row["expected_fact_code"], fact_codes, row["id"])

    def test_ids_are_unique_prefixed_and_cannot_be_mistaken_for_gl13_ids(self):
        _build(self.corpora, self.out)
        ids = [row["id"] for row in self.rows()]
        self.assertEqual(len(ids), len(set(ids)))
        for probe_id in ids:
            self.assertTrue(probe_id.startswith("gl10-"), probe_id)
            self.assertNotRegex(probe_id, builder.GL13_ID_RE)

    def test_shared_file_rows_point_at_the_generated_shared_document(self):
        _build(self.corpora, self.out)
        for company, manifest in self.manifests().items():
            corpus = json.loads((self.corpora / company / "manifest.json").read_text())
            paths = {entry["path"] for entry in corpus["files"]}
            shared = [path for path in paths if "shared-between-companies" in path]
            self.assertTrue(shared, "the generator must emit a shared file")
            for row in manifest["probes"]["shared_file"]:
                self.assertEqual(row["expected_shared_document"], shared[0], row["id"])
                self.assertEqual(row["expected_companies"], [company], row["id"])


class TestValidationRules(_Corpus):
    """Each rule must fire on a tampered row, and no file may be written when one does."""

    def rows_for(self):
        corpora = {company: json.loads((self.corpora / company / "manifest.json").read_text())
                   for company in COMPANIES}
        codes = builder._code_map(corpora)
        aliases = {company: [builder._alias_for(company)] for company in COMPANIES}
        rows = {company: builder.build_rows(company, corpora[company], corpora, aliases)
                for company in COMPANIES}
        return rows, corpora, codes, aliases

    def test_a_question_naming_the_wrong_company_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        rows[COMPANIES[0]][0]["question"] = (f"In {COMPANIES[1]}, what is recorded about the "
                                             f"cutover rehearsal and go/no-go criteria?")
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("scopes to" in problem for problem in problems), problems[:3])

    def test_a_question_copied_from_a_sentence_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        fact = corpora[COMPANIES[0]]["facts"][0]["text"]
        words = " ".join(fact.split()[:10])
        rows[COMPANIES[0]][0]["question"] = f"In {COMPANIES[0]}, explain: {words}?"
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("consecutive words" in problem for problem in problems), problems[:3])

    def test_an_unanswerable_code_that_exists_in_a_document_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        row = next(r for r in rows[COMPANIES[0]] if r["shape"] == "unanswerable")
        real_code = corpora[COMPANIES[0]]["facts"][0]["code"]
        row["expected_fact_code"] = real_code
        row["question"] = f"In {COMPANIES[0]}, what outcome was recorded under {real_code}?"
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("is not an " in p and "unanswerable fact" in p for p in problems),
                        problems[:3])

    def test_an_expected_document_that_is_not_in_the_corpus_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        row = next(r for r in rows[COMPANIES[0]] if r["shape"] == "scoped")
        row["expected_document"] = "transcripts/never-generated.txt"
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("is not a file in the corpus" in problem for problem in problems),
                        problems[:3])

    def test_a_duplicate_id_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        rows[COMPANIES[1]][0]["id"] = rows[COMPANIES[0]][0]["id"]
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("duplicate id" in problem for problem in problems), problems[:3])

    def test_an_alias_row_that_spells_the_whole_company_name_is_refused(self):
        rows, corpora, codes, aliases = self.rows_for()
        row = next(r for r in rows[COMPANIES[0]] if r["shape"] == "alias")
        row["question"] = (f"What does {COMPANIES[0]} report about the cutover rehearsal and "
                           f"go/no-go criteria?")
        problems = builder.validate(rows, corpora, codes, aliases, list(COMPANIES))
        self.assertTrue(any("only through" in problem for problem in problems), problems[:3])

    def test_a_refusal_writes_nothing(self):
        # Break the corpora so validation fails, and confirm no manifest appears.
        broken = self.root / "broken"
        broken.mkdir()
        (broken / COMPANIES[0]).mkdir()
        (broken / COMPANIES[0] / "manifest.json").write_text(json.dumps(
            {"company": COMPANIES[0], "company_code": "XX-0001", "seed": 1, "probes": {},
             "facts": [], "files": [], "topics": [], "unanswerable_facts": []}), encoding="utf-8")
        code, out = _build(broken, self.out)
        self.assertEqual(code, 2)
        self.assertIn("refusing to write", out)
        self.assertFalse(any((self.out / company / "manifest.json").exists()
                             for company in COMPANIES))


if __name__ == "__main__":
    unittest.main()
