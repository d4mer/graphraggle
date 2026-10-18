"""Document extraction for the structure-aware ingest route: PDF, DOCX, PPTX.

Synthetic documents only. The PDF and DOCX fixtures are REAL files built from bytes (a
hand-written PDF, a hand-zipped WordprocessingML package), so the parsers are exercised on
the formats rather than on mocks. Where a parser is genuinely optional the test says which
backend it ran, and the poppler test skips with a reason if poppler is absent.
"""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path


def _squash(text: str) -> str:
    return " ".join(text.split())


def make_pdf(pages: list[list[tuple[int, int, str]]], path: Path) -> Path:
    """Write a minimal valid PDF. Each page is a list of (x, y, text) positioned runs.

    The positions are the point: a PDF table is positioned text, and the wrapped-row
    reassembly reads where a line starts horizontally.
    """
    def escape(text: str) -> str:
        return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    count = len(pages)
    kids = " ".join(f"{3 + index} 0 R" for index in range(count))
    font_id = 3 + 2 * count
    objects = ["<< /Type /Catalog /Pages 2 0 R >>",
               f"<< /Type /Pages /Kids [{kids}] /Count {count} >>"]
    for index in range(count):
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                       f"/Contents {3 + count + index} 0 R "
                       f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
    for runs in pages:
        lines = ["BT", "/F1 10 Tf"]
        for x, y, text in runs:
            lines.append(f"1 0 0 1 {x} {y} Tm ({escape(text)}) Tj")
        lines.append("ET")
        stream = "\n".join(lines)
        objects.append(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\n"
                       f"endstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = [b"%PDF-1.4\n"]
    position = len(out[0])
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(position)
        chunk = f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
        out.append(chunk)
        position += len(chunk)
    table = [f"xref\n0 {len(objects) + 1}\n", "0000000000 65535 f \n"]
    table += [f"{offset:010d} 00000 n \n" for offset in offsets]
    out.append("".join(table).encode("latin-1"))
    out.append(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{position}\n"
               f"%%EOF\n".encode("latin-1"))
    path.write_bytes(b"".join(out))
    return path


def make_docx(paragraphs: list[tuple[str, str]], tables: list[list[list[str]]],
              order: list[str], path: Path) -> Path:
    """Write a minimal .docx (a ZIP of WordprocessingML).

    ``order`` interleaves "p" and "t": document order is what the extractor has to preserve,
    because a report's table sits between the sentences that introduce it.
    """
    body: list[str] = []
    paragraph_index = 0
    table_index = 0
    for kind in order:
        if kind == "p":
            style, text = paragraphs[paragraph_index]
            paragraph_index += 1
            style_xml = (f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else "")
            body.append(f"<w:p>{style_xml}<w:r><w:t xml:space=\"preserve\">{text}</w:t></w:r>"
                        f"</w:p>")
        else:
            rows = tables[table_index]
            table_index += 1
            rendered = []
            for row in rows:
                cells = "".join(
                    '<w:tc><w:p><w:r><w:t xml:space="preserve">%s</w:t></w:r></w:p></w:tc>'
                    % cell for cell in row)
                rendered.append(f"<w:tr>{cells}</w:tr>")
            body.append("<w:tbl>" + "".join(rendered) + "</w:tbl>")
    document = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/'
                '2006/main"><w:body>' + "".join(body) + "</w:body></w:document>")
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.'
            'openxmlformats.org/package/2006/content-types"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/><Override '
            'PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-'
            'officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
            'Target="word/document.xml" Type="http://schemas.openxmlformats.org/'
            'officeDocument/2006/relationships/document"/></Relationships>')
        archive.writestr("word/document.xml", document)
    return path


