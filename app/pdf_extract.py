"""PDF text extraction for the ingest worker, shaped for the structure chunker.

Why the worker extracts at all
------------------------------
``.pdf`` is 59% of the corpus together with ``.docx``. Uploading it means LightRAG's legacy
parser flattens the page and ``fixed_token`` cuts it: a table row can end up in one chunk
and its header in another. With ``STRUCTURE_CHUNKING_ENABLED`` on, the worker extracts the
text itself, hands it to :mod:`app.structure_chunker`, and submits
``/documents/text`` with the ``fixed_token`` + ``split_by_character_only`` params, so the
cut points are ours.

What the text is shaped for
---------------------------
pypdf's ``extract_text()`` gives one physical line per positioned text run. In a table, a
LOGICAL row is often 3-8 physical lines: the cells wrap and the wrapped continuation lines
come out indented under their column, with no blank line until the table ends. Chunking
those line-by-line cuts rows in half, which is exactly the failure this route exists to
avoid. So a contiguous table-like block is reassembled:

* a block is a run of consecutive non-blank lines, most of them table-like (two or more
  whitespace-separated runs AND columnar indentation or numeric content);
* within a block, a line at the block's minimum indentation STARTS a row; a deeper-indented
  line CONTINUES the current row (it is a wrapped cell, whichever column it came from);
* a row is emitted as one physical line, its lines joined by two spaces. Two spaces is not
  cosmetic: the chunker reads a two-space gap as a column boundary, so the row stays a
  table row (header row repeated when the table splits) and a re-cut lands on a cell
  boundary rather than mid-word.

Prose is left alone: paragraph lines stay lines, and the chunker packs them. Page furniture
(running headers, footers, page numbers) is deliberately left IN PLACE - the chunker drops
lines that repeat three or more times and counts the drops, which is a better test of what
is furniture than anything a parser can guess page by page.

No structure is invented: no page headings, no table markers, no column names.

Dependencies
------------
``pypdf`` is imported inside the function, so this module stays importable when the library
is absent (the worker then falls back to ``/documents/upload``, which is today's behaviour).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from .pptx_extract import cap_extracted_text, clean_extracted_text

PDF_LIB_MISSING = "pdf_lib_missing"
PDF_EXTRACT_FAILED = "pdf_extract_failed"
PDF_EMPTY = "pdf_empty"

# A table cell line has separate columns; a prose line almost never does.
_COLUMN_GAP = re.compile(r"\S[ \t]{2,}\S")
_NUMERIC_RUN = re.compile(r"(?<![\w.])[-+\u2212]?\d[\d,\u00a0.]*%?(?![\w])")
_PAGE_BREAK = re.compile(r"[\f\u000c]")


class PdfExtractError(Exception):
    """Extraction produced nothing usable.

    ``error_code`` is what the worker writes to ``document_states.error_code``:
    ``pdf_lib_missing``, ``pdf_extract_failed`` (corrupt, encrypted, unreadable) or
    ``pdf_empty`` (no text layer at all - a scan. The caller falls back to
    ``/documents/upload`` rather than submitting an empty document).
    """

    def __init__(self, message: str, *, error_code: str = PDF_EXTRACT_FAILED) -> None:
        super().__init__(message)
        self.error_code = error_code


# The two PDF readers, named in the state row. "-layout" is spelled out because it is the
# flag that matters, not the program.
PDF_BACKEND_LAYOUT = "pdftotext-layout"
PDF_BACKEND_PYPDF = "pypdf"


def _indent_of(line: str) -> int:
    expanded = line.replace("\t", "    ")
    return len(expanded) - len(expanded.lstrip(" "))


def _is_table_like(line: str) -> bool:
    """A line that looks like a positioned table cell rather than running prose."""
    stripped = line.strip()
    if not stripped or len(stripped) > 600:
        return False
    if stripped.startswith(("|", "\u2502")):
        return True                      # already pipe-shaped
    if len(stripped.split()) < 2:
        return False
    if _COLUMN_GAP.search(stripped):
        return True
    return len(_NUMERIC_RUN.findall(stripped)) >= 2


def join_wrapped_rows(text: str) -> str:
    """Reassemble logical table rows that the PDF text layer split across lines.

    Pure and total: prose in, prose out; a table-like block in, one line per logical row
    out. Never drops a character - the only change is which physical line a fragment sits
    on. A block starts at a table-like line and runs while the lines are non-blank and are
    either table-like or indented deeper than the block's left margin (a wrapped cell can be
    a single word); it ends at a blank line or at prose back at the margin.
    """
    out: list[str] = []
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip() or not _is_table_like(line):
            out.append(line.rstrip())
            index += 1
            continue
        floor = _indent_of(line)
        block = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            follower = lines[index]
            if _is_table_like(follower) or _indent_of(follower) > floor:
                block.append(follower)
                index += 1
            else:
                break
        out.extend(_rows_from_block(block))
    return "\n".join(out)


def _rows_from_block(block: list[str]) -> list[str]:
    """One output line per logical row: minimum-indent lines start a row."""
    if not block:
        return []
    floor = min(_indent_of(line) for line in block)
    rows: list[list[str]] = []
    for line in block:
        if not rows:
            rows.append([line.strip()])
        elif _indent_of(line) > floor:
            rows[-1].append(line.strip())        # a wrapped cell: same logical row
        else:
            rows.append([line.strip()])          # back to the left margin: a new row
    return ["  ".join(parts) for parts in rows if parts]


PDF_TEXT_MAX_BYTES = 8_000_000     # a PDF that yields more than this is not going to the LLM


def pdftotext_available() -> bool:
    """True when poppler's pdftotext is on PATH (the worker image installs poppler-utils)."""
    return bool(shutil.which("pdftotext"))


