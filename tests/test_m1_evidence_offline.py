"""Tests for the M1 evidence-investigation scripts. Investigation tooling: no app/ behaviour is
imported here except the gate rule itself (app/grounding.py), which these scripts measure.

Fixtures are invented in a temp directory — a tiny fake company (three planted facts across two .txt
documents, one unanswerable lookalike code) — so the offline measurements are pinned against
something whose answer key is known by construction. One class runs against the real GL-07 generated
corpora and the generated probe set when they are on disk, because those are the numbers the report
quotes; it skips otherwise, so the suite stays offline and portable.

What is pinned:
- run reconciliation: with evidence = "" the shipped rule fires on every identifier-asking row, and
  the invented fixture reproduces the real run's arithmetic (asking = fired, unexpected = asking -
  by-design);
- co-location verdicts are computed, not assumed: co_located, different_pieces and
  no_piece_with_sentence each occur in a fixture built to produce each case;
- evidence models behave as named: evidence_none never supports, evidence_all_docs supports every
  planted code and never a control code;
- the variant rules do what the report claims, including the cost of join_hyphen_split (it repairs
  a code opened at a hyphen and BREAKS a question naming two adjacent codes);
- the reference-shape probe distinguishes content-as-string from content-as-list: a list-valued
  content is counted as non-empty by app/generation.py's reader and as unreadable by the shipped
  gate's reader;
- membership counts evidence strings and answer strings separately, so "the model repeated the code"
  is never reported as "the evidence reached us".
"""

from __future__ import annotations

import importlib.util
import io
import json
import contextlib
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))


