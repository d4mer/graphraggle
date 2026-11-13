"""PDF chunk-token overflow (GL-07/08 fix 3): diagnosis pinned as tests.

What stack2 reported (Hermes, 2026-11-12, counts only)
-----------------------------------------------------
Two synthetic integration-analysis PDFs failed with
``Chunk token length 505 exceeds chunk_token_size 481``, the same family as the
production S/4 PDF, and every ``manifest.json`` failed with
``Chunk token length 3431 exceeds chunk_token_size 3060``.

What the tests below establish, offline, with invented text only
---------------------------------------------------------------
1. ``481`` and ``3060`` are **numbers the worker computed**, not LightRAG
   settings. ``render_for_lightrag`` puts ``chunk_token_size`` in the per-document
   request, and its value is ``largest rendered piece in the worker's counter +
   STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS``. A configured LightRAG limit would be one
   number for every document; these differ per document.
2. The counter in the worker image is ``conservative-overcount``
   (``max(ceil(chars / CHARS_PER_TOKEN=3.6), whitespace_unit_count)``), because
   ``requirements.txt`` does not
   install tiktoken - and it is only an upper bound for prose-shaped text. For the
   table/number/symbol text a ``pdftotext -layout`` extraction produces, it
   UNDER-counts cl100k. Measured on the fixtures below against a lower bound on
   LightRAG's own count (published cl100k pre-tokenizer, one token per chunk at
   minimum): the table class comes out at **1.94x** under, dotted number runs at
   **3.0x** under, JSON at **1.04x** under - while prose is 0.62x, i.e. safely over.
   That is how a segment ends up larger than the limit the worker itself asked
   LightRAG to enforce.
3. ``STRUCTURE_CHUNKING_MAX_PIECE_TOKENS`` (7500) is not the cap for this failure.
   It is the BGE-M3 embedder-window budget, a different number checked on a
   different question. Nothing in the path bounds the piece against LightRAG's own
   tokenizer.

STATUS (2026-11-13): the operator chose option B - a relative safety factor on the
limit we ask LightRAG to enforce (``STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY``, shipped at
3.2 = the largest measured under-count plus headroom, capped by
``STRUCTURE_CHUNKING_CHUNK_TOKEN_CEILING`` at the embedder window). Option A - counting in
LightRAG's own tokenizer family, i.e. installing tiktoken or vendoring cl100k - remains the
proper fix, because every ratio here is a LOWER bound: a factor of 3.2 bounds the cases we
measured, not every case there is.

So this file now holds both halves. ``TestTheGapUnderThePreFactorConfig`` keeps the defect
alive as a characterisation test at SAFETY=1.0 (the configuration stack2 ran with), so the
remedy cannot be silently removed. ``TestTheGapIsClosedByTheSafetyFactor`` is the inversion:
with the shipped factor, no submitted segment's lower-bound count exceeds the limit we ask
for, on the same adversarial fixtures - and the payload BYTES, hence every piece boundary,
are identical with and without the factor.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.structure_chunker as sc
import app.worker as worker
from app.config import Settings

REQUIREMENTS = Path(__file__).resolve().parent.parent / "requirements.txt"


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── a lower bound on LightRAG's own token count ─────────────────────────────
# cl100k_base pre-tokenizes before BPE, and every pre-token chunk yields at least
# one token. So counting the pre-token chunks is a strict LOWER bound on what
# LightRAG's encoder counts for the same string - no vocabulary needed, which is
# what makes this provable offline in a venv with no tiktoken.
#
# The pattern is the published cl100k one, restricted to ASCII (every fixture here
# is ASCII, where \p{L} == [A-Za-z] and \p{N} == [0-9]) with two deliberate
# relaxations that can only merge chunks and therefore only LOWER the count: digit
# runs are kept whole instead of split into groups of 1-3, and the optional single
# leading symbol is kept as-is. A number above the worker's own count is therefore
# a proof, not an estimate.
_PRETOK = re.compile(
    r"(?i:'s|'t|'re|'ve|'m|'ll|'d)"
    r"|[^\r\n]?[A-Za-z]+"
    r"|\d+"
    r"| ?[^A-Za-z0-9\s]+[\r\n]*"
    r"|\s*[\r\n]+"
    r"|\s+(?!\S)"
    r"|\s+"
)


def lightrag_token_lower_bound(text: str) -> int:
    return len(_PRETOK.findall(text))


# Invented text only. The table mimics what `pdftotext -layout` hands the chunker
# for the GL-07 synthetic PDFs: part codes, slash dates, money, percentages.
TABLE_TEXT = (
    "Part NC-3617 | 03/11/2026 | 12.5% | $1,240.00 | 48 units | line 7.3.1\n"
    "Part CP-2507 | 14/07/2026 |  9.1% | $8,900.50 | 12 units | line 7.3.2\n"
    "Part NC-3618 | 22/09/2026 | 31.0% | $  310.75 | 90 units | line 7.4.1\n"
    "Part CP-2508 | 05/01/2026 |  2.4% | $2,050.00 |  6 units | line 7.4.2\n"
) * 3

PROSE_TEXT = (
    "The expansion will add two new reactor lines at the Northwind site during the "
    "second quarter, subject to the permit that the county is still reviewing, and "
    "the programme office expects the first commissioning window to open in the "
    "autumn once the commissioning engineer signs the safety dossier.\n"
) * 6

# The two classes the safety factor was sized on, beyond the table and the manifest.
DOTTED_NUMBERS = "1.2.3.4.5.6.7.8.9.10.11.12.13.14.15 " * 20
CODE_MIXED = "if (a && b) { x = f(y); } // 42\n" * 30

# Invented transcript bodies, the shape whose byte-exact ingest results the earlier
# packets pinned. Eight of them, because that is how the GL-07 corpus is shaped, and the
# invariant must hold per document, not only on one sample.
_TRANSCRIPT_TOPICS = (
    ("order-to-cash handling", "the window ran 5 hours 55 minutes with 32 blockers"),
    ("flat-file interfaces", "the extract failed twice on the 14th and was rerun at 02:10"),
    ("master data cleansing", "1,240 customer masters were held for review by the plant team"),
    ("legacy decommissioning", "the parallel run closed with 3 open defects, none blocking"),
    ("role and authorisation redesign", "18 roles were merged and 4 were retired outright"),
    ("archive and retention", "the retention rule keeps 7 years of records at 12.5% cost"),
    ("three-way match exceptions", "90 invoices were released manually under $2,050.00 each"),
    ("cutover rehearsal", "the go/no-go call took 45 minutes and listed 6 exit criteria"),
)


def transcript_text(index: int) -> str:
    topic, outcome = _TRANSCRIPT_TOPICS[index]
    code = f"NC-3617-T{index + 1:02d}-01"
    return (
        f"Meeting {index + 1}: {topic}\n"
        f"A. Speaker: we have the entry in front of us, Entry {code}: {topic} "
        f"was reviewed on {index + 3:02d}/0{(index % 9) + 1}/2026.\n"
        f"B. Speaker: the outcome line reads {outcome}.\n"
        f"A. Speaker: agreed, and the follow-up is scheduled with 3 owners and 2 dates.\n"
    ) * 3


TRANSCRIPT_SET = [transcript_text(i) for i in range(8)]
TRANSCRIPT_TEXT = TRANSCRIPT_SET[0]

# A DOCX-shaped specification body: dense identifiers and a table, the shape
# OrderBook.docx stands for in the earlier packets.
DOCX_LIKE_TEXT = (
    "Interface specification - order book (invented)\n\n"
    "1. Scope\nThis document specifies the order book interface between the legacy system and "
    "the new ERP. Record NC-3617-OB-001 carries 12 fields.\n\n"
    "2. Field map\n"
    "FIELD | TYPE | WIDTH | NOTE\n"
    "ORDER_ID | CHAR | 10 | key NC-3617-OB-###\n"
    "QUANTITY | DEC | 8 | 3 decimals, 1,240.000 max\n"
    "UNIT_PRICE | CUR | 11 | $2,050.00 typical\n"
    "DELIVERY | DATE | 8 | 03/11/2026 format\n" * 4
)

# sha256 of the submitted (delimiter-joined) text for each fixture in TRANSCRIPT_SET +
# DOCX_LIKE_TEXT, captured when option B landed. Byte-exactness, not a count.
SUBMITTED_BYTES_SHA256: list[str] = [
    "5ed88e7ff66ec3aab3eec6d04887231c4f531fe4411721f77ba8a7a447bab464",  # transcript 1
    "5d24efb393c7f3e202f4a2edcc7d0476f9ff87427a8e6fed99b325334cc94fee",  # transcript 2
    "def4c77cfe7afdd86ffc06268932cb1dbc5ca050325b26ca4489c778101b90a1",  # transcript 3
    "05c785dce406224ef40816a0407d465dcc723e2944404880b380ef61d20d4f16",  # transcript 4
    "b29aa04541c822b6fea0a83a0e71aee0471c91746cf0ddb33d86588c9e3f949e",  # transcript 5
    "838921fe85b6c331fdac3faab337ec7e076d13c5e2484fff3f02e2e13fbe2220",  # transcript 6
    "ce2f78323ccaf85219a0ac7bb2dfe0d7d31a40b510f2788bc1ea65559fe5a5c7",  # transcript 7
    "4f429416d458293ff2648414cfd95dead6b84344c7632e06f92fa9c97281d32e",  # transcript 8
    "e0964929e06fcfc8d5b3e55fa32a1ba64fd23c8cc5acf00dff30da5267bdf571",  # docx-shaped spec
]

MANIFEST_LIKE_TEXT = (
    '{"company":"Northwind Chemicals","facts":[{"id":"nc-f1","doc":"docx",'
    '"sentence":"The batch record NC-3617 was signed on 03/11/2026 by the plant '
    "manager at 14:05.\\\"},{\"id\":\"nc-f2\",\"doc\":\"pdf\",\"value\":1240.50}]}\n"
) * 4


class TestEstimateDirection(unittest.TestCase):
    """The instrument, not the document: which way does it err, and by how much."""

    def test_prose_is_over_counted_in_the_safe_direction(self) -> None:
        ours = sc.token_count(PROSE_TEXT)
        bound = lightrag_token_lower_bound(PROSE_TEXT)
        self.assertGreater(ours, bound,
                           "prose must stay over-counted: this is the case the "
                           "chunker's docstring promise is based on")

    def test_pdf_table_text_is_under_counted(self) -> None:
        ours = sc.token_count(TABLE_TEXT)
        bound = lightrag_token_lower_bound(TABLE_TEXT)
        self.assertLess(ours, bound,
                        "the table class is where the promise breaks: LightRAG's "
                        f"count is at least {bound} for a piece we priced at {ours}")
        self.assertGreaterEqual(bound, int(ours * 1.5),
                                "the gap is relative, not a constant to absorb in "
                                "STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS")

    def test_dotted_numbers_are_the_worst_case(self) -> None:
        text = "1.2.3.4.5.6.7.8.9.10.11.12.13.14.15 " * 20
        ours = sc.token_count(text)
        bound = lightrag_token_lower_bound(text)
        self.assertLess(ours, bound)

    def test_code_and_symbol_text_is_under_counted_too(self) -> None:
        ours = sc.token_count(CODE_MIXED)
        bound = lightrag_token_lower_bound(CODE_MIXED)
        self.assertLess(ours, bound,
                        f"we price this at {ours}; LightRAG counts it at >= {bound}")

    def test_the_shipped_factor_covers_every_measured_ratio_in_this_file(self) -> None:
        """The default is tied to these measurements, not to a round number.

        If a fixture's ratio rises above the shipped factor, or someone lowers the factor,
        this fails here instead of in an ingest run.
        """
        ratios = {}
        for name, text in (("prose", PROSE_TEXT), ("pdf_table", TABLE_TEXT),
                           ("json_manifest", MANIFEST_LIKE_TEXT),
                           ("dotted_numbers", DOTTED_NUMBERS), ("code_mixed", CODE_MIXED)):
            ours = sc.token_count(text)
            ratios[name] = lightrag_token_lower_bound(text) / max(1, ours)
        shipped = Settings().structure_chunking_chunk_token_safety
        self.assertGreaterEqual(shipped, max(ratios.values()),
                                f"measured under-counts {ratios} exceed the factor {shipped}")
        self.assertLess(shipped, 5.0,
                        "a factor this large would out-argue its own evidence; see the "
                        "STRUCTURE_CHUNKING_CHUNK_TOKEN_CEILING for why it is capped")

    def test_json_is_not_reliably_over_counted_either(self) -> None:
        """The manifest.json shape: dense punctuation, so neither rule bounds it.

        ``_overcount_tokens`` is ``max(ceil(chars / 3.6), whitespace_unit_count)``.
        JSON has few words, and its punctuation costs a BPE more than one token per
        3.6 characters, so both halves of the max fall short - the same direction
        the real manifest failed in (3431 counted against the 3060 we asked for).
        """
        ours = sc.token_count(MANIFEST_LIKE_TEXT)
        bound = lightrag_token_lower_bound(MANIFEST_LIKE_TEXT)
        by_chars = math.ceil(len(MANIFEST_LIKE_TEXT) / sc.CHARS_PER_TOKEN)
        self.assertEqual(ours, by_chars,
                         "the char half of the max is what carries JSON: the word "
                         "half is far below it")
        self.assertLess(ours, bound,
                        f"we price this JSON at {ours}; LightRAG must count it at "
                        f"{bound} or more")


class TestInstrumentProvenance(unittest.TestCase):
    def test_requirements_do_not_install_a_cl100k_counter(self) -> None:
        self.assertTrue(REQUIREMENTS.exists())
        names = {line.split("#", 1)[0].strip().split("==")[0].split("[")[0]
                 for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()}
        self.assertNotIn("tiktoken", names,
                         "if tiktoken joined requirements.txt the worker would count "
                         "in LightRAG's family: update these tests with it")

    def test_the_basis_of_every_size_is_recorded_not_guessed(self) -> None:
        self.assertIn(sc.tokenizer_name(),
                      ("tiktoken:cl100k_base", "conservative-overcount"))


class _SubmitHarness(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="grg-overflow-"))
        (self.root / "source_docs").mkdir()
        self.store = _FakeStore()
        self.post_json = AsyncMock(return_value={"track_id": "trk-1"})
        patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.root / "source_docs"),
            uploads_dir=str(self.root / "uploads"),
            transcript_prep_enabled=False,
            structure_chunking_enabled=True,
            structure_chunking_max_piece_tokens=7500,
            structure_chunking_min_piece_tokens=150,
            structure_chunking_header_tokens=25,
            structure_chunking_headers=False,
            structure_chunking_fit_margin_tokens=64,
            structure_chunking_embed_window_tokens=8192,
            structure_chunking_embed_tokenizer_path="",
            worker_pdf_extract_enabled=True,
            # The PRE-FACTOR configuration. Everything in this class characterises the stack2
            # defect, so it must run with the sizing that produced it; the factor's own
            # behaviour is asserted in TestTheGapIsClosedByTheSafetyFactor.
            structure_chunking_chunk_token_safety=1.0,
            structure_chunking_chunk_token_ceiling=0,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def submit_text_file(self, name: str, content: str) -> dict:
        """Text route: same chunking chain the extracted-PDF route feeds."""
        path = self.root / "source_docs" / name
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest,
                                      "status": "accepted", "company": None,
                                      "filename": name, "original_filename": name,
                                      "warnings_json": None}
        with self._patches(), patch.object(worker.client, "post_json", self.post_json):
            _run_async(worker.submit_file(path, "filesystem", digest))
        return {"path": str(path), "body": self.post_json.await_args.args[1],
                "meta": self.store.meta(str(path))}

    def submit_pdf_file(self, name: str, extracted_text: str) -> dict:
        """The PDF route with the parser replaced by invented pdftotext output.

        pypdf and poppler are not both guaranteed in this venv, and the parser is
        not what this test is about: the seam is the text handed to the chunker.
        """
        path = self.root / "source_docs" / name
        path.write_bytes(b"%PDF-1.4 invented bytes\n%%EOF\n")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest,
                                      "status": "accepted", "company": None,
                                      "filename": name, "original_filename": name,
                                      "warnings_json": None}
        with self._patches(), \
             patch.object(worker, "extract_binary_payload_with_backend",
                          lambda _path: (worker.normalize_text_submission_payload(extracted_text),
                                         "pdftotext")), \
             patch.object(worker.client, "post_json", self.post_json):
            _run_async(worker.submit_file(path, "filesystem", digest))
        return {"path": str(path), "body": self.post_json.await_args.args[1],
                "meta": self.store.meta(str(path))}

    def _patches(self):
        return patch.multiple(
            worker,
            get_document_by_path=self.store.get_document_by_path,
            get_documents_by_sha256=self.store.get_documents_by_sha256,
            update_document_state=self.store.update_document_state,
            upsert_document=self.store.upsert_document,
            increment_retry=self.store.increment_retry,
        )


class _FakeStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def get_document_by_path(self, path: str):
        doc = self.docs.get(path)
        return dict(doc) if doc else None

    async def get_documents_by_sha256(self, sha256: str) -> list[dict]:
        return [dict(d) for d in self.docs.values() if d.get("sha256") == sha256]

    async def update_document_state(self, path: str, **kwargs) -> None:
        self.docs.setdefault(path, {"path": path}).update(kwargs)

    async def upsert_document(self, **kwargs) -> None:
        self.docs.setdefault(kwargs["path"], {}).update(kwargs)

    async def increment_retry(self, path: str, **kwargs) -> int:
        return 1

    def meta(self, path: str) -> dict:
        meta = worker.parse_warning_metadata(self.docs[path].get("warnings_json")).get("worker")
        return meta if isinstance(meta, dict) else {}


class TestRouteAndLimitProvenance(_SubmitHarness):
    def test_the_pdf_text_route_needs_both_flags(self) -> None:
        pdf = self.root / "source_docs" / "integration-analysis.pdf"
        with patch.object(worker.settings, "structure_chunking_enabled", False):
            self.assertFalse(worker.uses_binary_extract(pdf))
        with patch.object(worker.settings, "worker_pdf_extract_enabled", False):
            self.assertFalse(worker.uses_binary_extract(pdf))
        self.assertTrue(worker.uses_binary_extract(pdf))

    def test_the_limit_in_the_error_message_is_the_workers_own_number(self) -> None:
        got = self.submit_pdf_file("integration-analysis.pdf", TABLE_TEXT)
        params = got["body"]["chunking"]["params"]
        sent = params["chunk_token_size"]
        segments = [s for s in got["body"]["text"].split(params["split_by_character"]) if s.strip()]
        largest_ours = max(sc.token_count(segment) for segment in segments)

        self.assertEqual(
            sent, largest_ours + int(worker.settings.structure_chunking_fit_margin_tokens),
            "chunk_token_size is our largest-piece count plus the margin - which is why "
            "the two stack2 errors carried two different limits (481 and 3060) instead "
            "of one configured number")
        self.assertEqual(got["meta"].get("structure_chunking_chunk_token_basis"),
                         sc.tokenizer_name(),
                         "the counter that produced that number is recorded on the row")
        self.assertEqual(got["meta"].get("structure_chunking_chunk_token_safety"), 1.0,
                         "and so is the factor that was applied to it")

    def test_the_shipped_default_scales_the_limit_by_the_measured_worst_case(self) -> None:
        """At shipped defaults the limit is ceil(largest * SAFETY) + margin, not +margin."""
        shipped = Settings().structure_chunking_chunk_token_safety
        self.assertGreater(shipped, 1.0, "option B must not ship disabled")
        with patch.multiple(worker.settings,
                            structure_chunking_chunk_token_safety=shipped,
                            structure_chunking_chunk_token_ceiling=int(
                                Settings().structure_chunking_chunk_token_ceiling)):
            got = self.submit_pdf_file("integration-analysis.pdf", TABLE_TEXT)
        params = got["body"]["chunking"]["params"]
        segments = [s for s in got["body"]["text"].split(params["split_by_character"]) if s.strip()]
        largest_ours = max(sc.token_count(segment) for segment in segments)
        self.assertEqual(
            params["chunk_token_size"],
            math.ceil(largest_ours * shipped) + int(
                worker.settings.structure_chunking_fit_margin_tokens),
            "ceil(largest * SAFETY) + FIT_MARGIN: the formula, in words")
        worst_ratio = max(lightrag_token_lower_bound(s) / max(1, sc.token_count(s))
                          for s in segments)
        self.assertLessEqual(worst_ratio, shipped,
                             "the shipped factor must cover the fixture class it was sized on")
        self.assertEqual(got["meta"].get("structure_extract_backend"), "pdftotext")

    def test_max_piece_tokens_is_a_different_budget(self) -> None:
        """7500 is the embedder window minus margin; it never becomes LightRAG's limit."""
        got = self.submit_pdf_file("integration-analysis.pdf", TABLE_TEXT)
        sent = got["body"]["chunking"]["params"]["chunk_token_size"]
        self.assertLess(sent, int(worker.settings.structure_chunking_max_piece_tokens),
                        "a small document must not inherit the window as its limit")
        self.assertEqual(got["meta"].get("structure_chunking_max_piece_tokens"),
                         int(worker.settings.structure_chunking_max_piece_tokens))
        self.assertNotEqual(sent, got["meta"].get("structure_chunking_max_piece_tokens"))

    def test_the_fit_margin_is_a_constant_and_the_gap_is_not(self) -> None:
        """Raising STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS cannot bound this class."""
        table_bound = lightrag_token_lower_bound(TABLE_TEXT)
        prose_bound = lightrag_token_lower_bound(PROSE_TEXT)
        gaps = [table_bound - sc.token_count(TABLE_TEXT),
                prose_bound - sc.token_count(PROSE_TEXT)]
        self.assertGreater(gaps[0], 0)
        self.assertLess(gaps[1], 0)
        self.assertGreater(gaps[0], worker.settings.structure_chunking_fit_margin_tokens,
                           "the shortfall already exceeds the shipped margin, and it "
                           "scales with the piece, so a bigger constant is not a bound")