def _run_pdftotext(path: Path, *, timeout: float = 120.0) -> str:
    """``pdftotext -layout`` to stdout. No shell, fixed argv, bounded output and time."""
    argv = ["pdftotext", "-q", "-layout", "-enc", "UTF-8", str(path), "-"]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:                 # raced with an image change
        raise PdfExtractError("pdftotext disappeared mid-run", error_code=PDF_LIB_MISSING) from exc
    except subprocess.TimeoutExpired as exc:
        raise PdfExtractError(f"pdftotext timed out after {timeout:g}s",
                              error_code=PDF_EXTRACT_FAILED) from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[:160]
        raise PdfExtractError(f"pdftotext exited {proc.returncode}: {detail}",
                              error_code=PDF_EXTRACT_FAILED)
    raw = proc.stdout or b""
    if len(raw) > PDF_TEXT_MAX_BYTES:
        raise PdfExtractError(f"pdftotext produced {len(raw)} bytes, over the "
                              f"{PDF_TEXT_MAX_BYTES}-byte ceiling",
                              error_code=PDF_EXTRACT_FAILED)
    return raw.decode("utf-8", errors="replace")


def _extract_with_pypdf(path: Path) -> str:
    try:
        from pypdf import PdfReader        # noqa: PLC0415 - optional at install time
    except ImportError as exc:
        raise PdfExtractError(
            "pypdf is not installed and poppler's pdftotext is not on PATH",
            error_code=PDF_LIB_MISSING) from exc
    try:
        reader = PdfReader(str(path))
        if getattr(reader, "is_encrypted", False) and not reader.decrypt(""):
            raise PdfExtractError("PDF is encrypted", error_code=PDF_EXTRACT_FAILED)
        pages = [str(page.extract_text() or "") for page in reader.pages]
    except PdfExtractError:
        raise
    except Exception as exc:                       # corrupt file, truncated stream, oom guard
        raise PdfExtractError(f"pypdf could not read the file: "
                              f"{type(exc).__name__}: {str(exc)[:160]}",
                              error_code=PDF_EXTRACT_FAILED) from exc
    if not pages:
        raise PdfExtractError("PDF has no pages", error_code=PDF_EXTRACT_FAILED)
    return "\n\n".join(pages)


def extract_pdf_text_with_backend(path: Path, *, backend: str = "auto") -> tuple[str, str]:
    """Text of a PDF with logical rows reassembled, plus the backend that produced it.

    The backend is a fact about the result, not an implementation detail: the row
    reassembly reads the column positions only ``pdftotext -layout`` reproduces (Hermes'
    run: 803 rows rejoined with poppler, 0 with pypdf), so a state row that does not say
    which one ran cannot explain why two PDFs chunked differently.

    Backend order, and it matters: ``pdftotext -layout`` reproduces the column positions as
    spaces, which is what the row reassembly reads, and poppler-utils is already in the
    worker image, so it needs no new dependency. pypdf is the fallback for a host without
    poppler. ``backend`` pins one of them when an experiment needs reproducibility.
    """
    requested = (backend or "auto").strip().lower()
    if requested not in ("auto", "pdftotext", "pypdf"):
        raise PdfExtractError(f"unknown PDF text backend {requested!r} "
                              "(auto, pdftotext or pypdf)", error_code=PDF_EXTRACT_FAILED)
    if requested == "pypdf":
        body, used = _extract_with_pypdf(path), PDF_BACKEND_PYPDF
    elif requested == "pdftotext":
        if not pdftotext_available():
            raise PdfExtractError("pdftotext is not on PATH", error_code=PDF_LIB_MISSING)
        body, used = _run_pdftotext(path), PDF_BACKEND_LAYOUT
    elif pdftotext_available():
        body, used = _run_pdftotext(path), PDF_BACKEND_LAYOUT
    else:
        body, used = _extract_with_pypdf(path), PDF_BACKEND_PYPDF
    raw_hints = body.count("\f") + 1
    body = _PAGE_BREAK.sub("\n", body)
    joined = join_wrapped_rows(clean_extracted_text(body))
    if not joined.strip():
        # A scan has no text layer. LightRAG's own parser (or an OCR step upstream) is the
        # only thing that can do better here, so this is a fallback signal, not a failure.
        raise PdfExtractError(f"PDF has no extractable text layer ({raw_hints} pages of "
                              "output, no characters of text)", error_code=PDF_EMPTY)
    return joined, used


def extract_pdf_text(path: Path, *, backend: str = "auto") -> str:
    """The text only. See :func:`extract_pdf_text_with_backend` for the backend."""
    return extract_pdf_text_with_backend(path, backend=backend)[0]


def extract_pdf_payload_with_backend(path: Path, *, max_bytes: int = 0, max_chars: int = 0,
                                     backend: str = "auto") -> tuple[str, str]:
    """Byte-guard, extract, normalise and cap, and say which backend ran."""
    if max_bytes:
        size = path.stat().st_size
        if size > max_bytes:
            raise PdfExtractError(
                f"PDF too large to extract: {size} bytes exceeds the configured limit",
                error_code=PDF_EXTRACT_FAILED)
    text, used = extract_pdf_text_with_backend(path, backend=backend)
    return cap_extracted_text(text, int(max_chars) or None), used


def extract_pdf_payload(path: Path, *, max_bytes: int = 0, max_chars: int = 0,
                        backend: str = "auto") -> str:
    """Byte-guard, extract, normalise and cap. Same shape as the PPTX route."""
    return extract_pdf_payload_with_backend(path, max_bytes=max_bytes, max_chars=max_chars,
                                            backend=backend)[0]
