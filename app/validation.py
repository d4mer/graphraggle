"""Pre-ingest validation gate for document classification and verdict assignment.

This module implements the validation decisions defined in Packet 02:
Validation Gate Design. It classifies files into text_like / binary_office /
unsupported categories and assigns one of four verdicts:

  * accept      – safe to proceed normally
  * warn        – proceed but surface warnings to operators
  * reject      – do not submit to LightRAG
  * auto_split  – file is risky because of size; operator or later automation
                  must split before submission

Decision flow (deterministic):

  1. Unsupported extension → reject
  2. Empty file           → reject
  3. Duplicate (provided by caller) → warn with duplicate metadata
  4. Large text-like file → auto_split (if file_size >= AUTO_SPLIT_THRESHOLD)
  5. Fallback encoding    → warn
  6. Everything else      → accept

Size thresholds (tunable; see threshold recommendations in ADR):

  * AUTO_SPLIT_THRESHOLD = 500_000 bytes (~500 KB)
    Text-like files at or above this size are flagged auto_split to avoid
    opaque LightRAG timeouts on long transcripts.

  * WARN_SIZE_THRESHOLD  = 100_000 bytes (~100 KB)
    Text-like files between this and AUTO_SPLIT_THRESHOLD are accepted but
    flagged warn so operators are aware of potentially slow processing.

Duplicate detection is intentionally caller-driven. The upload API and
worker can use persistent document state to decide whether the current file
should be treated as duplicate content, then pass that structured metadata
into ``validate_file()``.

Public API
----------

* ``classify_file(filename: str) -> str``
  Returns one of ``"text_like"``, ``"binary_office"``, or ``"unsupported"``.

* ``validate_upload(filename: str, file_size: int) -> ValidationVerdict``
  Lightweight validation for the upload endpoint. Does NOT check duplicates
  (database not available at upload time).

* ``validate_file(path: Path, *, file_size: int | None = None,
                  sha256: str | None = None) -> ValidationVerdict``
  Full validation for the worker. Checks extension, emptiness, duplicates,
  size, and encoding.

* ``reset_seen_hashes() -> None``
  Compatibility no-op retained for earlier Packet 02 references.

"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

TEXT_LIKE_EXTENSIONS: frozenset[str] = frozenset(
    {".txt", ".md", ".html", ".htm", ".json", ".csv"}
)

BINARY_OFFICE_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx"}
)

SUPPORTED_EXTENSIONS: frozenset[str] = (
    TEXT_LIKE_EXTENSIONS | BINARY_OFFICE_EXTENSIONS
)


def classify_file(filename: str) -> str:
    """Classify a file by its extension.

    Returns one of:

    * ``"text_like"``  – plain-text or structured text
    * ``"binary_office"`` – PDF or Office binary formats
    * ``"unsupported"``  – not in any supported extension set
    """
    ext = Path(filename).suffix.lower()
    if ext in TEXT_LIKE_EXTENSIONS:
        return "text_like"
    if ext in BINARY_OFFICE_EXTENSIONS:
        return "binary_office"
    return "unsupported"


# ---------------------------------------------------------------------------
# Thresholds (Packet 02 recommendations)
# ---------------------------------------------------------------------------

# Text-like files at or above this size are flagged auto_split.
# Chosen to catch long transcripts and large reports that commonly timeout
# in LightRAG without being so aggressive that normal documents are blocked.
AUTO_SPLIT_THRESHOLD: int = 500_000  # bytes (~500 KB)

# Text-like files between this threshold and AUTO_SPLIT_THRESHOLD are
# accepted with a warning so operators are aware of potentially slow processing.
WARN_SIZE_THRESHOLD: int = 100_000  # bytes (~100 KB)


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


class ValidationVerdict:
    """Immutable verdict returned by the validation gate.

    Attributes
    ----------
    verdict : str
        One of ``"accept"``, ``"warn"``, ``"reject"``, ``"auto_split"``.
    meta : dict[str, Any]
        Optional structured metadata (e.g. ``error_code``, ``file_class``,
        ``original_path`` for duplicates).
    """

    __slots__ = ("verdict", "meta")

    def __init__(self, verdict: str, meta: dict[str, Any] | None = None) -> None:
        self.verdict = verdict
        self.meta = meta or {}

    def __repr__(self) -> str:  # pragma: no cover
        return f"ValidationVerdict(verdict={self.verdict!r}, meta={self.meta})"


# ---------------------------------------------------------------------------
# Duplicate tracking compatibility shim
# ---------------------------------------------------------------------------

def reset_seen_hashes() -> None:
    """Retained as a compatibility no-op for earlier references."""


def _compute_sha256(path: Path) -> str:
    """Compute SHA-256 digest of a file."""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public validation functions
# ---------------------------------------------------------------------------


def validate_upload(filename: str, file_size: int) -> ValidationVerdict:
    """Lightweight validation for the upload endpoint.

    This function does NOT check duplicates because the database is not
    available at upload time. It checks:

    1. Supported extension
    2. Non-empty
    3. Large text-like threshold (auto_split)

    Parameters
    ----------
    filename : str
        The original upload filename.
    file_size : int
        The file size in bytes.

    Returns
    -------
    ValidationVerdict
    """
    file_class = classify_file(filename)

    # 1. Unsupported extension
    if file_class == "unsupported":
        return ValidationVerdict(
            "reject",
            {
                "error_code": "unsupported_extension",
                "file_class": file_class,
                "error_message": "File extension is not supported",
            },
        )

    # 2. Empty file
    if file_size == 0:
        return ValidationVerdict(
            "reject",
            {
                "error_code": "empty_file",
                "file_class": file_class,
                "error_message": "File is empty (0 bytes)",
            },
        )

    # 3. Large text-like → auto_split
    if file_class == "text_like" and file_size >= AUTO_SPLIT_THRESHOLD:
        return ValidationVerdict(
            "auto_split",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": AUTO_SPLIT_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes (>= {AUTO_SPLIT_THRESHOLD} "
                    "threshold); flagging for auto-split to avoid timeout"
                ),
            },
        )

    # 4. Large-ish text-like → warn
    if file_class == "text_like" and file_size >= WARN_SIZE_THRESHOLD:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": WARN_SIZE_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes; may process slowly"
                ),
            },
        )

    # 5. Everything else → accept
    return ValidationVerdict(
        "accept",
        {
            "file_class": file_class,
        },
    )


def validate_file(
    path: Path,
    *,
    file_size: int | None = None,
    sha256: str | None = None,
    duplicate_of: dict[str, Any] | None = None,
) -> ValidationVerdict:
    """Full validation for the worker pre-submission gate.

    Checks extension, emptiness, duplicates, size, and encoding.

    Parameters
    ----------
    path : Path
        Absolute or relative path to the file.
    file_size : int, optional
        Pre-computed file size. If not provided, computed from ``path``.
    sha256 : str, optional
        Pre-computed SHA-256 digest. If not provided, computed from ``path``.

    Returns
    -------
    ValidationVerdict
    """
    filename = path.name

    # Compute size and hash if not provided
    if file_size is None:
        file_size = path.stat().st_size
    if sha256 is None:
        sha256 = _compute_sha256(path)

    file_class = classify_file(filename)

    # 1. Unsupported extension
    if file_class == "unsupported":
        return ValidationVerdict(
            "reject",
            {
                "error_code": "unsupported_extension",
                "file_class": file_class,
                "error_message": "File extension is not supported",
            },
        )

    # 2. Empty file
    if file_size == 0:
        return ValidationVerdict(
            "reject",
            {
                "error_code": "empty_file",
                "file_class": file_class,
                "error_message": "File is empty (0 bytes)",
            },
        )

    # 3. Duplicate content (explicit caller-provided context)
    if duplicate_of is not None:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "duplicate_content",
                "file_class": file_class,
                "sha256": sha256,
                "duplicate_of": duplicate_of,
                "error_message": "Duplicate content detected (same SHA-256 as existing document)",
            },
        )

    # 4. Large text-like → auto_split
    if file_class == "text_like" and file_size >= AUTO_SPLIT_THRESHOLD:
        return ValidationVerdict(
            "auto_split",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": AUTO_SPLIT_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes (>= {AUTO_SPLIT_THRESHOLD} "
                    "threshold); flagging for auto-split to avoid timeout"
                ),
            },
        )

    # 5. Large-ish text-like → warn
    if file_class == "text_like" and file_size >= WARN_SIZE_THRESHOLD:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": WARN_SIZE_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes; may process slowly"
                ),
            },
        )

    # 6. Check encoding for text-like files
    if file_class == "text_like":
        raw = path.read_bytes()
        encoding = _check_encoding(raw)
        if encoding is not None and encoding not in ("utf-8", "utf-8-sig"):
            return ValidationVerdict(
                "warn",
                {
                    "error_code": "fallback_encoding",
                    "file_class": file_class,
                    "encoding": encoding,
                    "error_message": (
                        f"File decoded with fallback encoding {encoding}; "
                        "original encoding may not be UTF-8"
                    ),
                },
            )

    # 7. Everything else → accept
    return ValidationVerdict(
        "accept",
        {
            "file_class": file_class,
            "sha256": sha256,
        },
    )


# ---------------------------------------------------------------------------
# Encoding helper (reuses the same decoding order as worker.py)
# ---------------------------------------------------------------------------

_DECODE_ORDER: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def _check_encoding(raw: bytes) -> str | None:
    """Return the encoding used to decode *raw*, or None if it decoded cleanly as UTF-8.

    Returns None when the first encoding (utf-8) succeeds, indicating no
    fallback was needed. Otherwise returns the encoding that succeeded.
    """
    for encoding in _DECODE_ORDER:
        try:
            raw.decode(encoding)
            if encoding in ("utf-8", "utf-8-sig"):
                return None  # Clean UTF-8; no warning needed
            return encoding
        except UnicodeDecodeError:
            continue
    # All encodings failed - decode with replacement (last resort)
    return "utf-8"  # pragma: no cover