class TestTheGapUnderThePreFactorConfig(_SubmitHarness):
    """CHARACTERISATION of the defect at SAFETY=1.0 - the config stack2 ran with.

    Kept on purpose: if the factor is ever set back to 1.0, this is the shape that comes
    back, and it must not come back unnoticed.
    """

    def test_adversarial_pdf_like_piece_exceeds_the_limit_we_ask_for(self) -> None:
        got = self.submit_pdf_file("integration-analysis.pdf", TABLE_TEXT)
        params = got["body"]["chunking"]["params"]
        sent = params["chunk_token_size"]
        segments = [s for s in got["body"]["text"].split(params["split_by_character"]) if s.strip()]
        worst = max(lightrag_token_lower_bound(segment) for segment in segments)

        self.assertGreater(len(segments), 0)
        self.assertLess(got["meta"]["structure_chunking_tokens_max"],
                        int(worker.settings.structure_chunking_max_piece_tokens),
                        "the piece cleared the chunker's own ceiling...")
        self.assertGreater(
            worst, sent,
            "...and yet LightRAG must count this same segment at >= that number, which is "
            "the 505 > 481 shape: a constant margin cannot bound a relative error.")


class TestTheGapIsClosedByTheSafetyFactor(_SubmitHarness):
    """THE INVERSION: with the shipped factor, the limit clears the measured classes.

    ``lightrag_token_lower_bound`` is a LOWER bound on LightRAG's count, so clearing it is
    evidence, not proof - which is exactly why option A stays on the list.
    """

    def setUp(self) -> None:
        super().setUp()
        self.shipped = Settings().structure_chunking_chunk_token_safety
        self.ceiling = Settings().structure_chunking_chunk_token_ceiling
        patcher = patch.multiple(
            worker.settings,
            structure_chunking_chunk_token_safety=self.shipped,
            structure_chunking_chunk_token_ceiling=self.ceiling)
        patcher.start()
        self.addCleanup(patcher.stop)

    def submitted(self, text: str) -> tuple[int, list[str], dict]:
        got = self.submit_pdf_file("integration-analysis.pdf", text)
        params = got["body"]["chunking"]["params"]
        segments = [s for s in got["body"]["text"].split(params["split_by_character"]) if s.strip()]
        return int(params["chunk_token_size"]), segments, got["meta"]

    def test_the_default_factor_covers_every_measured_under_counting_class(self) -> None:
        for name, text in (("pdf_table", TABLE_TEXT), ("prose", PROSE_TEXT),
                           ("json_manifest", MANIFEST_LIKE_TEXT), ("dotted_numbers", DOTTED_NUMBERS)):
            with self.subTest(shape=name):
                sent, segments, _meta = self.submitted(text)
                worst = max(lightrag_token_lower_bound(s) for s in segments)
                self.assertLessEqual(
                    worst, sent,
                    f"{name}: LightRAG counts a segment at >= {worst} but we asked it to "
                    f"enforce {sent}")

    def test_the_factor_is_what_closes_it_and_not_a_bigger_margin(self) -> None:
        """Same fixture, factor off: the exact failure the factor pays for."""
        with patch.multiple(worker.settings, structure_chunking_chunk_token_safety=1.0,
                            structure_chunking_chunk_token_ceiling=0):
            off_sent, off_segments, _meta = self.submitted(TABLE_TEXT)
        off_worst = max(lightrag_token_lower_bound(s) for s in off_segments)
        on_sent, on_segments, _meta = self.submitted(TABLE_TEXT)
        on_worst = max(lightrag_token_lower_bound(s) for s in on_segments)
        self.assertGreater(off_worst, off_sent)
        self.assertLessEqual(on_worst, on_sent)
        self.assertGreater(on_sent, off_sent)

    def test_eight_transcripts_and_a_docx_body_keep_their_exact_bytes(self) -> None:
        """The byte-exact ingest results from the earlier packets survive option B.

        Pinned as sha256 of the submitted text per document, so a later change that moves a
        boundary anywhere in this set fails here rather than in someone's ingest run. The
        table was produced at the factor's introduction and must only move with a deliberate
        chunking change.
        """
        for index, text in enumerate(TRANSCRIPT_SET + [DOCX_LIKE_TEXT]):
            with self.subTest(document=index):
                sent, segments, _meta = self.submitted(text)
                joined = self.post_json.await_args.args[1]["text"]
                self.assertEqual(
                    hashlib.sha256(joined.encode("utf-8")).hexdigest(),
                    SUBMITTED_BYTES_SHA256[index],
                    f"document {index} no longer submits the bytes it used to")
                self.assertTrue(segments)
                self.assertGreater(sent, 64)

    def test_the_factor_changes_no_byte_of_the_payload_and_no_piece_boundary(self) -> None:
        """The whole point of option B: only the acceptance threshold moves.

        Asserted on the submitted BYTES (digest), not on counts, for the document classes
        whose byte-exact ingest results the earlier packets pinned.
        """
        for name, text in (("transcript", TRANSCRIPT_TEXT), ("pdf_table", TABLE_TEXT),
                           ("docx_spec", DOCX_LIKE_TEXT)):
            with self.subTest(shape=name):
                with patch.object(worker.settings, "structure_chunking_chunk_token_safety", 1.0):
                    off_sent, off_segments, _ = self.submitted(text)
                    off_text = self.post_json.await_args.args[1]["text"]
                on_sent, on_segments, _ = self.submitted(text)
                on_text = self.post_json.await_args.args[1]["text"]
                self.assertEqual(off_text, on_text, f"{name}: the submitted bytes moved")
                self.assertEqual(len(off_segments), len(on_segments))
                self.assertEqual([len(s) for s in off_segments], [len(s) for s in on_segments])
                self.assertGreaterEqual(on_sent, off_sent)

    def test_a_code_shaped_fixture_never_reaches_the_limit_at_all(self) -> None:
        """Why CODE_MIXED is not in the submitted loop: it gets no chunk plan.

        The chunker returns no pieces for text with no prose units, so the worker falls back
        to the unchunked route. That class's 1.55x under-count is measured at the counter
        level (TestEstimateDirection) and is not something this limit governs.
        """
        got = self.submit_pdf_file("interface-code.pdf", CODE_MIXED)
        self.assertNotIn("chunking", got["body"])
        self.assertIn("no pieces", got["meta"]["structure_chunking_reason"])

    def test_the_ceiling_bounds_the_request_and_says_so_on_the_row(self) -> None:
        """A piece must not inherit an absurd limit from a 3.2x factor."""
        with patch.object(worker.settings, "structure_chunking_chunk_token_ceiling", 700):
            sent, _segments, meta = self.submitted(TABLE_TEXT)
        self.assertEqual(sent, 700)
        self.assertTrue(meta["structure_chunking_chunk_token_capped"])
        self.assertGreater(meta["structure_chunking_chunk_token_uncapped"], 700)
        self.assertEqual(meta["structure_chunking_chunk_token_safety"], self.shipped)

    def test_the_ceiling_never_pulls_the_limit_below_the_pre_factor_number(self) -> None:
        """A misconfigured ceiling cannot create a rejection that did not already exist."""
        with patch.multiple(worker.settings, structure_chunking_chunk_token_safety=3.2,
                            structure_chunking_chunk_token_ceiling=10):
            sent, segments, meta = self.submitted(TABLE_TEXT)
        largest_ours = max(sc.token_count(s) for s in segments)
        self.assertGreaterEqual(sent, largest_ours + int(
            worker.settings.structure_chunking_fit_margin_tokens))
        self.assertTrue(meta["structure_chunking_chunk_token_capped"])

    def test_a_safety_below_one_degrades_to_the_pre_factor_behaviour(self) -> None:
        with patch.multiple(worker.settings, structure_chunking_chunk_token_safety=0.2,
                            structure_chunking_chunk_token_ceiling=0):
            sent, segments, meta = self.submitted(TABLE_TEXT)
        largest_ours = max(sc.token_count(s) for s in segments)
        self.assertEqual(sent, largest_ours + int(
            worker.settings.structure_chunking_fit_margin_tokens))
        self.assertEqual(meta["structure_chunking_chunk_token_safety"], 1.0,
                         "the applied factor is recorded after clamping, not the typed one")


if __name__ == "__main__":
    unittest.main()
