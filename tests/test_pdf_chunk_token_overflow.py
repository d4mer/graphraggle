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

The fix is a design decision (change a limit vs. split pieces vs. count in
LightRAG's tokenizer family) and is reported to the lead rather than chosen here.
Nothing in ``app/`` changed for fix 3: the byte-exact transcript and DOCX results
are untouched by construction. ``test_the_gap_is_still_open`` documents the defect
as a characterisation test - when the lead lands a fix, that test must be inverted
to assert no submitted segment can exceed the limit.
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


class TestTheGapIsStillOpen(_SubmitHarness):
    """CHARACTERISATION of the unfixed defect - see the module docstring.

    This test asserts what the code does TODAY, so that a fix cannot land silently
    and so that the lead's decision is anchored to a measured case. When a fix
    lands, invert the last assertion to: no segment's true count may exceed the
    limit the request asks LightRAG to enforce.
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
            "...and yet LightRAG must count this same segment at >= that number, which "
            "is the 505 > 481 shape. Left unfixed on purpose: the remedy is a design "
            "decision reported to the lead.")


if __name__ == "__main__":
    unittest.main()
