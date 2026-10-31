"""Offline tests for scripts/make_synthetic_company.py (GL-07).

Every document here is invented by the generator; no test reads ``source_docs/``,
``lightrag_store/``, ``state/`` or any probe. What the suite holds the generator to:

- determinism: the same seed and company produce byte-identical files, including the
  OOXML parts whose zip entries python-pptx would otherwise stamp with the clock;
- the manifest is a true map: every file it names exists with the hash it claims, and
  every planted fact really is in the document it names;
- the two companies are disjoint where they must be (facts, codes, unique topics) and
  shared where they must be (the cross-company duplicate file, the shared topics);
- the files open with the libraries the worker uses, and the PDF keeps the shapes the
  chunker is expected to handle: numbered headings, ALL-CAPS headings, tables, a
  running header and footer on every page, at least 40 pages;
- the probe shapes the manifest emits are answerable: scoped questions name a company
  that holds the fact, and unanswerable questions name a code that is in nothing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "make_synthetic_company.py"

SPEC = importlib.util.spec_from_file_location("make_synthetic_company", SCRIPT)
gen = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
# Registered before exec: @dataclass resolves annotations through sys.modules, and
# an unregistered module is not found there.
import sys as _sys

_sys.modules[SPEC.name] = gen
SPEC.loader.exec_module(gen)

HAS_PPTX = importlib.util.find_spec("pptx") is not None

COMPANY_A = "Northwind Chemicals"
COMPANY_B = "Contoso Pharma"
SEED = 20261101


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def pdf_text(path: Path) -> str:
    """Text of a generated PDF, without depending on a PDF library.

    The generator writes uncompressed text-showing operators, so the strings are
    readable directly. pypdf and poppler are exercised separately where installed.
    """
    payload = path.read_bytes().decode("latin-1")
    raw = re.findall(r"\(((?:[^()]|\\.)*)\)\s*Tj", payload)
    unescaped = [
        re.sub(r"\\([()\\])", r"\1", s).replace("\\(", "(").replace("\\)", ")")
        for s in raw
    ]
    return "\n".join(unescaped)


def pdf_page_count(path: Path) -> int:
    return path.read_bytes().count(b"/Type /Page ")


def docx_text(path: Path) -> str:
    """DOCX text via the repo's dependency-free reader (the worker's fallback)."""
    import sys

    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from app.docx_extract import extract_docx_text

    return extract_docx_text(path)


def pptx_text(path: Path) -> str:
    """PPTX text via the repo extractor, which reads tables, notes and groups."""
    import sys

    if str(ROOT_DIR) not in sys.path:
        sys.path.insert(0, str(ROOT_DIR))
    from app.pptx_extract import extract_pptx_text

    return extract_pptx_text(path)


def document_text(path: Path) -> str:
    if path.suffix == ".pdf":
        return pdf_text(path)
    if path.suffix == ".docx":
        return docx_text(path)
    if path.suffix == ".pptx":
        return pptx_text(path)
    if path.suffix == ".json":
        return path.read_text(encoding="utf-8")
    return path.read_text(encoding="utf-8")


def build(root: Path, company: str, seed: int = SEED) -> tuple[Path, dict]:
    return gen.generate_company(company, seed, root)


class GeneratedCorpus(unittest.TestCase):
    """One corpus for both companies, built once."""

    @classmethod
    def setUpClass(cls) -> None:
        if not HAS_PPTX:
            raise unittest.SkipTest("python-pptx is not installed; the generator cannot build the deck")
        cls.tmp = Path(tempfile.mkdtemp(prefix="synthco-test-"))
        cls.dir_a, cls.manifest_a = build(cls.tmp, COMPANY_A)
        cls.dir_b, cls.manifest_b = build(cls.tmp, COMPANY_B)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def files(self, company_dir: Path) -> list[Path]:
        return sorted(p for p in company_dir.rglob("*") if p.is_file() and p.name != gen.MANIFEST_NAME)

    def corpus_text(self, company_dir: Path) -> str:
        return normalise("\n".join(document_text(p) for p in self.files(company_dir)))


class Determinism(GeneratedCorpus):
    def test_same_seed_is_byte_identical(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="synthco-rerun-"))
        try:
            again, manifest = build(other, COMPANY_A)
            first = {
                str(p.relative_to(self.dir_a)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.files(self.dir_a)
            }
            second = {
                str(p.relative_to(again)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.files(again)
            }
            self.assertEqual(first, second, "a rerun with the same seed changed file bytes")
            self.assertEqual(manifest, self.manifest_a, "the manifest is not stable across runs")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_a_different_seed_keeps_the_shape_and_changes_the_facts(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="synthco-seed-"))
        try:
            _, manifest = build(other, COMPANY_A, seed=SEED + 1)
            self.assertEqual(manifest["counts"], self.manifest_a["counts"])
            texts_new = {fact["text"] for fact in manifest["facts"]}
            texts_old = {fact["text"] for fact in self.manifest_a["facts"]}
            self.assertFalse(texts_new & texts_old, "facts did not change with the seed")
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_zip_entries_do_not_carry_the_clock(self) -> None:
        for company_dir in (self.dir_a, self.dir_b):
            for path in company_dir.rglob("*"):
                if path.suffix in (".docx", ".pptx"):
                    for info in zipfile.ZipFile(path).infolist():
                        self.assertEqual(info.date_time, gen.FIXED_ZIP_DATE,
                                         f"{path.name}:{info.filename} is timestamped")


class ManifestIsATrueMap(GeneratedCorpus):
    def test_every_listed_file_exists_with_the_claimed_hash(self) -> None:
        for company_dir, manifest in ((self.dir_a, self.manifest_a), (self.dir_b, self.manifest_b)):
            listed = {entry["path"]: entry for entry in manifest["files"]}
            on_disk = {str(p.relative_to(company_dir)): p for p in self.files(company_dir)}
            self.assertEqual(set(listed), set(on_disk), "manifest file list and disk disagree")
            for rel, entry in listed.items():
                payload = on_disk[rel].read_bytes()
                self.assertEqual(entry["sha256"], hashlib.sha256(payload).hexdigest(), rel)
                self.assertEqual(entry["bytes"], len(payload), rel)

    def test_every_planted_fact_is_in_the_document_it_names(self) -> None:
        for manifest in (self.manifest_a, self.manifest_b):
            for fact in manifest["facts"]:
                document = fact["document"]
                self.assertTrue(document, fact["fact_id"])
                path = (self.dir_a if manifest is self.manifest_a else self.dir_b) / document
                text = normalise(document_text(path))
                self.assertIn(normalise(fact["text"]), text,
                              f"{fact['fact_id']} missing from {document}")
                self.assertIn(fact["code"], text, f"{fact['fact_id']} code missing")

    def test_file_topic_and_fact_lists_have_no_duplicates(self) -> None:
        for manifest in (self.manifest_a, self.manifest_b):
            for entry in manifest["files"]:
                self.assertEqual(entry["topics"], sorted(set(entry["topics"])), entry["path"])
                self.assertEqual(entry["fact_ids"], sorted(set(entry["fact_ids"])), entry["path"])

    def test_fact_codes_are_unique_within_and_across_companies(self) -> None:
        codes = [fact["code"] for fact in self.manifest_a["facts"]]
        codes += [fact["code"] for fact in self.manifest_b["facts"]]
        self.assertEqual(len(codes), len(set(codes)), "a planted fact code is reused")

    def test_counts_match_the_document_set(self) -> None:
        counts = self.manifest_a["counts"]
        kinds = {}
        for entry in self.manifest_a["files"]:
            kinds[entry["kind"]] = kinds.get(entry["kind"], 0) + 1
        self.assertEqual(counts["transcripts"], 8)
        self.assertEqual(counts["docx"], 1)
        self.assertEqual(counts["pdf"], 1)
        self.assertEqual(counts["pptx"], 1)
        self.assertEqual(kinds["transcript"], 8)
        self.assertGreaterEqual(counts["shared_topics"], gen.MIN_SHARED_TOPICS)
        self.assertEqual(counts["unique_topics"], gen.UNIQUE_TOPIC_COUNT)


class CompaniesAreDisjointWhereTheyMustBe(GeneratedCorpus):
    def test_no_fact_of_one_company_appears_in_the_other(self) -> None:
        corpus_a, corpus_b = self.corpus_text(self.dir_a), self.corpus_text(self.dir_b)
        for fact in self.manifest_a["facts"]:
            self.assertNotIn(normalise(fact["text"]), corpus_b, fact["fact_id"])
        for fact in self.manifest_b["facts"]:
            self.assertNotIn(normalise(fact["text"]), corpus_a, fact["fact_id"])

    def test_unique_topics_do_not_collide_between_two_companies(self) -> None:
        unique_a = {t["id"] for t in self.manifest_a["topics"] if t["kind"] == "unique"}
        unique_b = {t["id"] for t in self.manifest_b["topics"] if t["kind"] == "unique"}
        self.assertEqual(len(unique_a), gen.UNIQUE_TOPIC_COUNT)
        self.assertEqual(len(unique_b), gen.UNIQUE_TOPIC_COUNT)
        self.assertFalse(unique_a & unique_b, "both companies describe the same unique topic")

    def test_forbidding_a_unique_topic_moves_it(self) -> None:
        other = Path(tempfile.mkdtemp(prefix="synthco-forbid-"))
        try:
            forbidden = sorted(
                t["id"] for t in self.manifest_b["topics"] if t["kind"] == "unique"
            )
            _, manifest = gen.generate_company(COMPANY_B, SEED, other, forbidden)
            unique = {t["id"] for t in manifest["topics"] if t["kind"] == "unique"}
            self.assertFalse(unique & set(forbidden))
            self.assertEqual(len(unique), gen.UNIQUE_TOPIC_COUNT)
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_unanswerable_codes_are_in_no_document_of_either_company(self) -> None:
        corpus = self.corpus_text(self.dir_a) + " " + self.corpus_text(self.dir_b)
        for manifest in (self.manifest_a, self.manifest_b):
            self.assertEqual(len(manifest["unanswerable_facts"]), gen.UNANSWERABLE_COUNT)
            for entry in manifest["unanswerable_facts"]:
                self.assertNotIn(entry["code"], corpus)
                self.assertEqual(entry["document"], "")


class DuplicatesAndProbes(GeneratedCorpus):
    def test_two_files_inside_one_company_are_byte_identical(self) -> None:
        pair = self.manifest_a["duplicates"]["within_company_pair"]
        self.assertEqual(len(pair), 2)
        first, second = (self.dir_a / rel for rel in pair)
        self.assertEqual(first.read_bytes(), second.read_bytes())
        self.assertEqual(
            self.manifest_a["duplicates"]["within_company_pair_sha256"],
            hashlib.sha256(first.read_bytes()).hexdigest(),
        )

    def test_the_shared_file_is_the_same_bytes_in_both_companies(self) -> None:
        shared = gen.SHARED_FILE_REL
        path_a, path_b = self.dir_a / shared, self.dir_b / shared
        self.assertEqual(path_a.read_bytes(), path_b.read_bytes())
        self.assertEqual(
            self.manifest_a["duplicates"]["cross_company_shared"]["sha256"],
            self.manifest_b["duplicates"]["cross_company_shared"]["sha256"],
        )
        self.assertNotIn(COMPANY_B, path_a.read_text(encoding="utf-8"))

    def test_emit_shared_file_writes_the_same_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "emitted.txt"
            self.assertEqual(
                gen.main(["--seed", str(SEED), "--emit-shared-file", str(target), "--quiet"]), 0
            )
            self.assertEqual(
                target.read_bytes(), (self.dir_a / gen.SHARED_FILE_REL).read_bytes()
            )

    def test_scoped_probes_name_a_company_that_holds_the_fact(self) -> None:
        facts = {fact["fact_id"]: fact for fact in self.manifest_a["facts"]}
        probes = self.manifest_a["probes"]["scoped"]
        self.assertGreaterEqual(len(probes), 10)
        for probe in probes:
            fact = facts[probe["expected_fact_id"]]
            self.assertEqual(probe["expected_company"], COMPANY_A)
            self.assertEqual(probe["expected_document"], fact["document"])
            self.assertEqual(probe["expected_fact_code"], fact["code"])
            self.assertIn(COMPANY_A, probe["question"])

    def test_cross_company_probes_cover_the_shared_topics(self) -> None:
        shared_ids = {t["id"] for t in self.manifest_a["topics"] if t["kind"] == "shared"}
        probes = self.manifest_a["probes"]["cross_company"]
        self.assertEqual({probe["topic"] for probe in probes}, shared_ids)
        for probe in probes:
            self.assertTrue(probe["this_company_fact_ids"], probe["topic"])
            self.assertTrue(probe["this_company_documents"], probe["topic"])

    def test_unanswerable_probes_point_at_a_code_nothing_holds(self) -> None:
        corpus = self.corpus_text(self.dir_a) + " " + self.corpus_text(self.dir_b)
        for probe in self.manifest_a["probes"]["unanswerable"]:
            self.assertNotIn(probe["fact_code"], corpus)


class DocumentShapes(GeneratedCorpus):
    def test_transcripts_are_speaker_turns_with_a_date_in_the_filename(self) -> None:
        transcripts = sorted(self.dir_a.glob(f"{gen.TRANSCRIPT_DIR_REL}/*.txt"))
        self.assertEqual(len(transcripts), 8)
        for path in transcripts:
            self.assertRegex(path.name, r"^\d{4}-\d{2}-\d{2} ")
            text = path.read_text(encoding="utf-8")
            speakers = set(re.findall(r"(?m)^([A-Z]\. [A-Za-z'-]+):", text))
            self.assertGreaterEqual(len(speakers), 3)
            self.assertLessEqual(len(speakers), 6)
            self.assertIn("\n\n", text)

    def test_pdf_has_the_page_floor_and_the_repeated_page_furniture(self) -> None:
        pdf = next(self.dir_a.glob("documents/*.pdf"))
        self.assertGreaterEqual(pdf_page_count(pdf), gen.MIN_PDF_PAGES)
        text = pdf_text(pdf)
        self.assertGreaterEqual(text.count("uncontrolled when printed"), gen.MIN_PDF_PAGES)
        self.assertGreaterEqual(text.count(f"{COMPANY_A} - ERP integration analysis"),
                                gen.MIN_PDF_PAGES)
        self.assertRegex(text, r"(?m)^\d+\.\s+[A-Z]")
        self.assertRegex(text, r"(?m)^[A-Z][A-Z0-9 ,\-\(\)/]{6,}$")
        self.assertIn("  |  ", text)

    def test_docx_has_a_table_whose_first_row_is_a_header_row(self) -> None:
        docx = next(self.dir_a.glob("documents/*.docx"))
        document = zipfile.ZipFile(docx).read("word/document.xml").decode("utf-8")
        self.assertIn("<w:tblHeader/>", document)
        self.assertIn("legacy order file", docx_text(docx))
        self.assertTrue(zipfile.ZipFile(docx).testzip() is None)

    def test_deck_has_titles_bullets_a_table_notes_and_a_group(self) -> None:
        pptx = next(self.dir_a.glob("deck/*.pptx"))
        try:
            from pptx import Presentation
        except ImportError:  # pragma: no cover - environment dependent
            self.skipTest("python-pptx is not installed")
        deck = Presentation(str(pptx))
        self.assertGreaterEqual(len(deck.slides), 20)
        self.assertLessEqual(len(deck.slides), 30)
        kinds = []
        for slide in deck.slides:
            for shape in slide.shapes:
                kinds.append(str(shape.shape_type))
        self.assertTrue(any("TABLE" in kind for kind in kinds), "no table on the deck")
        self.assertTrue(any("GROUP" in kind for kind in kinds), "no grouped shape on the deck")
        text = pptx_text(pptx)
        self.assertIn("[Notes]", text)
        self.assertIn("signed before the window opens", normalise(text))

    def test_plain_text_files_are_readable_text(self) -> None:
        for path in self.files(self.dir_a):
            if path.suffix in (".txt", ".json"):
                self.assertTrue(path.read_text(encoding="utf-8").strip(), str(path))

    def test_pdf_opens_with_pypdf(self) -> None:
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf is not installed in this venv (the worker's fallback extractor)")
        pdf = next(self.dir_a.glob("documents/*.pdf"))
        reader = PdfReader(str(pdf))
        self.assertGreaterEqual(len(reader.pages), gen.MIN_PDF_PAGES)
        self.assertIn("cutover", reader.pages[0].extract_text().lower())

    def test_docx_opens_with_python_docx(self) -> None:
        try:
            import docx  # python-docx
        except ImportError:
            self.skipTest("python-docx is not installed in this venv (the worker's preferred extractor)")
        document = docx.Document(str(next(self.dir_a.glob("documents/*.docx"))))
        self.assertTrue(document.paragraphs)
        self.assertTrue(document.tables, "python-docx sees no table")

    def test_pptx_opens_with_python_pptx(self) -> None:
        try:
            from pptx import Presentation
        except ImportError:
            self.skipTest("python-pptx is not installed")
        slides = list(Presentation(str(next(self.dir_a.glob("deck/*.pptx")))).slides)
        self.assertGreaterEqual(len(slides), 20)

    def test_every_transcript_carries_a_planted_fact(self) -> None:
        """A meeting with no register entry in it is useless for a labelled probe."""
        facts_by_document: dict[str, list[str]] = {}
        for fact in self.manifest_a["facts"]:
            facts_by_document.setdefault(fact["document"], []).append(fact["text"])
        transcripts = sorted(self.dir_a.glob(f"{gen.TRANSCRIPT_DIR_REL}/*.txt"))
        shared_topics = set()
        for path in transcripts:
            text = normalise(path.read_text(encoding="utf-8"))
            held = [fact for fact in facts_by_document.get(str(path.relative_to(self.dir_a)), [])]
            self.assertTrue(held, f"{path.name} holds no planted fact")
            self.assertTrue(any(normalise(fact) in text for fact in held), path.name)
            for entry in self.manifest_a["files"]:
                if entry["path"] == str(path.relative_to(self.dir_a)):
                    shared_topics.update(entry["topics"])
        self.assertGreaterEqual(len(shared_topics), gen.MIN_SHARED_TOPICS)

    def test_the_manifest_lists_the_people_who_speak(self) -> None:
        people = {entry["name"] for entry in self.manifest_a["people"]}
        self.assertTrue(people)
        for path in self.dir_a.glob(f"{gen.TRANSCRIPT_DIR_REL}/*.txt"):
            turns = set(re.findall(r"(?m)^([A-Z]\. [A-Za-z'\-]+):", path.read_text(encoding="utf-8")))
            self.assertTrue(turns, str(path))
            for speaker in turns:
                self.assertIn(speaker, people, str(path))

    @unittest.skipUnless(shutil.which("pdftotext"), "poppler's pdftotext is not installed")
    def test_pdf_extracts_with_the_backend_ingestion_prefers(self) -> None:
        """poppler is what app/pdf_extract.py uses when it is on PATH."""
        pdf = next(self.dir_a.glob("documents/*.pdf"))
        done = subprocess.run(
            ["pdftotext", "-layout", str(pdf), "-"], capture_output=True, text=True
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        text = normalise(done.stdout)
        self.assertGreaterEqual(len(text), 20000)
        self.assertGreaterEqual(text.count("uncontrolled when printed"), gen.MIN_PDF_PAGES)
        for fact in self.manifest_a["facts"]:
            path = self.dir_a / fact["document"]
            if path.suffix != ".pdf":
                continue
            self.assertIn(normalise(fact["text"]), text, fact["fact_id"])


class GeneratorGuards(unittest.TestCase):
    def test_refuses_to_write_inside_the_repo(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            gen.main(["--out", str(ROOT_DIR / "synthetic-co"), "--quiet"])
        self.assertIn("inside the repo", str(caught.exception))
        self.assertFalse((ROOT_DIR / "synthetic-co").exists())

    def test_default_output_is_outside_the_repo(self) -> None:
        default = Path(gen.DEFAULT_OUT).resolve()
        self.assertNotIn(ROOT_DIR, default.parents)

    def test_every_fact_sentence_fits_one_template_bound(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _, manifest = gen.generate_company(COMPANY_A, SEED, Path(tmp))
        for fact in manifest["facts"]:
            self.assertLessEqual(len(fact["text"]), gen.MAX_FACT_CHARS, fact["fact_id"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