class PdfRowReassembly(unittest.TestCase):
    """The PDF route must not cut a logical row, even one spread over 3-8 lines."""

    def test_wrapped_cells_become_one_line_per_logical_row(self):
        from app.pdf_extract import join_wrapped_rows
        block = "\n".join([
            "Region          Q1        Q2",
            "north           12        15",
            "                grew      more",
            "                across the year",
            "south           7         9",
        ])
        rows = [line for line in join_wrapped_rows(block).splitlines() if line.strip()]
        self.assertEqual(len(rows), 3, "three logical rows, not five physical lines")
        self.assertTrue(_squash(rows[0]).startswith("Region Q1 Q2"))
        self.assertIn("across the year", rows[1])
        self.assertTrue(rows[1].lstrip().startswith("north"))
        self.assertNotIn("across the year", rows[2])

    def test_the_reassembled_row_is_still_table_shaped_for_the_chunker(self):
        from app.pdf_extract import join_wrapped_rows
        from app.structure_chunker import _is_table_line
        joined = join_wrapped_rows("Region    Q1    Q2\nnorth     12     15\n        and more")
        lines = [line for line in joined.splitlines() if line.strip()]
        self.assertTrue(all(_is_table_line(line) for line in lines),
                        "two-space joining keeps the row reading as a table row")

    def test_prose_is_left_exactly_as_it_was(self):
        from app.pdf_extract import join_wrapped_rows
        prose = ("The board met on Tuesday and discussed the acquisition at length.\n"
                 "Nobody objected, although two members asked for a written summary.")
        self.assertEqual(join_wrapped_rows(prose), prose)

    def test_a_deeply_indented_single_word_still_belongs_to_its_row(self):
        from app.pdf_extract import join_wrapped_rows
        rows = [line for line in join_wrapped_rows("contract 2026   14\n        renewal")
                .splitlines() if line.strip()]
        self.assertEqual(len(rows), 1, "an indented one-word continuation is not a new row")

    def test_rows_of_three_to_eight_lines_stay_atomic_and_under_budget(self):
        from app.pdf_extract import join_wrapped_rows
        from app.structure_chunker import chunk_document
        lines: list[str] = ["Item          Budget     Owner      Notes"]
        for index in range(1, 9):
            width = 3 + (index % 6)                    # rows spanning 3..8 physical lines
            lines.append(f"line {index:02d}      {index * 100}     alice      "
                         f"fragment {index} starts here")
            for extra in range(1, width):
                lines.append(f"                                 continuation {index}.{extra}")
        joined = join_wrapped_rows("\n".join(lines))
        rows = [line for line in joined.splitlines() if line.strip()]
        self.assertEqual(len(rows), 9, "eight logical rows plus the header, not 40 lines")
        plan = chunk_document(joined, filename="budget.txt", budget_tokens=120,
                              header_tokens=20)
        self.assertEqual(plan.kind_detected, "table")
        self.assertGreater(len(plan.pieces), 1, "the table has to split at this budget")
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 120)
            body_lines = [line for line in piece.body.splitlines() if line.strip()]
            self.assertEqual(_squash(body_lines[0]), _squash(rows[0]),
                             "every piece of a split table repeats the header row")
        for row in rows:
            self.assertTrue(any(row in piece.body for piece in plan.pieces),
                            "a logical row was cut across pieces")
        repeated_headers = len(plan.pieces) - 1      # the first piece's header is a real row
        total_rows = sum(len([line for line in piece.body.splitlines() if line.strip()])
                         for piece in plan.pieces) - repeated_headers
        self.assertEqual(total_rows, len(rows),
                         "each row appears exactly once outside the repeated headers")


