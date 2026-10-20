"""DOCX text extraction for the ingest worker, shaped for the structure chunker.

The upload route hands a ``.docx`` to LightRAG's legacy parser, which walks paragraphs and
loses the table grid - after that a 60-row budget table is just a run of lines that
``fixed_token`` slices wherever the token window happens to fall. With
``STRUCTURE_CHUNKING_ENABLED`` on, the worker reads the document itself and hands the
chunker text whose tables are still tables:

* paragraphs in document order (not all paragraphs then all tables - a report's table sits
  between the sentences that introduce it, and that lead-in is what makes the row
  searchable);
* each table emitted as a canonical pipe-table row, header row first, one row per line, cell
  newlines flattened to spaces: ``| Region | Q1 | Q2 |``. Leading and trailing bars are not
  decoration: they are the shape app.structure_chunker reads as a table,
  so the header row is repeated on every piece a long table splits across, and a piece that
  must split further breaks at cell boundaries;
* a ``[Table]`` marker line before each table, matching what the PPTX extractor already
  emits, so an operator reading a stored chunk can tell a table from prose;
* heading-styled paragraphs (``Heading 1``..``Heading 6``, ``Title``) emitted as markdown
  headings, which is how the chunker builds the section path it puts in each header. The
  style name is the document's own structure, not an invention; a document without heading
  styles gets plain paragraphs and nothing is invented in its place.

Merged cells: python-docx reports a merged cell's text once per covered grid column, so a
spanned cell's words repeat inside the row. Repeating is safer than dropping (the row stays
aligned with its header), and it is visible in the text rather than hidden.

``python-docx`` is imported inside the function so this module stays importable without it;
the worker then falls back to ``/documents/upload``, which is today's behaviour.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .pptx_extract import cap_extracted_text, clean_extracted_text

DOCX_LIB_MISSING = "docx_lib_missing"
DOCX_EXTRACT_FAILED = "docx_extract_failed"
DOCX_EMPTY = "docx_empty"

_HEADING_STYLE = re.compile(r"^(?:heading\s*([1-6])|title)$", re.IGNORECASE)
_CELL_BREAKS = re.compile(r"\s*\n\s*")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


class DocxExtractError(Exception):
    """Extraction produced nothing usable.

    ``error_code``: ``docx_lib_missing``, ``docx_extract_failed`` (corrupt, encrypted,
    unreadable) or ``docx_empty`` (a document with no text at all - usually an image-only
    file or a wrapper of nothing but drawings).
    """

    def __init__(self, message: str, *, error_code: str = DOCX_EXTRACT_FAILED) -> None:
        super().__init__(message)
        self.error_code = error_code


def _heading_for_style(style_name: str) -> str:
    """'#'..'######' for a heading-styled paragraph, '' for everything else.

    Both spellings arrive: python-docx reports the pretty name ("Heading 2"), the raw
    OOXML style id is "Heading2". Neither is invented here - a document with no heading
    styles gets plain paragraphs.
    """
    match = _HEADING_STYLE.match(str(style_name or "").strip())
    if not match:
        return ""
    level = match.group(1)
    return "#" if level is None else "#" * min(6, max(1, int(level)))


def _heading_prefix(paragraph: Any) -> str:
    try:
        style = getattr(paragraph, "style", None)
        name = str(getattr(style, "name", "") or "").strip()
    except Exception:                               # a corrupted style reference
        return ""
    return _heading_for_style(name)


def table_to_rows(table: Any) -> list[str]:
    """Pipe-shaped rows, header row first, one row per line."""
    rows: list[str] = []
    for row in getattr(table, "rows", []) or []:
        cells = []
        for cell in getattr(row, "cells", []) or []:
            text = _MULTI_SPACE.sub(" ", _CELL_BREAKS.sub(" ", str(getattr(cell, "text", "")
                                                                     or "").strip()))
            cells.append(text.replace("|", "/"))
        if not cells:
            continue
        if any(cells):
            # Canonical pipe row (leading and trailing bar): that is the shape
            # app.structure_chunker protects - header repeated on every piece it splits.
            rows.append("| " + " | ".join(cells) + " |")
    return rows


def _iter_blocks(document: Any, paragraph_cls: Any, table_cls: Any):
    """Paragraphs and tables in DOCUMENT order (python-docx separates the two by default)."""
    body = getattr(getattr(document, "element", None), "body", None)
    if body is None:
        return
    children = getattr(body, "iterchildren", None)
    if children is None:
        return
    for child in children():
        tag = str(getattr(child, "tag", "") or "")
        if tag.endswith("}p"):
            yield paragraph_cls(child, document)
        elif tag.endswith("}tbl"):
            yield table_cls(child, document)


_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _xml_run_text(element: Any) -> str:
    """All text inside an OOXML element, tabs and breaks folded to spaces."""
    parts: list[str] = []
    for node in element.iter():
        if node.tag == f"{_W}t":
            parts.append(node.text or "")
        elif node.tag == f"{_W}tab":
            parts.append("  ")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append(" ")
    return re.sub(r"\s+", " ", "".join(parts)).strip()


def _xml_style(element: Any) -> str:
    properties = element.find(f"{_W}pPr")
    if properties is None:
        return ""
    style = properties.find(f"{_W}pStyle")
    return str(style.get(f"{_W}val") or "") if style is not None else ""


def extract_docx_with_stdlib(path: Path) -> str:
    """.docx text with no third-party library: a .docx is a ZIP of XML.

    This is the reason python-docx is a preference rather than a hard dependency: the route
    works in an image that has not been rebuilt yet, and the same table-shaping rules apply
    either way. Only direct children of the body are walked, so a table nested inside a cell
    is read as that cell's text (its rows are flattened into the outer row) - documented,
    not silent.
    """
    import zipfile                                # noqa: PLC0415
    import xml.etree.ElementTree as ET            # noqa: PLC0415
    try:
        with zipfile.ZipFile(str(path)) as archive:
            raw = archive.read("word/document.xml")
    except KeyError:
        raise DocxExtractError("DOCX archive has no word/document.xml (not a WordprocessingML "
                               "document?)", error_code=DOCX_EXTRACT_FAILED) from None
    except (zipfile.BadZipFile, OSError) as exc:
        raise DocxExtractError(f"DOCX could not be read: {type(exc).__name__}: "
                               f"{str(exc)[:160]}", error_code=DOCX_EXTRACT_FAILED) from exc
    try:
        root = ET.fromstring(raw.decode("utf-8", errors="replace"))
    except ET.ParseError as exc:
        raise DocxExtractError(f"DOCX document.xml is not well-formed XML: {exc}",
                               error_code=DOCX_EXTRACT_FAILED) from exc
    body = root.find(f"{_W}body")
    out: list[str] = []
    for child in (list(body) if body is not None else []):
        if child.tag == f"{_W}p":
            text = _xml_run_text(child)
            if not text:
                continue
            prefix = _heading_for_style(_xml_style(child))
            out.append(f"{prefix} {text}".strip() if prefix else text)
        elif child.tag == f"{_W}tbl":
            rows: list[str] = []
            for row in child.findall(f"{_W}tr"):
                cells = [_xml_run_text(cell).replace("|", "/")
                         for cell in row.findall(f"{_W}tc")]
                if cells and any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                out.append("[Table]")
                out.extend(rows)
                out.append("")
    return "\n".join(out)


def extract_docx_text(path: Path) -> str:
    """Document-order text of a .docx, tables kept as rows. Raises DocxExtractError.

    python-docx when it is installed (it normalises more Word quirks), the stdlib OOXML
    reader otherwise. Both produce the same shape: paragraphs in document order, tables as
    pipe-shaped rows with the header row first, heading styles as markdown headings.
    """
    try:
        import docx                                  # noqa: PLC0415 - optional dependency
        from docx.table import Table                 # noqa: PLC0415
        from docx.text.paragraph import Paragraph    # noqa: PLC0415
    except ImportError:
        text = clean_extracted_text(extract_docx_with_stdlib(path))
        if not text:
            raise DocxExtractError("DOCX carries no text (image-only or empty document)",
                                   error_code=DOCX_EMPTY)
        return text
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocxExtractError(f"python-docx could not read the file: "
                               f"{type(exc).__name__}: {str(exc)[:160]}",
                               error_code=DOCX_EXTRACT_FAILED) from exc

    out: list[str] = []
    for block in _iter_blocks(document, Paragraph, Table):
        if isinstance(block, Table):
            rows = table_to_rows(block)
            if rows:
                out.append("[Table]")
                out.extend(rows)
                out.append("")
            continue
        prefix = _heading_prefix(block)
        try:
            text = str(block.text or "").strip()
        except Exception:
            continue
        if not text:
            continue
        out.append(f"{prefix} {text}".strip() if prefix else text)
    text = clean_extracted_text("\n".join(out))
    if not text:
        raise DocxExtractError("DOCX carries no text (image-only or empty document)",
                               error_code=DOCX_EMPTY)
    return text


def extract_docx_payload(path: Path, *, max_bytes: int = 0, max_chars: int = 0) -> str:
    """Byte-guard, extract, normalise and cap. Same shape as the PPTX route."""
    if max_bytes:
        size = path.stat().st_size
        if size > max_bytes:
            raise DocxExtractError(
                f"DOCX too large to extract: {size} bytes exceeds the configured limit",
                error_code=DOCX_EXTRACT_FAILED)
    return cap_extracted_text(extract_docx_text(path), int(max_chars) or None)
