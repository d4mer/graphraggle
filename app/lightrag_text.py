"""What LightRAG does to text on the way in, imitated so our cuts can survive it.

Hermes read the v1.5.7 source in the container: ``lightrag.py:1921`` passes the whole
document through ``sanitize_text_for_encoding`` BEFORE any splitting, and
``strip_control_characters`` removes the C0 separators - including the ``\\x1e`` we used as
the piece delimiter. Every multi-piece document therefore arrived as ONE segment, and under
``split_by_character_only`` LightRAG either accepted it whole or raised
``ChunkTokenLimitExceededError``. A check at the API boundary ("the delimiter survives the
JSON body") was true and useless: the damage happens later, inside the pipeline.

So two things live here. ``normalise_for_lightrag`` removes, at plan time, exactly the
characters the pipeline will remove anyway - which makes "the text we planned" and "the text
that gets stored" the same string. And ``sanitize_like_lightrag`` + ``pieces_survive`` let
the worker replay that step before submitting, so a delimiter or a piece that cannot survive
it is a recorded fallback rather than a silent re-cut.

The imitation is built from the documented behaviour of that image (C0 controls except tab
and newline, DEL, lone surrogates, zero-width and format characters, CRLF folding, and a
strip of the whole field), not from its source, which is not in this sandbox. If the image
changes, the worker's pre-submit replay is what notices.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Sequence

__all__ = ["SURVIVABLE_DELIMITER", "is_conservative_marker", "is_survivable_delimiter",
           "normalise_for_lightrag", "sanitize_like_lightrag", "pieces_survive",
           "stripped_characters"]

# C0 controls the pipeline drops. Tab (\t) and newline (\n) are NOT in that set; \r is
# folded rather than dropped, and \x0b / \x0c (vertical tab, form feed) ARE dropped -
# a form feed is what pdftotext and pypdf leave behind at a page break.
_DROP = set(range(0x00, 0x09)) | set(range(0x0B, 0x0D)) | set(range(0x0E, 0x20)) | {0x7F}
# Zero-width and format characters: invisible in a stored chunk, so a piece boundary or a
# table cell can change meaning without changing what anybody can see.
_FORMAT = {0x00AD, 0x034F, 0x061C, 0x180E, 0x200B, 0x200C, 0x200D, 0x200E, 0x200F,
           0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2061, 0x2062, 0x2063,
           0x2064, 0x206A, 0x206B, 0x206C, 0x206D, 0x206E, 0x206F, 0xFEFF, 0xFFF9,
           0xFFFA, 0xFFFB}
_DROP_CODEPOINTS = {code for code in _DROP | _FORMAT}
_LONE_SURROGATE = range(0xD800, 0xE000)
_SURROGATE_RE = re.compile("[\ud800-\udfff]")

# A delimiter must survive the sanitiser, NFKC, whitespace collapsing at its edges, a strip
# of the whole field, and anything that treats markup as suspicious. That leaves a small
# alphabet: newline plus ASCII letters, '<', '>', '-'.
SURVIVABLE_DELIMITER = re.compile(r"\A(?:\n|[A-Za-z0-9<>-])+\Z")


def _translate(text: str) -> str:
    table = {code: None for code in _DROP_CODEPOINTS}
    return text.translate(table)


def normalise_for_lightrag(text: str) -> str:
    """Remove what the pipeline removes, without touching the edges of the document.

    Used at plan time so the plan is stated over the text that will actually be stored:
    headers, table rows and turn labels keep the same characters before and after the
    request, and a piece's token estimate cannot be describing text that no longer exists.
    """
    if not text:
        return text
    folded = text.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _translate(folded)
    cleaned = _SURROGATE_RE.sub("", cleaned)
    return cleaned


def sanitize_like_lightrag(text: str) -> str:
    """The pipeline's own step, imitated: same removals, CRLF folded, whole field stripped.

    This is the function the tests and the worker's pre-submit replay call. It is
    deliberately stricter than ``normalise_for_lightrag`` in one way only: it strips the
    ends, which is what a stored field does to a chunk.
    """
    if not text:
        return text
    return normalise_for_lightrag(text).strip()


def is_survivable_delimiter(delimiter: str) -> bool:
    """True when a delimiter reaches LightRAG unchanged.

    The test is integrity, not taste: nothing in it may be a character the sanitiser drops,
    it may not be whitespace-only, and it must be identical after the pipeline's normalising
    pass. A delimiter that fails arrives as nothing, and a whole document becomes one
    segment - which is what ``\\x1e`` did to eight of ten documents in the stack2 run.
    """
    if not delimiter or not delimiter.strip():
        return False
    return normalise_for_lightrag(delimiter) == delimiter


def is_conservative_marker(delimiter: str) -> bool:
    """True for a marker built only from characters nothing in the pipeline can touch.

    Newline plus ASCII letters, digits, ``<``, ``>``, ``-``: not a control character, not markup a
    scrubber would remove, not whitespace a collapse could merge. OUR candidates obey this;
    an explicit delimiter a caller chose only has to be survivable.
    """
    if not is_survivable_delimiter(delimiter):
        return False
    return bool(SURVIVABLE_DELIMITER.match(delimiter))


def stripped_characters(text: str) -> dict[str, int]:
    """Which characters the pipeline would take out of ``text``, and how many of each.

    The report the brief asked for: a form feed from a page break is invisible in the
    output and moves where a piece appears to end, so it has to be visible somewhere.
    """
    counts: Counter[str] = Counter()
    lone_cr = text.count("\r") - text.count("\r\n")
    if lone_cr:
        counts["U+000D lone carriage return (folded, not dropped)"] = lone_cr
    for char in text:
        if char in "\n\t":
            continue
        if _translate(char) == "":
            counts[f"U+{ord(char):04X}"] += 1
    return dict(counts)


def pieces_survive(payload: str, delimiter: str,
                   rendered_pieces: Sequence[str]) -> tuple[bool, str]:
    """Replay the pipeline: sanitise the payload, split on the delimiter, compare.

    ``(True, "")`` means the segments LightRAG will cut are exactly the pieces we planned,
    in the same order, with the same text once each side is stripped. ``(False, why)`` says
    what changed, in counts only - no text, because this reaches the state row.
    """
    if not is_survivable_delimiter(delimiter):
        return False, ("delimiter contains characters LightRAG strips or reorders "
                       "(only newline plus ASCII letters, digits, '<', '>', '-' are known safe)")
    sanitised = sanitize_like_lightrag(payload)
    segments = [segment.strip() for segment in sanitised.split(delimiter) if segment.strip()]
    expected = [piece.strip() for piece in rendered_pieces]
    if len(segments) != len(expected):
        return False, (f"sanitiser replay cut the payload into {len(segments)} segments for "
                       f"{len(expected)} planned pieces")
    differing = [index for index, (got, want) in enumerate(zip(segments, expected))
                 if got != want]
    if differing:
        return False, (f"piece {differing[0]} changes under the sanitiser "
                       f"({len(differing)} of {len(expected)} pieces differ)")
    return True, ""