class PdfExtractor(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _pdf(self, name: str, pages: list[list[tuple[int, int, str]]]) -> Path:
        return make_pdf(pages, self.root / name)

    def test_a_real_pdf_goes_through_the_installed_poppler(self):
        from app.pdf_extract import extract_pdf_text, pdftotext_available
        if not pdftotext_available():
            self.skipTest("poppler's pdftotext is not installed here")
        path = self._pdf("report.pdf", [
            [(72, 760, "Acme Corporation - quarterly report")],
            [(72, 760, "Acme Corporation - quarterly report"),
             (72, 720, "1. Revenue"),
             (72, 700, "Region        Q1         Q2"),
             (72, 686, "north         120        150"),
             (180, 672, "grew on the same headcount"),
             (72, 658, "south         70         90")],
            [(72, 760, "Acme Corporation - quarterly report"),
             (72, 720, "2. Costs"),
             (72, 700, "Costs fell in the north region during the second quarter.")],
        ])
        text = extract_pdf_text(path, backend="pdftotext")
        self.assertIn("1. Revenue", text)
        self.assertTrue(any(_squash(line) == "north 120 150 grew on the same headcount"
                            for line in text.splitlines()),
                        "the wrapped cell is one row again: " + repr(text))

    def test_the_repeated_page_header_survives_extraction_and_is_dropped_by_the_chunker(self):
        from app.pdf_extract import extract_pdf_text
        from app.structure_chunker import chunk_document
        path = self._pdf("furniture.pdf", [
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "1. Revenue grew because the pipeline was fuller than last year."),
             (300, 30, "3")],
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "2. Costs Costs fell after the vendor renegotiation finished."),
             (300, 30, "4")],
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "3. Risks Risks were unchanged from the previous quarter."),
             (300, 30, "5")],
        ])
        text = extract_pdf_text(path)
        self.assertEqual(text.count("Acme Corporation confidential"), 3,
                         "extraction does not guess at page furniture")
        plan = chunk_document(text, filename="furniture.txt", budget_tokens=200,
                              header_tokens=20)
        self.assertEqual(plan.stats.get("dropped_running_lines"), 3,
                         "the running header is dropped, and counted")
        self.assertNotIn("Acme Corporation confidential", "\n".join(p.body for p in plan.pieces))

    def test_a_scan_reports_empty_so_the_worker_falls_back_to_upload(self):
        from app.pdf_extract import PDF_EMPTY, PdfExtractError, extract_pdf_text
        with self.assertRaises(PdfExtractError) as caught:
            extract_pdf_text(make_pdf([[], []], self.root / "blank.pdf"))
        self.assertEqual(caught.exception.error_code, PDF_EMPTY)

    def test_a_non_pdf_is_a_failure_with_a_reason_not_an_empty_document(self):
        from app.pdf_extract import (PDF_EXTRACT_FAILED, PDF_LIB_MISSING, PdfExtractError,
                                     extract_pdf_text, pdftotext_available)
        path = self.root / "not-a-pdf.pdf"
        path.write_bytes(b"this is not a pdf at all")
        with self.assertRaises(PdfExtractError) as caught:
            extract_pdf_text(path)
        if pdftotext_available():
            self.assertEqual(caught.exception.error_code, PDF_EXTRACT_FAILED,
                             "poppler is installed here, so a corrupt file is a read failure")
        else:
            self.assertEqual(caught.exception.error_code, PDF_LIB_MISSING,
                             "with no PDF reader installed, that is the honest answer")

    def test_an_unknown_backend_is_refused(self):
        from app.pdf_extract import PdfExtractError, extract_pdf_text
        with self.assertRaises(PdfExtractError):
            extract_pdf_text(self.root / "missing.pdf", backend="magic")