def _load(script: str, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, ROOT_DIR / "scripts" / script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


offline = _load("m1_evidence_offline.py", "m1_evidence_offline_for_tests")
probe = _load("m1_reference_shape_probe.py", "m1_reference_shape_probe_for_tests")

from app import grounding  # noqa: E402

REAL_CORPORA = Path("/home/maverick/work/synthetic-co")
REAL_PROBES = Path("/home/maverick/work/synthetic-co-probes")
REAL_COMPANIES = ("Northwind Chemicals", "Contoso Pharma")

CODE_A = "ACME-001-T01-01"
CODE_B = "ACME-001-T01-02"
CODE_C = "ACME-001-T02-01"
CONTROL = "ACME-001-NA-07"
COMPANY = "Acme Widgets"
COMPANY_CODE = "ACME-001"

SENTENCE_A = f"Entry {CODE_A}: the cutover window ran four hours with nine blockers."
SENTENCE_B = f"Entry {CODE_B}: the payroll reconciliation queue peaked at 612 items."
# CODE_C sits in the same document as SENTENCE_A but far from it, so the two land in different pieces.
FILLER = ("The warehouse rota was reviewed line by line. " * 40).strip()


def build_company(root: Path) -> None:
    company_dir = root / COMPANY
    (company_dir / "transcripts").mkdir(parents=True, exist_ok=True)
    doc_one = company_dir / "transcripts" / "one.txt"
    doc_one.write_text(
        "Decision log review\n\n" + SENTENCE_A + "\n\n" + FILLER + f"\n\nRecorded under {CODE_C}.\n",
        encoding="utf-8")
    doc_two = company_dir / "transcripts" / "two.txt"
    doc_two.write_text("Night shift notes\n\n" + SENTENCE_B + "\n", encoding="utf-8")
    manifest = {
        "company": COMPANY,
        "company_code": COMPANY_CODE,
        "files": [{"path": "transcripts/one.txt"}, {"path": "transcripts/two.txt"}],
        "facts": [
            {"fact_id": "F-1", "code": CODE_A, "document": "transcripts/one.txt", "text": SENTENCE_A},
            {"fact_id": "F-2", "code": CODE_B, "document": "transcripts/two.txt", "text": SENTENCE_B},
            {"fact_id": "F-3", "code": CODE_C, "document": "transcripts/one.txt", "text": SENTENCE_A},
        ],
    }
    (company_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def build_probes(root: Path) -> None:
    probe_dir = root / COMPANY
    probe_dir.mkdir(parents=True, exist_ok=True)
    probes = {
        "scoped": [
            {"id": "idn-a1", "probe_kind": "identifier_answerable", "expected_gate": "not_fire",
             "expected_gate_asked": [CODE_A], "expected_unsupported": [],
             "expected_document": "transcripts/one.txt",
             "question": f"What outcome was recorded under {CODE_A}?"},
            {"id": "idn-a2", "probe_kind": "identifier_answerable", "expected_gate": "not_fire",
             "expected_gate_asked": [CODE_B], "expected_unsupported": [],
             "expected_document": "transcripts/two.txt",
             "question": f"What outcome was recorded under {CODE_B}?"},
            {"id": "idn-hyphen", "probe_kind": "edge_hyphen_as_space", "expected_gate": "fire",
             "expected_gate_asked": [CODE_A], "expected_unsupported": [CODE_A],
             "expected_document": "transcripts/one.txt",
             "question": "What outcome was recorded under ACME-001 T01-01?"},
            {"id": "idn-lower", "probe_kind": "edge_lowercase_code", "expected_gate": "no_identifier",
             "expected_gate_asked": [], "expected_unsupported": [],
             "expected_document": "transcripts/one.txt",
             "question": "What outcome was recorded under acme-001-t01-01?"},
            {"id": "idn-two", "probe_kind": "two_code_both_present", "expected_gate": "not_fire",
             "expected_gate_asked": [CODE_A, CODE_B], "expected_unsupported": [],
             "expected_document": "transcripts/one.txt",
             "question": f"How do {CODE_A} and {CODE_B} compare?"},
        ],
        "unanswerable": [
            {"id": "idn-u1", "probe_kind": "control_unanswerable", "expected_gate": "fire",
             "expected_gate_asked": [CONTROL], "expected_unsupported": [CONTROL],
             "fact_code": CONTROL,
             "question": f"What outcome was recorded under {CONTROL}?"},
        ],
    }
    (probe_dir / "manifest.json").write_text(
        json.dumps({"company": COMPANY, "company_code": COMPANY_CODE, "probes": probes}, indent=2),
        encoding="utf-8")


class OfflineHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.corpus_root = self.root / "corpus"
        self.probe_root = self.root / "probes"
        build_company(self.corpus_root)
        build_probes(self.probe_root)
        self.addCleanup(self.tmp.cleanup)

    # ── loaders ────────────────────────────────────────────────────────────
    def test_loaders_read_the_scorers_manifest_format(self):
        rows = offline.load_probes(self.probe_root, COMPANY)
        self.assertEqual(len(rows), 6)
        self.assertEqual({row["company_code"] for row in rows}, {COMPANY_CODE})
        self.assertIn("idn-u1", {row["id"] for row in rows})
        pieces = offline.corpus_pieces(self.corpus_root, COMPANY)
        self.assertEqual(sorted(pieces), ["transcripts/one.txt", "transcripts/two.txt"])

    def test_missing_probe_manifest_fails_loudly(self):
        with self.assertRaises(FileNotFoundError):
            offline.load_probes(self.probe_root / "nowhere", COMPANY)

    # ── run reconciliation (evidence = "") ────────────────────────────────
    def test_empty_evidence_explains_an_all_fire_run(self):
        recon = offline.run_reconciliation(self.probe_root, (COMPANY,))
        self.assertEqual(recon["probe_rows"], 6)
        self.assertEqual(recon["answerable_rows"], 5)
        self.assertEqual(recon["control_rows"], 1)
        # the lowercase row yields no identifier for the shipped rule, so it cannot fire
        self.assertEqual(recon["identifier_asking_rows"], 4)
        self.assertEqual(recon["answerable_rows_without_identifier"], 1)
        self.assertEqual(recon["would_fire"], 4)
        self.assertEqual(recon["by_design_fires"], 1)         # the hyphen row only
        self.assertEqual(recon["unexpected_fires"], 3)        # two answerable + the two-code row
        self.assertEqual(recon["manifest_vs_reextraction_disagreements"], [])

    # ── co-location ───────────────────────────────────────────────────────
    def test_co_location_reports_each_verdict_it_can_tell_apart(self):
        """At the configured window a synthetic document is one piece, so everything is co-located;
        the verdict that needs a real split is produced below by shrinking the window."""
        report = offline.co_location(self.corpus_root, (COMPANY,))
        cells = report["companies"][COMPANY]["cells"]
        self.assertEqual(cells["text|co_located"], 3)
        self.assertEqual(cells.get("text|no_piece_with_sentence", 0), 0)
        self.assertEqual(cells["code_supported_corpus_wide"], 3)
        self.assertEqual(cells["code_supported_own_doc"], 3)
        # two planted sentences carry their code inline; F-3's does not
        self.assertEqual(cells["fact_text_contains_code"], 2)
        self.assertEqual(cells["fact_text_token_supports_code"], 2)
        # the company code is a prefix of every planted code, never a standalone token of its own
        self.assertEqual(cells["company_code_standalone_pieces"], 0)
        self.assertEqual(cells["pieces_total"], 2)             # one piece per document

    def test_a_smaller_piece_window_can_split_a_code_from_its_fact(self):
        """The split the lead's brief asked about, produced on demand instead of assumed away."""
        cells = offline.co_location(self.corpus_root, (COMPANY,), max_piece_tokens=200)["totals"]
        self.assertGreater(cells["pieces_total"], 2)
        self.assertEqual(cells["text|different_pieces"], 1)    # F-3's code sits far from F-3's text
        self.assertEqual(cells["text|co_located"], 2)

    def test_office_route_is_labelled_as_office(self):
        """The office route is measured against OUR extraction of the file bytes, which is not
        LightRAG's extractor. The label is what this pins; the number is an approximation."""
        company_dir = self.corpus_root / COMPANY
        (company_dir / "deck").mkdir()
        with zipfile.ZipFile(company_dir / "deck" / "deck.docx", "w") as archive:
            archive.writestr("word/document.xml",
                             "<w:p><w:t>" + SENTENCE_B + "</w:t></w:p>")
        manifest = json.loads((company_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"].append({"path": "deck/deck.docx"})
        manifest["facts"].append({"fact_id": "F-4", "code": CODE_B, "document": "deck/deck.docx",
                                  "text": SENTENCE_B})
        (company_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        cells = offline.co_location(self.corpus_root, (COMPANY,))["companies"][COMPANY]["cells"]
        self.assertEqual(cells["route|office"], 1)
        self.assertEqual(cells["office|co_located"], 1)

    # ── variant rules ─────────────────────────────────────────────────────
    def test_shipped_extraction_ignores_lowercase_and_requires_hyphens(self):
        self.assertEqual(offline.asked_codes("under acme-001-t01-01?", COMPANY_CODE, set()), [])
        self.assertEqual(offline.asked_codes("under ACME-001-T01-01?", COMPANY_CODE, set()),
                         ["ACME-001-T01-01"])

    def test_fold_case_sees_lowercase_and_supports_it(self):
        self.assertEqual(offline.asked_codes("under acme-001-t01-01?", COMPANY_CODE, {"fold_case"}),
                         ["acme-001-t01-01"])
        self.assertTrue(offline.supported_by("acme-001-t01-01", SENTENCE_A, {"fold_case"}))
        self.assertFalse(offline.supported_by("acme-001-t01-01", SENTENCE_A, set()))

    def test_join_hyphen_split_repairs_a_split_code(self):
        self.assertEqual(offline.asked_codes("under ACME-001 T01-01?", COMPANY_CODE, set()),
                         ["ACME-001", "T01-01"])
        self.assertEqual(offline.asked_codes("under ACME-001 T01-01?", COMPANY_CODE,
                                            {"join_hyphen_split"}),
                         ["ACME-001-T01-01"])

    def test_join_hyphen_split_breaks_a_two_code_question(self):
        # Measured cost, pinned: pairing adjacent tokens cannot know whether the pair was one code
        # opened at a hyphen or two separate codes.
        joined = offline.asked_codes(f"How do {CODE_A} and {CODE_B} compare?", COMPANY_CODE,
                                     {"join_hyphen_split"})
        self.assertEqual(joined, [f"{CODE_A}-{CODE_B}"])
        self.assertFalse(all(offline.supported_by(code, SENTENCE_A, {"join_hyphen_split"})
                             for code in joined))

    def test_reconstruct_if_known_tells_a_split_code_from_two_codes(self):
        known = {CODE_A, CODE_B}
        split = offline.asked_codes("under ACME-001 T01-01?", COMPANY_CODE,
                                   {"reconstruct_if_known"}, known)
        self.assertEqual(split, [CODE_A])                      # joined form is a real code
        pair = offline.asked_codes(f"compare {CODE_A} and {CODE_B}", COMPANY_CODE,
                                  {"reconstruct_if_known"}, known)
        self.assertEqual(pair, [CODE_A, CODE_B])               # joined form is not a real code
        invented = offline.asked_codes(f"what about {CODE_A} and ZZ-99-9?", COMPANY_CODE,
                                       {"reconstruct_if_known"}, known)
        self.assertEqual(invented, [CODE_A, "ZZ-99-9"])        # the invented half survives to fire

    def test_evidence_identifiers_reads_the_corpus_not_the_question(self):
        self.assertEqual(offline.evidence_identifiers(SENTENCE_A), {CODE_A})
        self.assertEqual(offline.evidence_identifiers("no codes here"), set())

    def test_reconstruct_if_known_with_corpus_evidence_reaches_the_floor(self):
        table = offline.gate_simulation(self.probe_root, (COMPANY,), self.corpus_root)["by_evidence"]
        cell = table["evidence_all_docs"]["+reconstruct_if_known"]
        self.assertEqual(cell["answerable_refused_by_kind"], {})   # the hyphen row is recovered
        self.assertEqual(cell["control_refused"], 1)

    def test_drop_company_code_removes_a_bare_company_reference(self):
        self.assertEqual(offline.asked_codes(f"how did {COMPANY_CODE} do?", COMPANY_CODE, set()),
                         [COMPANY_CODE])
        self.assertEqual(offline.asked_codes(f"how did {COMPANY_CODE} do?", COMPANY_CODE,
                                            {"drop_company_code"}), [])

    def test_all_unsupported_is_stricter_than_the_shipped_rule(self):
        codes = [CODE_A, CONTROL]
        self.assertTrue(offline.fires(codes, SENTENCE_A, set()))               # any unsupported
        self.assertFalse(offline.fires(codes, SENTENCE_A, {"stricter_all_unsupported"}))
        self.assertTrue(offline.fires([CONTROL, "ZZ-9"], SENTENCE_A,
                                      {"stricter_all_unsupported"}))
        self.assertFalse(offline.fires([], SENTENCE_A, set()))                 # nothing asked

    # ── evidence models ───────────────────────────────────────────────────
    def test_evidence_models_separate_blindness_from_absence(self):
        result = offline.gate_simulation(self.probe_root, (COMPANY,), self.corpus_root)
        by_model = result["by_evidence"]
        # the lowercase row asks nothing the shipped rule can see, so it cannot fire: 4 of 5
        self.assertEqual(by_model["evidence_none"]["shipped_rule"]["answerable_refused"], 4)
        self.assertEqual(by_model["evidence_none"]["shipped_rule"]["answerable_refused_by_kind"]
                         ["two_code_both_present"], 1)
        # own document: only the fact's own document is visible, so the two-code row still fires
        own = by_model["evidence_own_doc"]["shipped_rule"]
        self.assertEqual(own["answerable_refused_by_kind"].get("two_code_both_present"), 1)
        self.assertEqual(own["answerable_refused_by_kind"].get("identifier_answerable", 0), 0)
        # whole corpus: every planted code is supported, every control still refused
        corpus = by_model["evidence_all_docs"]["shipped_rule"]
        self.assertEqual(corpus["answerable_refused_by_kind"],
                         {"edge_hyphen_as_space": 1})
        self.assertEqual(corpus["control_refused"], 1)
        self.assertEqual(corpus["control_rows"], 1)

    def test_fold_case_recovers_lowercase_rows_without_losing_the_control(self):
        table = offline.gate_simulation(self.probe_root, (COMPANY,), self.corpus_root)["by_evidence"]["evidence_all_docs"]
        self.assertEqual(table["+fold_case"]["answerable_refused_by_kind"],
                         {"edge_hyphen_as_space": 1})
        self.assertEqual(table["+fold_case"]["control_refused"], 1)


class ReferenceShapeProbe(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        build_probes(self.root)
        self.addCleanup(self.tmp.cleanup)

    RAW_LIST_SHAPED = {
        "response": f"Entry {CODE_A} ran four hours with nine blockers.",
        "references": [
            {"reference_id": 1, "file_path": "transcripts/one.txt", "line_numbers": [3, 4],
             "content": [SENTENCE_A]},
            {"reference_id": 2, "file_path": "transcripts/two.txt", "content": []},
        ],
    }

    def test_content_as_list_is_visible_to_generation_but_not_to_the_gate(self):
        report = probe.reference_report(self.RAW_LIST_SHAPED)
        self.assertEqual(report["reference_count"], 2)
        self.assertEqual(report["content_key_counts"]["content"], 2)
        self.assertEqual(report["content_key_list_valued"]["content"], 2)
        self.assertEqual(report["references_with_nonempty_content"], 1)
        self.assertEqual(report["readable_by_the_shipped_gate"], 0)

    def test_content_as_string_is_readable_by_the_gate(self):
        response = {"references": [{"reference_id": 1, "content": SENTENCE_A}]}
        self.assertEqual(probe.reference_report(response)["readable_by_the_shipped_gate"], 1)

    def test_references_absent_is_reported_not_crashed(self):
        report = probe.reference_report({"response": "x"})
        self.assertFalse(report["references_present"])
        self.assertEqual(report["top_level_keys"], ["response"])

    def test_membership_separates_evidence_from_answer(self):
        report = probe.membership_report(self.RAW_LIST_SHAPED, [CODE_A])
        hits = report["asked_code_token_hits_by_bucket"]
        self.assertGreater(hits[CODE_A]["evidence"], 0)
        self.assertEqual(hits[CODE_A]["answer"], 1)          # the answer echoed it back
        self.assertTrue(report["evidence_reached_the_gateway"])

    def test_a_code_only_in_the_answer_is_not_evidence(self):
        response = {"response": f"{CODE_A} was recorded.",
                    "references": [{"reference_id": 1, "content": "unrelated notes"}]}
        report = probe.membership_report(response, [CODE_A])
        self.assertEqual(report["asked_code_token_hits_by_bucket"][CODE_A].get("evidence", 0), 0)
        self.assertFalse(report["evidence_reached_the_gateway"])

    def test_question_echo_is_never_counted_as_a_hit(self):
        response = {"query": f"what about {CODE_A}?", "references": []}
        self.assertFalse(probe.membership_report(response, [CODE_A])["evidence_reached_the_gateway"])
        self.assertEqual(probe.walk_strings(response), [])

    def test_shape_reports_types_lengths_and_hashes_but_never_text(self):
        rendered = probe.describe(self.RAW_LIST_SHAPED)
        self.assertIn("references[].content[]: str(len=", rendered)
        self.assertIn("line_numbers: list(n=2)", rendered)
        self.assertNotIn(SENTENCE_A, rendered)
        self.assertIn("sha256=", rendered)

    def test_cli_end_to_end_against_a_fake_stack(self):
        calls = []

        def fake_post(url, payload, api_key, timeout):
            calls.append((url, payload, api_key))
            if "gateway" in url:
                return 200, {"ok": True, "data": {"answer": f"Entry {CODE_A} ran four hours.",
                                                  "citations": [{"file_path": "one.txt"}]}}
            return 200, self.RAW_LIST_SHAPED

        original = probe.post
        probe.post = fake_post
        self.addCleanup(setattr, probe, "post", original)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            status = probe.main(["--gateway-url", "http://gateway.invalid", "--company", COMPANY,
                                 "--probe-id", "idn-a1", "--probe-root", str(self.root),
                                 "--raw-lightrag-url", "http://lightrag.invalid"])
        printed = buffer.getvalue()
        self.assertEqual(status, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][1]["include_chunk_content"], True)
        self.assertIn("readable_by_the_shipped_gate", printed)
        self.assertIn("expected_document=transcripts/one.txt", printed)
        self.assertNotIn(SENTENCE_A, printed)


@unittest.skipUnless(all((REAL_CORPORA / company / "manifest.json").exists() and
                         (REAL_PROBES / company / "manifest.json").exists()
                         for company in REAL_COMPANIES),
                     "generated GL-07 corpora and probe set are not on disk")
class RealGl07Measurements(unittest.TestCase):
    """The numbers the report quotes, pinned where they come from."""

    @classmethod
    def setUpClass(cls):
        cls.recon = offline.run_reconciliation(REAL_PROBES, REAL_COMPANIES)
        cls.sim = offline.gate_simulation(REAL_PROBES, REAL_COMPANIES, REAL_CORPORA)
        cls.co = offline.co_location(REAL_CORPORA, REAL_COMPANIES)

    def test_the_run_reconciles_to_empty_evidence(self):
        # Hermes reported 102 identifier-asking rows, 96 fired, 86 unexpected.
        self.assertEqual(self.recon["probe_rows"], 114)
        self.assertEqual(self.recon["answerable_rows"], 102)
        self.assertEqual(self.recon["control_rows"], 12)
        self.assertEqual(self.recon["identifier_asking_rows"], 96)
        self.assertEqual(self.recon["would_fire"], 96)
        self.assertEqual(self.recon["by_design_fires"], 10)
        self.assertEqual(self.recon["unexpected_fires"], 86)
        self.assertEqual(self.recon["manifest_vs_reextraction_disagreements"], [])

    def test_codes_are_always_in_a_piece_of_their_own_document(self):
        cells = self.co["totals"]
        self.assertEqual(cells["route|text"] + cells["route|office"], 78)
        self.assertEqual(cells["code_supported_own_doc"], 78)
        self.assertEqual(cells["code_supported_corpus_wide"], 78)
        # wherever the fact sentence survives byte-identical, the code sits in the same piece
        self.assertEqual(cells.get("text|different_pieces", 0) + cells.get("office|different_pieces", 0), 0)
        self.assertEqual(cells["text|co_located"], 29)
        self.assertEqual(cells["office|co_located"], 22)
        self.assertEqual(cells["text|no_piece_with_sentence"], 7)
        self.assertEqual(cells["office|no_piece_with_sentence"], 20)

    def test_the_planted_sentence_carries_its_own_code(self):
        """The finding that reframes the run: every planted fact sentence names its code inline, and
        that sentence alone passes the gate's token test. A chunk carrying the fact CANNOT have
        omitted the code - they are the same bytes."""
        cells = self.co["totals"]
        self.assertEqual(cells["fact_text_contains_code"], 78)
        self.assertEqual(cells["fact_text_token_supports_code"], 78)

    def test_shrinking_the_piece_window_does_not_split_a_code_from_its_fact(self):
        sweep = offline.co_location_sweep(REAL_CORPORA, REAL_COMPANIES, budgets=(None, 400))
        for label, cells in sweep.items():
            self.assertEqual(cells.get("text|different_pieces", 0)
                             + cells.get("office|different_pieces", 0), 0, label)
        self.assertGreater(sweep["t400"]["pieces_total"], sweep["configured"]["pieces_total"])

    def test_company_code_is_never_a_standalone_token(self):
        self.assertEqual(self.co["totals"]["company_code_standalone_pieces"], 0)
        self.assertGreater(self.co["totals"]["pieces_total"], 200)

    def test_corpus_evidence_and_shipped_rule_reach_the_by_design_floor(self):
        table = self.sim["by_evidence"]
        self.assertEqual(table["evidence_none"]["shipped_rule"]["answerable_refused"], 96)
        self.assertEqual(table["evidence_all_docs"]["shipped_rule"]["answerable_refused"], 10)
        self.assertEqual(table["evidence_all_docs"]["shipped_rule"]["answerable_refused_by_kind"],
                         {"edge_hyphen_as_space": 6, "two_code_one_absent": 4})
        self.assertEqual(table["evidence_all_docs"]["shipped_rule"]["control_refused"], 12)
        # own-document evidence additionally refuses the two-code rows: their codes live in
        # different documents, so one document's pieces cannot support both.
        self.assertEqual(table["evidence_own_doc"]["shipped_rule"]["answerable_refused_by_kind"],
                         {"edge_hyphen_as_space": 6, "two_code_both_present": 4,
                          "two_code_one_absent": 4})

    def test_case_folding_adds_the_lowercase_rows_and_supports_them(self):
        table = self.sim["by_evidence"]["evidence_all_docs"]
        self.assertEqual(table["+fold_case"]["answerable_refused_by_kind"],
                         table["shipped_rule"]["answerable_refused_by_kind"])
        self.assertNotIn("edge_lowercase_code",
                         table["+fold_case"]["answerable_refused_by_kind"])

    def test_reconstruct_if_known_is_the_only_variant_that_fixes_hyphen_without_losing_catch(self):
        table = self.sim["by_evidence"]["evidence_all_docs"]
        cell = table["+reconstruct_if_known"]
        self.assertEqual(cell["answerable_refused_by_kind"], {"two_code_one_absent": 4})
        self.assertEqual(cell["control_refused"], 12)
        # own-document evidence still refuses the two-code rows: their codes live in different
        # documents, so this variant cannot rescue a retrieval miss.
        self.assertEqual(self.sim["by_evidence"]["evidence_own_doc"]["+reconstruct_if_known"]
                         ["answerable_refused_by_kind"],
                         {"two_code_both_present": 4, "two_code_one_absent": 4})

    def test_join_hyphen_split_recovers_six_and_breaks_four(self):
        table = self.sim["by_evidence"]["evidence_all_docs"]
        kinds = table["+join_hyphen_split"]["answerable_refused_by_kind"]
        self.assertNotIn("edge_hyphen_as_space", kinds)
        self.assertEqual(kinds.get("two_code_both_present"), 4)

    def test_drop_company_code_changes_nothing_on_this_probe_set(self):
        # No probe asks the bare company code, so the probe set cannot measure that decision.
        table = self.sim["by_evidence"]["evidence_all_docs"]
        self.assertEqual(table["+drop_company_code"]["answerable_refused"],
                         table["shipped_rule"]["answerable_refused"])


if __name__ == "__main__":
    unittest.main()
