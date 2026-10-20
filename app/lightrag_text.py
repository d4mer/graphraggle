"""What LightRAG does to text on the way in, imitated so our cuts can survive it.

Hermes read the v1.5.7 source in the container: ``lightrag.py:1921`` passes the whole
document through ``sanitize_text_for_encoding`` (``lightrag/utils.py:5529-5566``) BEFORE any
splitting. It deletes the C0 separators - including the ``\\x1e`` we used as the piece
delimiter - so every multi-piece document arrived as ONE segment, and under
``split_by_character_only`` LightRAG either accepted it whole or raised
``ChunkTokenLimitExceededError``. A check at the API boundary ("the delimiter survives the
JSON body") was true and useless: the damage happens later, inside the pipeline.

This module is now a PARITY implementation, not an imitation built from documentation. The
real function is five steps, in this order, and the order is what makes it behave oddly:

1. ``text.strip()``                                  - the whole field, once
2. ``html.unescape(text)``                           - exactly ONE pass: ``&amp;lt;`` -> ``&lt;``
3. remove ``[\\uD800-\\uDFFF\\uFFFE\\uFFFF]``          - lone surrogates and two non-characters
4. remove ``[\\x00-\\x08\\x0B\\x0C\\x0E-\\x1F\\x7F]``      - C0 except \\t \\n \\r, plus DEL
5. ``text.strip()``

There is no Unicode normalisation, no whitespace collapse, NO CR folding (``\\r`` is KEPT),
and every zero-width/format character (``\\u00AD``, ``200B``-``200F``, ``2060``, ``202A``-``202E``,
``2066``-``2069``, ``FEFF``, ``FFF9`` ...) is KEPT. ``tests/fixtures/sancmp_tests.json`` and
``sancmp_real.json`` are a 62-string golden fixture produced by running the real function
inside lightrag-server2; ``tests/test_sanitiser_parity.py`` asserts all 62. That fixture is
the oracle - if the image changes, re-run it, do not adjust this file from memory.

Two consequences shape the rest of the module. Step 2 can SYNTHESISE a delimiter out of
entities (``&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;`` is not a marker until the image
unescapes it), so the delimiter has be chosen against the unescaped text as well, and
``delimiter_unescape_hazard`` says when a payload would change shape on the way in. And
because step 2 must run exactly ONCE - in the image - the plan-time normaliser applies the
DELETIONS only, never the unescape, and never the field strip.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from typing import Sequence

__all__ = ["SURVIVABLE_DELIMITER", "delimiter_unescape_hazard", "entity_expansions",
           "first_unescape_hazard",
           "is_conservative_marker", "is_survivable_delimiter", "normalise_for_lightrag",
           "pieces_survive", "sanitize_like_lightrag", "stripped_characters",
           "unescape_like_lightrag"]

# Step 3 of the real function: lone surrogates and the two non-characters it happens to
# name. A lone surrogate cannot be encoded, which is why it is gone rather than replaced.
_STEP3 = re.compile(r"[\ud800-\udfff\ufffe\uffff]")
# Step 4: C0 except \t (09), \n (0A) and \r (0D), plus DEL. \x0b (vertical tab) and \x0c
# (form feed, what a PDF page break leaves behind) ARE deleted. \x1c-\x1f - including the
# \x1e we used - are deleted, and Python calls them whitespace, which is how the old
# "not whitespace only" guard let them through.
_STEP4 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_REMOVED = re.compile(r"[\ud800-\udfff\ufffe\uffff\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# An entity LightRAG's html.unescape will expand. Used only for reporting; the hazard check
# compares real unescape output, not this pattern.
_ENTITY_RE = re.compile(r"&(?:#[0-9]{1,8};|#[xX][0-9a-fA-F]{1,7};|[a-zA-Z][a-zA-Z0-9]{1,31};)")

# A marker has to survive the deletions, the unescape pass, and a strip of the whole field.
# Newline plus ASCII letters, digits, '<', '>', '-' does all three. It does NOT on its own
# cover synthesis from entities, which is what delimiter_unescape_hazard is for.
SURVIVABLE_DELIMITER = re.compile(r"\A(?:\n|[A-Za-z0-9<>-])+\Z")


def unescape_like_lightrag(text: str) -> str:
    """Step 2 alone: ``html.unescape``, ONE pass (``&amp;lt;`` -> ``&lt;``, not ``<``)."""
    return html.unescape(text)


def _remove_invisible(text: str) -> str:
    return _STEP4.sub("", _STEP3.sub("", text))


def normalise_for_lightrag(text: str) -> str:
    """The pipeline's deletions (steps 3-4), applied to the document BEFORE planning.

    Planning over the deleted-character-free text is what keeps "the piece we measured" and
    "the chunk that gets stored" one string: a form feed cannot sit in a piece, vanish on the
    way in, and move where the piece reads as ending.

    Two things this deliberately does NOT do. It does not ``html.unescape`` - step 2 runs
    exactly once, in the image, and unescaping here would mean the image unescaping twice
    (``&amp;lt;`` would reach the reader as ``<``). And it does not strip the ends or fold
    CR: the real function strips the whole field once and keeps every ``\\r``.
    """
    if not text:
        return text
    return _remove_invisible(text)


def sanitize_like_lightrag(text: str) -> str:
    """The real ``sanitize_text_for_encoding``, in its order: strip, unescape, remove, strip.

    Parity with the image is asserted against a 62-string golden fixture, not against this
    docstring. Note the sequence effects: an entity can produce a character step 4 then
    deletes (``&#x1e;`` -> U+001E -> nothing), and an entity can produce whitespace that the
    final strip removes (a payload ending in ``&nbsp;``).
    """
    if not text:
        return text
    return _remove_invisible(html.unescape(text.strip())).strip()


def is_survivable_delimiter(delimiter: str) -> bool:
    """True when a delimiter reaches LightRAG unchanged by the real function.

    The test is integrity, not taste: it may not be whitespace-only, and the sanitiser must
    leave it identical apart from the field strip that necessarily eats its edges. A delimiter
    that fails arrives as nothing and the whole document becomes one segment - which is what
    ``\\x1e`` did to eight of ten documents in the stack2 run. Format characters such as
    ``\\ufeff`` are NOT stripped by the real function, so they are survivable and simply
    refused by :func:`is_conservative_marker` instead.
    """
    if not delimiter or not delimiter.strip():
        return False
    return sanitize_like_lightrag(delimiter) == delimiter.strip()


def is_conservative_marker(delimiter: str) -> bool:
    """True for a marker built only from characters nothing in the pipeline can touch.

    Newline plus ASCII letters, digits, ``<``, ``>``, ``-``: not a control character, not a
    surrogate, not whitespace a collapse could merge, and not something ``html.unescape``
    invents out of nothing. OUR candidates obey this; an explicit delimiter a caller chose
    only has to be survivable.
    """
    if not is_survivable_delimiter(delimiter):
        return False
    return bool(SURVIVABLE_DELIMITER.match(delimiter))


def stripped_characters(text: str) -> dict[str, int]:
    """Which characters the pipeline would take out of ``text``, and how many of each.

    The report the brief asked for: a form feed from a page break is invisible in the stored
    chunk and moves where a piece reads as ending, so it has to be visible somewhere. Only
    what the real function actually deletes is reported - CR and the zero-width/format
    characters are kept by it, so they are not reported here.
    """
    counts: Counter[str] = Counter()
    for char in text:
        if _REMOVED.match(char):
            code = ord(char)
            name = f"U+{code:04X}"
            if code >= 0xD800:
                name += " lone surrogate or non-character"
            counts[name] += 1
    return dict(counts)


def entity_expansions(text: str) -> int:
    """How many HTML entities the pipeline will expand. Reporting only: expansion itself is
    the image's business and is never done here. A document whose entities expand is the one
    case where stored text is not the text we planned, character for character."""
    return len(_ENTITY_RE.findall(text)) if text else 0


def delimiter_unescape_hazard(text: str, delimiter: str) -> tuple[int, int]:
    """``(occurrences_before, occurrences_after)`` of the unescape step on one text.

    Step 2 runs before the split, so an entity-encoded marker becomes a real cut in the
    reader: ``&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;`` is one of those. Any change in the
    count is a hazard - up means a cut we never planned, down means a cut we did plan stops
    being one.
    """
    return text.count(delimiter), html.unescape(text).count(delimiter)


def first_unescape_hazard(texts: Sequence[tuple[str, str]],
                          delimiter: str) -> tuple[str, int, int] | None:
    """First ``(label, before, after)`` whose unescaped form changes the delimiter count."""
    for label, text in texts:
        before, after = delimiter_unescape_hazard(text, delimiter)
        if before != after:
            return label, before, after
    return None


def _stored_form(rendered: str, *, first: bool, last: bool) -> str:
    """What LightRAG keeps for one piece inside a payload: unescape, then delete.

    The field strip bites at the payload's own two edges only, and the payload's start is the
    first piece's start while its end is the last piece's end - so the first piece is lstripped,
    the last is rstripped, and nothing in between is touched. Renders are normally already
    stripped, which makes this a no-op except when an entity expands into edge whitespace
    (``&nbsp;``) or a header arrives padded.
    """
    form = _remove_invisible(html.unescape(rendered))
    if first:
        form = form.lstrip()
    if last:
        form = form.rstrip()
    return form


def pieces_survive(payload: str, delimiter: str,
                   rendered_pieces: Sequence[str]) -> tuple[bool, str]:
    """Replay the pipeline: sanitise the payload, split on the delimiter, compare.

    ``(True, "")`` means the segments LightRAG will cut are exactly the pieces we planned,
    in the same order, with the text the image will actually store. ``(False, why)`` says
    what changed, in counts and codepoints only - never text, because this reaches the state
    row.
    """
    if not is_survivable_delimiter(delimiter):
        return False, ("delimiter contains characters LightRAG deletes (only newline plus "
                       "ASCII letters, digits, '<', '>', '-' are known safe)")
    hazard = first_unescape_hazard([("payload", payload)]
                                   + [(f"piece {index}", piece)
                                      for index, piece in enumerate(rendered_pieces)],
                                   delimiter)
    if hazard is not None:
        label, before, after = hazard
        return False, (f"html entities in the {label} change the delimiter count from "
                       f"{before} to {after}: the reader would cut somewhere this plan did not")
    stored = sanitize_like_lightrag(payload)
    segments = stored.split(delimiter)
    expected = [_stored_form(piece, first=index == 0,
                              last=index == len(rendered_pieces) - 1)
                for index, piece in enumerate(rendered_pieces)]
    if len(segments) != len(expected):
        return False, (f"sanitiser replay cut the payload into {len(segments)} segments for "
                       f"{len(expected)} planned pieces")
    # Exact, not stripped: _stored_form already models the one strip the real function
    # applies (the payload's two outer edges), and anything else that moved has moved.
    differing = [index for index, (got, want) in enumerate(zip(segments, expected))
                 if got != want]
    if differing:
        return False, (f"piece {differing[0]} changes under the sanitiser "
                       f"({len(differing)} of {len(expected)} pieces differ)")
    return True, ""