class DocxExtractor(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _docx(self, name: str, paragraphs, tables, order) -> Path:
        return make_docx(paragraphs, tables, order, self.root / name)

    def test_a_sixty_row_table_keeps_one_row_per_line_with_the_header_first(self):
        from app.docx_extract import extract_docx_text
        rows = [["Region", "Q1", "Q2"]] + [
            [f"north {index}", str(index * 3), str(index * 5)] for index in range(1, 61)]
        path = self._docx("budget.docx",
                          [("Heading1", "Budget detail"),
                           ("", "The table below lists every region.")],
                          [rows], ["p", "p", "t"])
        lines = [line for line in extract_docx_text(path).splitlines() if line.strip()]
        self.assertEqual(lines[0], "# Budget detail", "a heading style becomes a heading")
        self.assertEqual(lines[2], "[Table]")
        self.assertEqual(lines[3], "| Region | Q1 | Q2 |", "the header row comes first")
        self.assertEqual(lines[4], "| north 1 | 3 | 5 |")
        self.assertEqual(lines[-1], "| north 60 | 180 | 300 |", "and the last row is intact")
        self.assertEqual(len(lines), 3 + 61)

    def test_every_extracted_row_is_a_table_row_to_the_chunker(self):
        """The cross-module invariant the whole route depends on."""
        from app.docx_extract import extract_docx_text
        from app.structure_chunker import _is_table_line
        rows = [["Region", "Q1"], ["north", "12"], ["south", "7"]]
        path = self._docx("shape.docx", [("", "regions")], [rows], ["p", "t"])
        lines = [line for line in extract_docx_text(path).splitlines() if "|" in line]
        self.assertEqual(len(lines), 3)
        self.assertTrue(all(_is_table_line(line) for line in lines))

    def test_a_table_stays_where_the_prose_put_it(self):
        from app.docx_extract import extract_docx_text
        text = extract_docx_text(self._docx(
            "order.docx", [("", "before the table"), ("", "after the table")],
            [[["key", "value"], ["k1", "v1"]]], ["p", "t", "p"]))
        self.assertLess(text.index("before the table"), text.index("| key | value |"))
        self.assertLess(text.index("| key | value |"), text.index("after the table"))

    def test_an_empty_document_is_reported_as_empty(self):
        from app.docx_extract import DOCX_EMPTY, DocxExtractError, extract_docx_text
        with self.assertRaises(DocxExtractError) as caught:
            extract_docx_text(self._docx("empty.docx", [("", "")], [], ["p"]))
        self.assertEqual(caught.exception.error_code, DOCX_EMPTY)

    def test_a_corrupt_archive_is_a_failure_with_a_reason(self):
        from app.docx_extract import DOCX_EXTRACT_FAILED, DocxExtractError, extract_docx_text
        path = self.root / "broken.docx"
        path.write_bytes(b"not a zip")
        with self.assertRaises(DocxExtractError) as caught:
            extract_docx_text(path)
        self.assertEqual(caught.exception.error_code, DOCX_EXTRACT_FAILED)

    def test_a_document_without_a_document_xml_part_says_so(self):
        from app.docx_extract import DOCX_EXTRACT_FAILED, DocxExtractError, extract_docx_text
        path = self.root / "wrong.zip.docx"
        with zipfile.ZipFile(str(path), "w") as archive:
            archive.writestr("hello.txt", "not a word document")
        with self.assertRaises(DocxExtractError) as caught:
            extract_docx_text(path)
        self.assertEqual(caught.exception.error_code, DOCX_EXTRACT_FAILED)

    def test_the_two_readers_agree_so_the_dependency_is_a_preference_not_a_fork(self):
        from app.docx_extract import clean_extracted_text, extract_docx_text, \
            extract_docx_with_stdlib
        rows = [["Region", "Q1"], ["north", "12"], ["south", "7"]]
        path = self._docx("both.docx", [("Heading2", "Regions")], [rows], ["p", "t"])
        self.assertEqual(extract_docx_text(path),
                         clean_extracted_text(extract_docx_with_stdlib(path)))


class PptxRouteStaysCompatible(unittest.TestCase):
    def test_a_slide_table_comes_out_as_pipe_rows(self):
        try:
            from pptx import Presentation
        except ImportError:
            self.skipTest("python-pptx is not installed")
        from app.pptx_extract import extract_pptx_text
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Regions"
        table = deck.slides[0].shapes.add_table(3, 3, 100000, 2000000, 4000000, 1000000).table
        for column, heading in enumerate(("Region", "Q1", "Q2")):
            table.cell(0, column).text = heading
        for row, values in enumerate((("north", "12", "15"), ("south", "7", "9")), start=1):
            for column, value in enumerate(values):
                table.cell(row, column).text = value
        path = self.root = Path(tempfile.mkdtemp()) / "deck.pptx"
        deck.save(str(path))
        text = extract_pptx_text(path)
        self.assertIn("| Region | Q1 | Q2 |", text)
        self.assertIn("| north | 12 | 15 |", text)


class EmbedderTokenizerFiles(unittest.TestCase):
    """The window test needs the SERVED embedder's vocabulary, read from local files."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def _wordpiece_dir(self) -> Path:
        directory = self.root / "wp"
        directory.mkdir()
        (directory / "vocab.txt").write_text(
            "[PAD]\n[UNK]\n[CLS]\n[SEP]\n[MASK]\nthe\nquick\n##ly\nfox\njump\n##ed\nover\n"
            "dog\nac\n##me\n##-\n##jo\n##nes\ngm\nbh\n", encoding="utf-8")
        return directory

    def test_wordpiece_counts_subwords_and_the_wrapping_specials(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self._wordpiece_dir()))
        self.assertEqual(tokenizer.kind, "wordpiece")
        self.assertEqual(tokenizer.count("the quick fox"), 3 + 2, "three pieces plus CLS/SEP")
        self.assertEqual(tokenizer.count("quickly jumped"), 4 + 2,
                         "quick+##ly and jump+##ed: four pieces for two words")
        self.assertEqual(tokenizer.count("zzz zzz"), 2 + 2,
                         "an unknown word costs [UNK], not nothing")

    def test_wordpiece_from_tokenizer_json_honours_the_lowercase_normalizer(self):
        from app.embed_tokenizer import load_embed_tokenizer
        directory = self.root / "json"
        directory.mkdir()
        vocab = {token: index for index, token in enumerate(
            ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "the", "quick", "##ly", "fox"])}
        (directory / "tokenizer.json").write_text(json.dumps({
            "model": {"type": "WordPiece", "vocab": vocab, "unk_token": "[UNK]",
                      "continuing_subword_prefix": "##",
                      "special_tokens": ["[CLS]", "[SEP]"]},
            "normalizer": {"type": "Sequence", "normalizers": [{"type": "Lowercase"}]},
        }), encoding="utf-8")
        tokenizer = load_embed_tokenizer(str(directory))
        self.assertEqual(tokenizer.count("THE QUICKLY FOX"), 4 + 2)

    def test_unigram_vocabulary_is_segmented_by_probability(self):
        from app.embed_tokenizer import load_embed_tokenizer
        directory = self.root / "uni"
        directory.mkdir()
        vocab = [["<unk>", 0.0], ["<s>", 0.0], ["</s>", 0.0], ["\u2581the", -1.0],
                 ["\u2581quick", -1.5], ["ly", -2.0], ["\u2581fox", -1.0], ["\u2581", -8.0],
                 ["t", -5.0], ["he", -4.0], ["q", -5.0], ["u", -5.0], ["i", -5.0],
                 ["c", -5.0], ["k", -5.0]]
        (directory / "tokenizer.json").write_text(json.dumps({
            "model": {"type": "Unigram", "unk_id": 0, "unk_token": "<unk>", "vocab": vocab,
                      "special_tokens": ["<s>", "</s>"]},
        }), encoding="utf-8")
        tokenizer = load_embed_tokenizer(str(directory))
        self.assertEqual(tokenizer.kind, "unigram")
        self.assertEqual(tokenizer.count("the quick fox"), 3 + 2)
        self.assertGreater(tokenizer.count("the quikc fox"), 3,
                           "a garbled word costs more pieces: that is the whole point")

    def test_a_hub_id_is_refused_rather_than_downloaded(self):
        from app.embed_tokenizer import EmbedTokenizerError, load_embed_tokenizer
        with self.assertRaises(EmbedTokenizerError) as caught:
            load_embed_tokenizer("mixedai/mxbai-embed-large-v1")
        self.assertIn("never downloads", str(caught.exception))

    def test_an_unsupported_model_type_says_what_it_can_read(self):
        from app.embed_tokenizer import EmbedTokenizerError, load_embed_tokenizer
        directory = self.root / "bpe"
        directory.mkdir()
        (directory / "tokenizer.json").write_text(json.dumps(
            {"model": {"type": "BPE", "vocab": {"a": 0}, "merges": []}}), encoding="utf-8")
        with self.assertRaises(EmbedTokenizerError) as caught:
            load_embed_tokenizer(str(directory))
        self.assertIn("WordPiece", str(caught.exception))

    def test_the_count_is_larger_than_a_word_count_on_numbers_and_names(self):
        """The claim this module exists for: cl100k-style counts under-state the window."""
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self._wordpiece_dir()))
        # One written word, five pieces: a name is what a WordPiece tokenizer charges for.
        # (Every piece after the first carries the "##" prefix, which is why the vocab has
        # "##jo" rather than "jo".)
        self.assertEqual(tokenizer.count("acme-jones"), 5 + 2)
        self.assertGreater(tokenizer.count("the acme-jones"), len("the acme-jones".split()) + 2,
                           "the same string costs the embedder more than a word count")


class LibraryAdaptersWithFakeModules(unittest.TestCase):
    """The python-docx and pypdf adapters, exercised even though neither library is installed.

    These are the paths the worker image WILL use once requirements.txt is installed, so
    they are tested against a stand-in module rather than left uncovered. The stand-in only
    mimics the library's shape; the real formats are covered by the fixtures above.
    """

    def _fake_docx_module(self, blocks):
        import types

        class FakeParagraph:
            def __init__(self, element, document):
                raw = element.element                     # as python-docx hands the node over
                self.text = raw["text"]
                self.style = types.SimpleNamespace(name=raw["style"])

        class FakeTable:
            def __init__(self, element, document):
                raw = element.element
                self.rows = [types.SimpleNamespace(
                    cells=[types.SimpleNamespace(text=cell) for cell in row])
                    for row in raw["rows"]]

        class FakeBody:
            def __init__(self, elements):
                self._elements = elements

            def iterchildren(self):
                for element in self._elements:
                    tag = "p" if element["kind"] == "p" else "tbl"  # python-docx strips the prefix
                    yield types.SimpleNamespace(tag="{http://schemas.openxmlformats.org/"
                                                f"wordprocessingml/2006/main}}{tag}",
                                                element=element)

        document = types.SimpleNamespace(element=types.SimpleNamespace(
            body=FakeBody(blocks)))
        docx = types.ModuleType("docx")
        docx.Document = lambda path: document
        table_module = types.ModuleType("docx.table")
        table_module.Table = FakeTable
        paragraph_module = types.ModuleType("docx.text.paragraph")
        paragraph_module.Paragraph = FakeParagraph
        text_module = types.ModuleType("docx.text")
        oxml = types.ModuleType("docx.oxml")
        ns = types.ModuleType("docx.oxml.ns")
        ns.qn = lambda tag: tag
        text_module.paragraph = paragraph_module
        oxml.ns = ns
        docx.table = table_module
        docx.text = text_module
        docx.oxml = oxml
        return types.SimpleNamespace(docx=docx, table=table_module, paragraph=paragraph_module,
                                     text=text_module, oxml=oxml, ns=ns)

    def test_the_python_docx_adapter_walks_document_order_and_shapes_tables(self):
        import sys
        import unittest.mock as mock
        from app.docx_extract import extract_docx_text
        blocks = [
            {"kind": "p", "style": "Heading 1", "text": "Budget detail"},
            {"kind": "p", "style": "Normal", "text": "the table below lists regions"},
            {"kind": "t", "rows": [["Region", "Q1"], ["north", "12"], ["south", "7"]]},
            {"kind": "p", "style": "Normal", "text": "and the note after it"},
        ]
        fakes = self._fake_docx_module(blocks)
        with mock.patch.dict(sys.modules, {"docx": fakes.docx, "docx.table": fakes.table,
                                           "docx.text": fakes.text,
                                           "docx.text.paragraph": fakes.paragraph,
                                           "docx.oxml": fakes.oxml, "docx.oxml.ns": fakes.ns}):
            path = Path(tempfile.mkdtemp()) / "library.docx"
            path.write_bytes(b"the fake Document ignores the bytes")
            text = extract_docx_text(path)
        lines = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(lines, ["# Budget detail", "the table below lists regions", "[Table]",
                                 "| Region | Q1 |", "| north | 12 |", "| south | 7 |",
                                 "and the note after it"],
                         "document order and table rows are the whole point of this adapter")

    def test_the_pypdf_adapter_reads_page_by_page(self):
        import sys
        import types
        import unittest.mock as mock
        from app.pdf_extract import extract_pdf_text
        pages = [types.SimpleNamespace(extract_text=lambda: "Region        Q1\nnorth         12"),
                 types.SimpleNamespace(extract_text=lambda: "footer line")]
        reader = types.SimpleNamespace(pages=pages, is_encrypted=False, decrypt=lambda _: True)
        pypdf = types.ModuleType("pypdf")
        pypdf.PdfReader = lambda path: reader
        with mock.patch.dict(sys.modules, {"pypdf": pypdf}), \
             mock.patch("app.pdf_extract.pdftotext_available", lambda: False):
            path = Path(tempfile.mkdtemp()) / "fake.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            text = extract_pdf_text(path)
        rows = [line for line in text.splitlines() if line.strip()]
        self.assertEqual(rows[0].split(), ["Region", "Q1"],
                         "the column gap must survive into a table row")
        self.assertEqual(rows[-1].split(), ["footer", "line"], "page 2 is not dropped")
        self.assertTrue(any("north" in line for line in rows))

    def test_the_backend_that_actually_ran_is_reported(self):
        import sys
        import types
        import unittest.mock as mock
        from app.pdf_extract import (PDF_BACKEND_LAYOUT, PDF_BACKEND_PYPDF,
                                     extract_pdf_text_with_backend, pdftotext_available)
        page = types.SimpleNamespace(extract_text=lambda: "Region   Q1\nnorth   12\nfooter line")
        reader = types.SimpleNamespace(pages=[page], is_encrypted=False, decrypt=lambda _: True)
        pypdf = types.ModuleType("pypdf")
        pypdf.PdfReader = lambda path: reader
        path = Path(tempfile.mkdtemp()) / "fake.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        with mock.patch.dict(sys.modules, {"pypdf": pypdf}), \
             mock.patch("app.pdf_extract.pdftotext_available", lambda: False):
            _text, used = extract_pdf_text_with_backend(path)
        self.assertEqual(used, PDF_BACKEND_PYPDF)
        if not pdftotext_available():
            self.skipTest("poppler's pdftotext is not installed here")
        real = make_pdf([[(72, 760, "1. Revenue"), (72, 720, "north         120")]],
                        Path(tempfile.mkdtemp()) / "layout.pdf")
        _text, used = extract_pdf_text_with_backend(real, backend="pdftotext")
        self.assertEqual(used, PDF_BACKEND_LAYOUT)

    def test_pypdf_style_text_without_column_layout_is_still_a_document(self):
        """pypdf loses the column positions, so a wrapped cell arrives flush left.

        Nothing can rejoin that reliably, and guessing would move words between rows. The
        promise here is narrower and must hold: the text arrives whole, nothing raises, and
        the chunker still accounts for every word.
        """
        import sys
        import types
        import unittest.mock as mock
        from app.pdf_extract import extract_pdf_text
        from app.structure_chunker import chunk_document, coverage_gaps
        flat = ("1. Revenue\nRegion\nQ1\nQ2\nnorth\n120\n150\n"
                "the north region grew on the same headcount as last year, which the "
                "review noted twice in the same paragraph about the carrier lane.\n"
                "2. Costs\nCosts fell after the vendor renegotiation finished for the "
                "depot, and the carrier kept the same lane at the same rate for the term.")
        page = types.SimpleNamespace(extract_text=lambda: flat)
        reader = types.SimpleNamespace(pages=[page], is_encrypted=False, decrypt=lambda _: True)
        pypdf = types.ModuleType("pypdf")
        pypdf.PdfReader = lambda path: reader
        with mock.patch.dict(sys.modules, {"pypdf": pypdf}), \
             mock.patch("app.pdf_extract.pdftotext_available", lambda: False):
            path = Path(tempfile.mkdtemp()) / "flat.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            text = extract_pdf_text(path)
        self.assertEqual(" ".join(sorted(text.split())),
                         " ".join(sorted(flat.split())),
                         "no word is invented, moved or lost when the layout is absent")
        plan = chunk_document(text, filename="flat.txt", max_piece_tokens=200,
                              min_piece_tokens=40, header_tokens=20)
        self.assertEqual(coverage_gaps(text, plan)["unaccounted_word_tokens"], 0)

    def test_a_missing_pypdf_says_which_backends_are_missing(self):
        import sys
        import unittest.mock as mock
        from app.pdf_extract import PDF_LIB_MISSING, PdfExtractError, extract_pdf_text
        with mock.patch.dict(sys.modules, {"pypdf": None}), \
             mock.patch("app.pdf_extract.pdftotext_available", lambda: False):
            path = Path(tempfile.mkdtemp()) / "nothing.pdf"
            path.write_bytes(b"%PDF-1.4 fake")
            with self.assertRaises(PdfExtractError) as caught:
                extract_pdf_text(path)
        self.assertEqual(caught.exception.error_code, PDF_LIB_MISSING)
        self.assertIn("pdftotext", str(caught.exception))



class BgeM3ShapedTokenizerFiles(unittest.TestCase):
    """The three files BGE-M3 ships, rebuilt synthetically: XLMRobertaTokenizer, Unigram.

    What this proves is local parsing and counting: the vocabulary, the specials, the
    byte-fallback path and the pre-tokenizer split. What it cannot prove is what the
    served BGE-M3 actually does with a real 250k vocabulary - the real files are not in
    this repository and no model server is reachable here.
    """

    WORDS = ["▁the", "▁depot", "▁carrier", "▁lane", "▁freight", "▁tariff", "▁customs",
             "▁origin", "▁papers", "▁margin", "▁period", "▁moved", "the", "depot",
             "carrier", "lane", "freight", "tariff", "customs", ".", ",", "!", "?",
             "▁", "<unk>", "<s>", "</s>", "<pad>"]

    def build(self, *, gz: bool = False) -> Path:
        import gzip
        import json
        directory = Path(tempfile.mkdtemp())
        byte_tokens = [[f"<0x{value:02X}>", -15.0] for value in range(256)]
        vocab = [[token, -1.0 - index / 100.0] for index, token in enumerate(self.WORDS)]
        vocab += byte_tokens
        tokenizer = {
            "version": "1.0",
            "truncation": {"direction": "Right", "maximum_length": 8192},
            "normalizer": {"type": "Sequence", "normalizers": [{"type": "Lowercase"}]},
            "pre_tokenizer": {"type": "Sequence", "pretokenizers": [
                {"type": "Split", "pattern": {"String": "()!?,;\""}, "behavior": "Isolated"},
                {"type": "Whitespace"},
                {"type": "PreservePattern",
                 "pattern": {"Regex": r"\p{Han}|\p{Hangul}|\p{Hiragana}|\p{Katakana}"}}]},
            "model": {"type": "Unigram", "unk_id": self.WORDS.index("<unk>"),
                      "vocab": vocab, "byte_fallback": True,
                      "unk_piece": "<unk>", "special_tokens": ["<s>", "</s>"]},
            "added_tokens": [{"id": 1, "content": "<s>", "special": True},
                             {"id": 2, "content": "</s>", "special": True},
                             {"id": 3, "content": "<unk>", "special": True},
                             {"id": 4, "content": "<pad>", "special": True}],
        }
        if gz:
            with gzip.open(directory / "tokenizer.json.gz", "wt", encoding="utf-8") as handle:
                json.dump(tokenizer, handle)
        else:
            (directory / "tokenizer.json").write_text(json.dumps(tokenizer), encoding="utf-8")
        (directory / "tokenizer_config.json").write_text(json.dumps(
            {"model_max_length": 8192, "do_lower_case": True, "tokenizer_class":
             "XLMRobertaTokenizer"}), encoding="utf-8")
        (directory / "special_tokens_map.json").write_text(json.dumps(
            {"bos_token": "<s>", "eos_token": "</s>", "unk_token": "<unk>",
             "pad_token": "<pad>"}), encoding="utf-8")
        return directory

    def test_the_three_files_load_as_one_unigram_tokenizer(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        self.assertEqual(tokenizer.kind, "unigram")
        self.assertEqual(tokenizer.special_overhead, 2)
        self.assertTrue(tokenizer.byte_fallback)
        self.assertRegex(tokenizer.name, r"^unigram-\d+-entry-local\+2special$")

    def test_a_gzipped_tokenizer_json_loads_the_same_way(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build(gz=True)))
        self.assertEqual(tokenizer.kind, "unigram")
        self.assertTrue(tokenizer.byte_fallback)

    def test_count_includes_the_two_specials(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        self.assertEqual(tokenizer.count("depot"), len(tokenizer.segments("depot")) + 2)

    def test_case_folding_follows_the_files(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        self.assertEqual(tokenizer.count("DEPOT CARRIER"), tokenizer.count("depot carrier"))

    def test_an_unknown_character_costs_its_utf8_bytes_not_one_token(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        base = tokenizer.count("depot")
        # Neither character is in this synthetic vocabulary, so byte fallback decides.
        # The extra token over the byte count is the word-start marker the space becomes.
        self.assertEqual(tokenizer.count("depot 企") - base, 4,
                         "the word marker plus three UTF-8 bytes: BGE-M3 spends a byte "
                         "token per byte on text its vocabulary cannot represent")
        self.assertEqual(tokenizer.segments("depot 企")[-3:],
                         ["<0xE4>", "<0xBC>", "<0x81>"])
        self.assertEqual(tokenizer.segments("depot é")[-2:], ["<0xC3>", "<0xA9>"])
        self.assertEqual(tokenizer.segments("depot carrier"), ["▁depot", "▁carrier"],
                         "text the vocabulary does hold is segmented normally")

    def test_punctuation_is_segmented_as_its_own_piece(self):
        """Shape, not count: with punctuation tokens in the vocabulary the count is the
        same either way, and saying otherwise would be a claim this test does not prove.
        What matters is that a comma is never glued into the word in front of it."""
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        # The isolated comma carries its own word-start marker, which is one token more
        # than XLM-R's "▁," piece: over-counting is the only safe direction here.
        self.assertEqual(tokenizer.segments("depot, carrier"),
                         ["▁depot", "▁", ",", "▁carrier"])
        self.assertNotIn("depot,", tokenizer.segments("depot, carrier"))

    def test_the_window_default_is_bge_m3s_window(self):
        from app.embed_tokenizer import EMBED_WINDOW_DEFAULT, load_embed_tokenizer
        self.assertEqual(EMBED_WINDOW_DEFAULT, 8192,
                         "BGE-M3's real window; the retired 512-token model is why a "
                         "1200-token chunk used to be truncated silently")
        self.assertEqual(load_embed_tokenizer(None), None,
                         "no tokenizer path means the conservative over-count, not a guess")

    def test_the_added_specials_are_not_segmentable_text(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        self.assertNotIn("<s>", tokenizer.body)
        self.assertNotIn("</s>", tokenizer.body)
        self.assertNotIn("<unk>", tokenizer.body)

    def test_the_byte_tokens_come_from_the_file_not_from_imagination(self):
        from app.embed_tokenizer import load_embed_tokenizer
        tokenizer = load_embed_tokenizer(str(self.build()))
        self.assertEqual(tokenizer.byte_tokens.get(0xE9), "<0xE9>")
        self.assertTrue(all(f"<0x{value:02X}>" in tokenizer.body for value in range(256)))



if __name__ == "__main__":
    unittest.main()
