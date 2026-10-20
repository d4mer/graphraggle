"""Structure-aware pre-chunker: OUR code decides every cut, LightRAG obeys it.

Why this module exists
----------------------
Fixed-token cutting is blind. It splits tables between the header row and the
numbers, cuts a speaker turn in half, and leaves every chunk after the first
without any idea which document or section it came from. The embedder makes the
same demand from the other side: a piece has to fit what the embedder can see, and a
piece that is coherent is worth more than a piece that is a round number.

There is no chunk-size budget here. The only ceiling is the embedder window (BGE-M3:
8192 tokens; this module works in a conservative estimate and is told 7500), and the only
reason to cut a natural unit is that it cannot fit it. Below the floor (default 150) a
unit is welded to its neighbour instead of standing alone, which is what keeps a page of
numbered sub-headings from becoming a hundred fragments. Hermes' real-document run is the
evidence: capping at 350 turned one PDF into 596 pieces with a median of 111 tokens.

LightRAG 1.5.7 cannot be handed a custom chunker through the API, but it WILL cut
exactly where we tell it: ``fixed_token`` accepts ``split_by_character`` +
``split_by_character_only``. So we choose every cut, insert a delimiter there, and
LightRAG obeys. This module produces the pieces; :func:`render_for_lightrag`
returns the text plus the params that carry the instruction.

Contract
--------
1. Nothing is invented. Header fields come from the file-name stem (title, date)
   and from structure already in the text (section path, part index, observed
   speakers). No LLM anywhere, for any field.
2. Words are never changed, only placed. Every boundary falls between words, and
   the pieces' bodies - with inserted headers and repeated table header rows
   removed - concatenate back to the input modulo whitespace. The only text that
   disappears is running furniture (short lines repeated >= 3 times), and that is
   counted in ``stats``, never silent.
3. Every piece fits the budget (body + header). Oversize is impossible by
   construction: a long paragraph splits at sentences then words, a long table
   splits BY ROW with the header row (and its ``---`` rule) repeated on every
   piece, a row longer than the budget splits at cell boundaries with the row key
   repeated, a turn splits at sentences then clauses then words. The one exception
   is a single word longer than the whole budget: it cannot be broken without
   corrupting it, so it stands alone and is reported in ``warnings``.
4. Detection is conservative and reported. A document that is not transcript-,
   table- or heading-shaped is chunked as plain text and says so in
   ``kind_detected``. Nothing is refused - a plan is always returned, and
   :mod:`app.worker` falls back to untouched text if this module raises at all.
5. Pure and deterministic: no I/O, no clock, no randomness, no settings import, so
   ``scripts/overnight_loop.py`` and ``scripts/chunk_embed_bench.py`` benchmark the
   SAME code the worker ships.

Token counting
--------------
LightRAG tokenizes with tiktoken (cl100k_base), which is not installed in this
repo's venv. When tiktoken is importable its count is used; when it is not,
:func:`token_count` deliberately OVER-counts (3 characters per token, floor of one
token per word) so a piece that fits here cannot blow the real limit in the image.
Which counter was used is reported in ``stats["tokenizer"]``, never guessed at.

Delimiter
---------
``split_by_character`` only works if the delimiter cannot occur inside a piece.
:func:`pick_delimiter` returns the first candidate that appears nowhere in the
text; an explicit delimiter that DOES appear raises :class:`StructureChunkerError`
rather than quietly producing a wrong cut.

Rules, in priority order (this docstring is the specification;
tests/test_structure_chunker.py names every one of them)
--------------------------------------------------------
* Transcript (same bars, same parser as :mod:`app.transcript_prep`): cut at turn
  boundaries; short turns group until the budget; a turn longer than the budget
  splits at sentences, then clauses, then words; the header carries meeting / date
  / observed speakers exactly as transcript_prep writes it.
* Headings (``#``, ``1.2.3``, short ALL-CAPS lines) start a piece and feed a
  ``section_path`` that appears in the header.
* Tables (pipe rows, tab-separated rows, columnar runs of two or more spaces,
  markdown tables with a ``---`` rule) stay whole when they fit; otherwise split BY
  ROW with the header row repeated on every piece; a row longer than the budget
  splits at cell boundaries with the row key repeated; a cell is never split
  unless it alone exceeds the budget. Caption lines immediately above or below a
  table travel with it.
* Lists travel with their lead-in line when they fit.
* Running headers/footers and page markers (a short line repeated >= 3 times) are
  dropped from bodies and counted.
* Plain text: paragraphs, then sentences, then words.

Header format: ``[Doc: <title> | <date> | <section path> | Part k/n]``, omitted
fields dropped, never padded, capped at ``header_tokens``; ``headers=False`` turns
it off; ``headers="shuffled"`` is a benchmark placebo that keeps the token cost but
attaches each header to a different piece, so a win can be attributed to the
information rather than to the extra tokens.
"""

from __future__ import annotations

import hashlib
import html
import math
import re
from collections import Counter
from . import topic_segment
from .lightrag_text import (entity_expansions, is_conservative_marker,
                            is_survivable_delimiter, normalise_for_lightrag,
                            pieces_survive, stripped_characters)
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# Reuse ONE transcript parser instead of writing a second one: if this module had
# its own idea of a speaker label, the two would drift and the chunker would start
# cutting turns the preparer never recognised.
from .transcript_prep import (
    _HEADER_RE,
    _MONOLITHIC_LINE_CHARS,
    monolithic_speaker_set,
    speaker_set_from_alternation,
    uniform_label_speaker_set,
    _classify_paren_label,
    _leading_parenthetical,
    _metadata_from_name,
    _parse_turn,
    split_inline_turns,
)

# A conservative over-count. cl100k runs at roughly 4 characters per token for
# prose; 3.6 leaves ~10% headroom there, and the floor of one token per
# whitespace-separated unit covers the shapes the char term under-counts (short
# lines, heavy punctuation, unicode). Measured direction on this repo's fixtures:
# prose over-counts by ~10-15%, pipe tables by up to ~1.7x. That wastes a little
# budget and can never produce an oversize piece, which is the trade wanted.
CHARS_PER_TOKEN = 3.6

# There is no chunk-size budget any more: the embedder's window is the only ceiling, and
# a natural unit (a section, a table, a run of turns) is cut only when it cannot fit it.
# 7500 is BGE-M3's 8192-token window minus a safety margin, measured in THIS module's
# conservative estimate, which over-counts English prose by ~10-15% and therefore errs
# on the small side. ``DEFAULT_BUDGET_TOKENS`` survives as an alias for callers that pass
# an explicit cap (the offline benchmark compares capped arms on purpose).
DEFAULT_MAX_PIECE_TOKENS = 7500
DEFAULT_MIN_PIECE_TOKENS = 150
# "floor": a chain of small units stops as soon as it clears the floor, so a piece stays a
# natural unit (the lead's call, 2026-10-18: welding unrelated short sections into one
# piece makes a mixed-topic piece, which dilutes the vector it is embedded into).
# "window" lets a chain run to the window; kept as an opt-in knob and a benchmark arm.
DEFAULT_MERGE_UNTIL = "floor"
DEFAULT_BUDGET_TOKENS = DEFAULT_MAX_PIECE_TOKENS
DEFAULT_HEADER_TOKENS = 25

# A short line seen this many times is document furniture, not content.
RUNNING_LINE_REPEATS = 3
_MAX_RUNNING_LINE_CHARS = 90
_MAX_RUNNING_LINE_WORDS = 12

# Delimiter candidates, safest first. Each can stand on its own line without being
# mistaken for content, and - the rule that cost the stack2 run eight documents - every
# candidate is PRINTABLE. LightRAG sanitises the whole document before it splits anything
# (sanitize_text_for_encoding -> strip_control_characters), which deletes the C0 separators
# \x1c-\x1f including \x1e, so an ASCII-record-separator delimiter silently turned every
# multi-piece document into one segment. Only newline plus ASCII letters, digits, '<', '>',
# '-' survive sanitising, NFKC, a whitespace collapse at the edges, and a strip of the field.
DELIMITER_CANDIDATES: tuple[str, ...] = (
    "\n\n<<<CHUNK-BREAK>>>\n\n",
    "\n\n<<<CHUNK-BREAK-HERE>>>\n\n",
    "\n\n<<<GRAGGLE-PIECE-BOUNDARY>>>\n\n",
    "\n\n<<<PIECE-DONE-NEXT-PIECE>>>\n\n",
)
# If every candidate is present in the text (they will not be, but the picker must not
# guess) the escalation is deterministic and printable too: a document-hash suffix, so the
# same input always gets the same delimiter and two runs of one file stay comparable.
_HASH_DELIMITER_LENGTHS: tuple[int, ...] = (8, 12, 16, 24, 32, 40, 64)

_TOKENIZER: tuple[str, Callable[[str], int]] | None = None


class StructureChunkerError(ValueError):
    """Raised when a safe plan could not be built (bad delimiter, no text)."""


# ── token counting ─────────────────────────────────────────────────────────

def _overcount_tokens(text: str) -> int:
    if not text:
        return 0
    units = len(text.split())          # one token per whitespace-separated unit
    by_chars = int(math.ceil(len(text) / CHARS_PER_TOKEN))
    return max(by_chars, units, 1)


def _tokenizer_pair() -> tuple[str, Callable[[str], int]]:
    """``(name, counter)``: tiktoken when installed, otherwise the over-count."""
    global _TOKENIZER
    if _TOKENIZER is not None:
        return _TOKENIZER
    try:                                    # pragma: no cover - absent in this venv
        import tiktoken                     # noqa: PLC0415 - optional dependency

        encoding = tiktoken.get_encoding("cl100k_base")

        def count(text: str) -> int:
            return len(encoding.encode(text))

        _TOKENIZER = ("tiktoken:cl100k_base", count)
    except Exception:                       # pragma: no cover - the shipped path
        _TOKENIZER = ("conservative-overcount", _overcount_tokens)
    return _TOKENIZER


def token_count(text: str) -> int:
    """Token count used for every budget decision in this module."""
    if not text:
        return 0
    return _tokenizer_pair()[1](text)


def tokenizer_name() -> str:
    """Which counter was used - reported in stats so a reader can price the number."""
    return _tokenizer_pair()[0]


# ── results ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Piece:
    """One chunk as this module means it: header line, body, and why it is cut here."""

    body: str
    header: str | None
    kind: str                       # turn | section | table | list | paragraph
    section_path: tuple[str, ...] = ()
    token_estimate: int = 0         # body + header: the number the budget applies to
    part_index: int = 1             # 1-based position within the plan
    part_total: int = 1

    def render(self) -> str:
        return f"{self.header}\n{self.body}" if self.header else self.body


@dataclass(frozen=True)
class ChunkPlan:
    """Every cut decision for one document. Safe to submit as-is."""

    pieces: list[Piece]
    kind_detected: str              # transcript | table | structured | plain
    warnings: tuple[str, ...] = ()
    stats: dict[str, Any] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.pieces


# ── line classification ────────────────────────────────────────────────────

_HEADING_MD = re.compile(r"^#{1,6}[ \t]*\S")
# A numbered HEADING is "3.1 Freight costs" or "3. Approval". It is NOT "12 months ago we
# signed it", and it is not the columnar row "2019 1240 3300". A dotted number is a
# section number by shape alone; a bare "3." only counts when the line is short, is not a
# sentence, is not table-shaped, and carries more letters than digits.
_HEADING_NUMBERED_DOTTED = re.compile(r"^\d{1,3}(?:\.\d{1,3})+(?:[.)][ \t]|[ \t])\S")
_HEADING_NUMBERED_BARE = re.compile(r"^\d{1,3}[.)][ \t]+\S")
_HEADING_BARE_TITLE = re.compile(r"^\d{1,3}[.)][ \t]+[A-Z\u00c0-\u0178]")
_PAGE_MARK = re.compile(r"^\s*(?:page[ \t]*\d+|\d{1,4}|[ivxlcdm]{1,5})\s*$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*\u2022\u00b7]|\(?\d{1,3}[.)])\s+\S")
_TERMINAL = re.compile(r"[.!?\u2026]$")
_CLAUSE = re.compile(r"[,;:\u2014\u2013]$")
_CAPTION = re.compile(r"^\s*(?:table|figure|fig\.|exhibit|ex\.)[ \t]*[\divx]+[:.)\s]",
                      re.IGNORECASE)


def _is_all_caps_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > 60 or _TERMINAL.match(stripped):
        return False
    words = stripped.split()
    if not 1 <= len(words) <= 6:
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if len(letters) < 2:
        return False
    return stripped == stripped.upper()


def _is_numbered_heading(line: str) -> bool:
    """A numbered section title, and not a sentence or a data row that starts with a digit."""
    stripped = line.strip()
    if not stripped or _is_table_line(stripped) or _is_table_separator(stripped):
        return False
    dotted = bool(_HEADING_NUMBERED_DOTTED.match(stripped))
    bare = bool(_HEADING_NUMBERED_BARE.match(stripped))
    if not (dotted or bare):
        return False
    if _TERMINAL.search(stripped) or len(stripped.split()) > 12:
        return False
    letters = sum(1 for ch in stripped if ch.isalpha())
    digits = sum(1 for ch in stripped if ch.isdigit())
    if letters < digits:
        return False
    return dotted or bool(_HEADING_BARE_TITLE.match(stripped))


def _is_heading(line: str) -> bool:
    return bool(_HEADING_MD.match(line) or _is_numbered_heading(line)
                or _is_all_caps_heading(line))


def _heading_text(line: str) -> str:
    text = re.sub(r"^#{1,6}[ \t]*", "", line).strip()
    text = re.sub(r"^\d{1,3}(?:\.\d{1,3}){0,4}[.)]?[ \t]+", "", text)
    return " ".join(text.split())[:80]


def _heading_level(line: str) -> int:
    match = re.match(r"^(#{1,6})[ \t]*", line)
    if match:
        return len(match.group(1))
    match = re.match(r"^(\d{1,3}(?:\.\d{1,3}){0,4})[.)]?[ \t]", line)
    if match:
        return match.group(1).count(".") + 1
    return 1


def _is_table_line(line: str) -> bool:
    if line.strip().startswith("|") and line.count("|") >= 2:
        return True
    if "\t" in line and line.strip():
        return True
    # Columnar: two or more spaces between non-space runs, at least twice.
    return len(re.findall(r"\S[ \t]{2,}\S", line)) >= 2


def _is_table_separator(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(re.fullmatch(r"\|[ \t:|-]{3,}\|?", stripped)) or bool(
        re.fullmatch(r"[-\u2013\u2014]{3,}(?:[ \t]+[-\u2013\u2014]{3,})+", stripped))


def _is_caption(line: str) -> bool:
    return bool(_CAPTION.match(line))


def _content_words(text: str) -> list[str]:
    """Whitespace units that carry a letter or a digit.

    Repeat and drop accounting uses these, not raw whitespace units: re-rendering a
    table row from its cells changes how many pipe characters there are, and a pipe
    is not content. Content words are what the coverage invariant promises to keep.
    """
    return [word for word in text.split() if any(ch.isalnum() for ch in word)]


def _running_key(line: str) -> str:
    return " ".join(line.split()).lower()


# ── splitting helpers (never mid-word) ─────────────────────────────────────

def _join(words: Iterable[str]) -> str:
    return " ".join(words)


def _pack(words: Sequence[str], budget: int) -> list[str]:
    """Whole words into bodies of at most ``budget`` tokens. Never breaks a word."""
    out: list[str] = []
    current: list[str] = []
    for word in words:
        if current and token_count(_join(current + [word])) > budget:
            out.append(_join(current))
            current = []
        current.append(word)
    if current:
        out.append(_join(current))
    return out


def _split_run(text: str, budget: int, *, clause_fallback: bool = False) -> list[str]:
    """Sentences first, then clauses, then words. Every boundary is between words.

    A sentence-bounded piece that is itself over budget goes back through the same
    function at a finer grain, so an oversized run cannot survive by having no
    punctuation. The final resort is :func:`_pack`, which breaks at whitespace.
    """
    words = text.split()
    if not words:
        return []
    if token_count(_join(words)) <= budget:
        return [_join(words)]
    sentences: list[list[str]] = []
    current: list[str] = []
    for word in words:
        current.append(word)
        # search, not match: the terminator is at the END of the word. With .match this
        # branch only ever fired on a bare "." token, so every real paragraph arrived as
        # one "sentence" and an over-budget block was cut by whitespace inside a sentence.
        if _TERMINAL.search(word) and token_count(_join(current)) >= budget * 0.5:
            sentences.append(current)
            current = []
    if current:
        sentences.append(current)
    pieces: list[str] = []
    for sentence in sentences:
        joined = _join(sentence)
        if token_count(joined) <= budget:
            pieces.append(joined)
        elif len(sentence) == 1:
            pieces.append(joined)          # one unsplittable word; reported upstream
        elif clause_fallback:
            pieces.extend(_split_clause(sentence, budget))
        else:
            pieces.extend(_split_run(joined, budget, clause_fallback=True))
    return [piece for piece in pieces if piece]


def _split_oversize(text: str, window: int, stats: "_Stats",
                    warnings: list[str], *,
                    floor: int = DEFAULT_MIN_PIECE_TOKENS) -> list[str]:
    """Split a body that cannot fit the window, aiming for EQUAL pieces, never mid-word.

    The shape this exists for is one 9,000-token paragraph or a 20,000-token single line.
    Greedy packing would emit a full piece and a stub; equal pieces retrieve better and
    keep every piece under the window. Boundaries are tried in the same order everywhere
    else in this module: paragraph, sentence, clause, whitespace. A single word longer
    than the window cannot be broken without inventing text, so it stays and is reported.
    """
    total = token_count(text)
    if total <= int(window):
        return [text]
    stats.bump("oversized_units_split")
    # The window is the budget _split_run is given, because its sentence rule already
    # lands pieces just above half the budget: asking it for less produces smaller pieces
    # without producing more of them, and a smaller piece is a quote that no longer fits
    # inside one chunk. Balance is restored at the tail, below.
    raw = _split_run(text, int(window), clause_fallback=True)
    if len(raw) <= 1:
        warnings.append("unit-over-window(one-unsplittable-word)")
        stats.bump("units_unsplittable")
        return raw or [text]
    # A tail leftover is a stub, not a piece: a 90-token chunk retrieves badly and is a
    # fifth of the context its neighbour already has. Weld it sideways while the
    # neighbour still fits, which is the same reasoning as the merge floor.
    welded: list[str] = []
    stub = max(64, int(window) // 8)
    for piece in raw:
        if (welded and token_count(piece) < stub
                and token_count(welded[-1]) + token_count(piece) <= int(window)):
            welded[-1] = _join(welded[-1].split() + piece.split())
            stats.bump("oversized_stub_welds")
        else:
            welded.append(piece)
    welded = _rebalance_parts(welded, int(window), stats, floor)
    out: list[str] = []
    for piece in welded:
        if token_count(piece) > int(window):
            out.extend(_split_oversize(piece, window, stats, warnings, floor=floor))
        else:
            out.append(piece)
    return out


def _sentence_atoms(text: str) -> list[str]:
    """Sentences, for redistribution. Falls back to single words if nothing terminates."""
    atoms: list[str] = []
    current: list[str] = []
    for word in text.split():
        current.append(word)
        if _TERMINAL.search(word):
            atoms.append(_join(current))
            current = []
    if current:
        atoms.extend(current)
    return atoms or text.split()


def _rebalance_parts(parts: list[str], window: int, stats: "_Stats",
                     floor: int = DEFAULT_MIN_PIECE_TOKENS) -> list[str]:
    """The same number of pieces, each roughly the same size.

    Greedy packing fills to the window and dumps whatever is left, which is how a
    monolithic turn came out 13,500 / 13,500 / 3,700 characters: two pieces a reader can
    use and one orphan. Redistribute the sentences across the SAME count so every piece
    takes its share: boundaries are placed at the atom nearest each k-th of the total, so
    the count is a promise, not an outcome. It only fires when some part is under half the
    mean, and it runs AFTER the tail weld, so a document that already splits evenly keeps
    exactly the pieces it had - the chunkrun7 build must not move because of this.
    """
    if len(parts) < 3:
        return parts
    sizes = [token_count(part) for part in parts]
    total = sum(sizes)
    mean = total / len(parts)
    # Two different complaints. A part at or above the merge floor that is much smaller
    # than its neighbours is a badly drawn boundary: equalise it. A part BELOW the floor is
    # the stub question, and the document-level rule answers that one (weld it into a
    # neighbour, or leave it and say so) - equalising there would push every piece well
    # under the window and cost capacity that retrieval needs.
    if min(sizes) >= 0.5 * mean or min(sizes) < max(1, int(floor)):
        return parts
    atoms: list[str] = []
    for part in parts:
        atoms.extend(_sentence_atoms(part))
    if len(atoms) < len(parts):
        return parts
    costs = [token_count(atom) for atom in atoms]
    prefix = [0]
    for cost in costs:
        prefix.append(prefix[-1] + cost)
    total = prefix[-1]
    target = len(parts)
    cuts: list[int] = []
    for step in range(1, target):
        want = step * total / target
        index = min(range(1, len(atoms)), key=lambda at: abs(prefix[at] - want))
        if cuts:
            index = max(index, cuts[-1] + 1)
        if index >= len(atoms):
            break
        cuts.append(index)
    bounds = [0] + cuts + [len(atoms)]
    groups = [atoms[low:high] for low, high in zip(bounds, bounds[1:])]
    if any(sum(costs[at] for at in range(low, high)) > int(window)
           for low, high in zip(bounds, bounds[1:])):
        return parts                      # the window outranks balance; let the caller recurse
    stats.bump("oversized_pieces_rebalanced")
    return [_join(group) for group in groups if group]


def _merge_stub_pieces(units: list[tuple[str, str, tuple[str, ...]]], floor: int,
                       window: int, stats: "_Stats", warnings: list[str]
                       ) -> list[tuple[str, str, tuple[str, ...]]]:
    """The merge floor applied to the finished document, not just inside one unit.

    A trailing "(Ana) the note was read twice." is a 26-token turn standing beside a
    3,700-token neighbour: it retrieves badly, and its own context header costs more than
    its content. Weld it into the previous piece (a document reads forward), or the next
    one when there is no previous. When both joins would break the window the piece stays
    alone and the row says so - silently dropping text is not an option.
    """
    if floor <= 0 or len(units) < 2:
        return list(units)
    out = list(units)
    changed = True
    while changed:
        changed = False
        for index in range(len(out)):
            kind, body, _path = out[index]
            if token_count(body) >= floor:
                continue
            neighbours = [index - 1] if index > 0 else []
            if index + 1 < len(out):
                neighbours.append(index + 1)
            for target in neighbours:
                first, second = ((out[target], out[index]) if target < index
                                 else (out[index], out[target]))
                merged_body = f"{first[1]}\n{second[1]}"
                if token_count(merged_body) > int(window):
                    continue
                # "paragraph" is the neutral label, so the other kind wins a tie.
                merged_kind = (first[0] if first[0] == second[0]
                               else (second[0] if first[0] == "paragraph" else first[0]))
                out[target] = (merged_kind, merged_body, first[2])
                del out[index]
                stats.bump("stub_pieces_merged")
                changed = True
                break
            else:
                warnings.append("stub-piece-cannot-merge")
                stats.bump("stub_pieces_left")
            if changed:
                break
    return out


def _split_clause(sentence: Sequence[str], budget: int) -> list[str]:
    clauses: list[list[str]] = []
    inner: list[str] = []
    for word in sentence:
        inner.append(word)
        if _CLAUSE.match(word) and token_count(_join(inner)) >= budget * 0.5:
            clauses.append(inner)
            inner = []
    if inner:
        clauses.append(inner)
    pieces: list[str] = []
    for clause in clauses:
        joined = _join(clause)
        if token_count(joined) <= budget:
            pieces.append(joined)
        else:
            pieces.extend(_pack(clause, budget))
    return pieces


def _table_cells(line: str) -> list[str]:
    if line.strip().startswith("|"):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return cells if len(cells) > 1 else [line.strip()]
    if "\t" in line:
        return [cell.strip() for cell in line.split("\t")]
    parts = re.split(r"[ \t]{2,}", line.strip())
    return [part.strip() for part in parts if part.strip()] or [line.strip()]


def _render_cells(cells: Sequence[str], shape: str) -> str:
    """Re-join cells in the shape the source used, so nothing is restyled."""
    if shape == "pipe":
        return "| " + " | ".join(cells) + " |"
    if shape == "tab":
        return "\t".join(cells)
    return "  ".join(cells)


def _line_shape(line: str) -> str:
    if line.strip().startswith("|"):
        return "pipe"
    if "\t" in line:
        return "tab"
    return "columns"


# ── the planner ────────────────────────────────────────────────────────────

@dataclass
class _Block:
    kind: str                       # heading | table | list | para
    lines: list[str]
    path: tuple[str, ...]
    caption: str | None = None


class _Stats:
    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()

    def bump(self, name: str, amount: int = 1) -> None:
        self.counts[name] += amount


# ── running furniture ──────────────────────────────────────────────────────

def _furniture_candidate(line: str) -> bool:
    """Could this line be running furniture at all, before the repetition evidence?

    Repetition alone is not enough, because legitimate text repeats: a transcript
    says "(Alice) Got it." twenty times, a slogan runs down a page, a checklist
    reuses its items. What running headers and footers actually look like is a
    SHORT FRAGMENT with no sentence punctuation - "Acme Corp confidential", "Page
    3", "12", "Draft - internal" - so a line that reads as a sentence, or that the
    transcript parser claims as a turn, is never furniture no matter how often it
    recurs. A line that does not even have letters (a rule, a number) is only
    dropped when it matches the page-marker shape.
    """
    stripped = line.strip()
    if not stripped:
        return False
    if _TERMINAL.search(stripped):
        return False
    if _parse_turn(stripped) is not None:
        return False
    if _leading_parenthetical(stripped) is not None:
        return False
    # Structure is never furniture. A repeated heading or list item is the document
    # saying the same thing twice (a slide dump, a repeated section title); deleting it
    # would delete a section path. Only a prose-ish fragment can be a running header.
    if _is_heading(stripped) or _BULLET.match(stripped):
        return False
    letters = sum(1 for ch in stripped if ch.isalpha())
    if letters == 0:
        return bool(_PAGE_MARK.match(stripped))
    return True


def _drop_running_lines(lines: Sequence[str], stats: _Stats) -> list[str]:
    """Remove short fragment lines repeated >= RUNNING_LINE_REPEATS times.

    Two pieces of evidence are required, not one: the line has to look like
    furniture (:func:`_furniture_candidate`) AND it has to repeat. Table lines are
    exempt because a table repeats its header row on purpose and eating one would
    be data loss.
    """
    keys: Counter[str] = Counter()
    for line in lines:
        stripped = line.strip()
        if not stripped or len(stripped) > _MAX_RUNNING_LINE_CHARS:
            continue
        if len(stripped.split()) > _MAX_RUNNING_LINE_WORDS:
            continue
        if _is_table_line(line) or _is_table_separator(line):
            continue
        if not _furniture_candidate(line):
            continue
        keys[_running_key(line)] += 1
    repeated = {key for key, count in keys.items() if count >= RUNNING_LINE_REPEATS}
    if not repeated:
        return list(lines)
    kept: list[str] = []
    for line in lines:
        if line.strip() and _running_key(line) in repeated:
            stats.bump("dropped_running_lines")
            stats.bump("dropped_running_words", len(_content_words(line)))
            if _PAGE_MARK.match(line):
                stats.bump("dropped_page_markers")
            continue
        kept.append(line)
    return kept


# ── block scan ─────────────────────────────────────────────────────────────

def _scan_blocks(lines: Sequence[str], stats: _Stats) -> list[_Block]:
    """Line list -> blocks, with the section path each block sits under.

    Headings update the path (level N replaces position N and truncates deeper
    levels) and are emitted as their own block, so a piece that starts under a
    heading can say which one.
    """
    blocks: list[_Block] = []
    path: list[tuple[int, str]] = []
    index = 0
    total = len(lines)

    def current_path() -> tuple[str, ...]:
        return tuple(title for _level, title in path)

    while index < total:
        line = lines[index]
        stripped = line.strip()
        if not stripped:
            index += 1
            continue
        if _is_heading(line):
            level = _heading_level(line)
            title = _heading_text(line)
            path = [entry for entry in path if entry[0] < level]
            path.append((level, title))
            blocks.append(_Block(kind="heading", lines=[line.rstrip()], path=current_path()))
            index += 1
            continue
        if _is_table_line(line) or _is_table_separator(line):
            run: list[str] = []
            while index < total and (
                    _is_table_line(lines[index]) or _is_table_separator(lines[index])):
                run.append(lines[index].rstrip())
                index += 1
            if len(run) >= 2 or any(_is_table_separator(item) for item in run):
                stats.bump("tables")
                block = _Block(kind="table", lines=run, path=current_path())
                # A caption directly above or below travels with the table: a
                # number grid without its title is a table nobody can interpret.
                if blocks and blocks[-1].kind == "para" and _is_caption(blocks[-1].lines[-1]):
                    block.caption = blocks.pop().lines[-1]
                if index < total and _is_caption(lines[index]):
                    block.caption = (block.caption + "\n" if block.caption else "") + lines[index].strip()
                    index += 1
                blocks.append(block)
                continue
            # A lone columnar line is prose with wide spacing, not a table.
            blocks.append(_Block(kind="para", lines=run, path=current_path()))
            continue
        if _BULLET.match(line):
            run = []
            while index < total and (_BULLET.match(lines[index])
                                     or (lines[index].startswith(("  ", "\t")) and run)):
                run.append(lines[index].rstrip())
                index += 1
            if not run:                              # same progress guarantee
                run = [line.rstrip()]
                index += 1
            blocks.append(_Block(kind="list", lines=run, path=current_path()))
            continue
        # Paragraph: everything up to the next blank line, heading, table or list.
        run = []
        while index < total:
            candidate = lines[index]
            if (not candidate.strip() or _is_heading(candidate) or _BULLET.match(candidate)
                    or _is_table_line(candidate) or _is_table_separator(candidate)):
                break
            run.append(candidate.rstrip())
            index += 1
        if not run:
            # No branch claimed this line, so take it as prose and move on. Without
            # this the scan would not advance and a document made of a shape none of
            # the branches recognises would hang the ingest instead of failing it.
            # (Found by sabotage: disable the heading branch and this loop never
            # terminates on "# Heading".)
            run = [lines[index].rstrip()]
            index += 1
        blocks.append(_Block(kind="para", lines=run, path=current_path()))
    return blocks


# ── headers ────────────────────────────────────────────────────────────────

def _compose_header(*, doc: str | None, date: str | None, path: Sequence[str],
                    part: int, total: int, meeting: str | None = None,
                    speakers: Sequence[str] | None = None, max_speakers: int = 8,
                    header_budget: int, warnings: list[str]) -> str:
    """Build ``[Doc: … | Date: … | Section: … | Part k/n]``, shrinking to fit.

    Nothing is invented and nothing is padded: an absent field is simply not there.
    Drop order is least useful first - the deepest section component, then the rest
    of the section path, then the date, then the speaker list (shortened before it
    is dropped), and the document title last, because "which document is this from"
    is the reason the header exists. ``[Part k/n]`` always fits, so a header is
    never empty.
    """
    speaker_list = list(speakers or ())

    def render(path_parts: Sequence[str], speaker_count: int | None,
               keep_date: bool, keep_doc: bool) -> str:
        fields: list[str] = []
        if keep_doc and meeting:
            fields.append(f"Meeting: {meeting}")
        elif keep_doc and doc:
            fields.append(f"Doc: {doc}")
        if keep_date and date:
            fields.append(f"Date: {date}")
        if path_parts:
            fields.append(f"Section: {' / '.join(path_parts)}")
        if speaker_list and speaker_count:
            shown = speaker_list[:speaker_count]
            extra = len(speaker_list) - len(shown)
            fields.append("Speakers: " + ", ".join(shown)
                          + (f", +{extra} more" if extra > 0 else ""))
        fields.append(f"Part {part}/{total}")
        return "[" + " | ".join(fields) + "]"

    ladder: list[tuple[tuple[str, ...], int | None, bool, bool]] = []
    parts = list(path)
    while len(parts) > 1:
        parts.pop()
        ladder.append((tuple(parts), max_speakers, True, True))
    ladder.append(((), max_speakers, True, True))
    ladder.append(((), max_speakers, False, True))
    for speaker_count in ([3, 1] if speaker_list else []):
        ladder.append(((), speaker_count, False, True))
    ladder.append(((), max_speakers if speaker_list else None, False, False))
    header = render(tuple(path), max_speakers, True, True)
    if header_budget <= 0 or token_count(header) <= header_budget:
        return header
    for step, (path_parts, speaker_count, keep_date, keep_doc) in enumerate(ladder):
        header = render(path_parts, speaker_count, keep_date, keep_doc)
        if token_count(header) <= header_budget:
            if step:
                warnings.append("header-fields-dropped-to-fit")
            return header
    warnings.append("header-minimised")
    return f"[Part {part}/{total}]"


def _shuffle_headers(pieces: list["Piece"], *, seed_shift: int, stats: _Stats) -> None:
    """Placebo: same token cost, header attached to the WRONG piece.

    A benchmark win has to beat this, not merely beat nothing: if the gain came
    from the extra tokens rather than the information, a shuffled header is
    equally good. Deterministic rotation only - no randomness, no clock.
    """
    if not pieces:
        return
    shift = seed_shift % len(pieces)
    headers = [piece.header for piece in pieces]
    for index, piece in enumerate(pieces):
        source = (index + shift) % len(headers)
        header = headers[source]
        if header and re.search(r"Part \d+/\d+", header):
            header = re.sub(r"Part \d+/\d+", f"Part {index + 1}/{len(pieces)}", header)
        object.__setattr__(piece, "header", header)
        object.__setattr__(piece, "token_estimate",
                           token_count(piece.render()))
    stats.bump("headers_shuffled")


# ── tables ─────────────────────────────────────────────────────────────────

_NUMERICISH = re.compile(r"^[\d$%\u2013-][\d.,:$%\u2013\- ]*$")


def _header_rows(rows: Sequence[str], stats: _Stats) -> list[str]:
    """Which leading rows must be repeated on every piece of a split table.

    A markdown separator line settles it. Without one, the first row is the header
    only when it reads like a header - words where the data rows have numbers -
    because repeating a data row would put a false label on every piece.
    """
    if len(rows) >= 2 and _is_table_separator(rows[1]):
        return [rows[0], rows[1]]
    if len(rows) >= 2:
        first = _table_cells(rows[0])
        rest = [_table_cells(row) for row in rows[1:]]
        first_numeric = sum(1 for cell in first if _NUMERICISH.match(cell))
        rest_numeric = sum(1 for row in rest for cell in row if _NUMERICISH.match(cell))
        if first_numeric == 0 and rest_numeric > 0:
            return [rows[0]]
    stats.bump("tables_without_header_row")
    return []


def _split_row(row: str, budget: int, headers: Sequence[str], stats: _Stats,
               warnings: list[str]) -> list[str]:
    """One row too long for the budget: split at CELL boundaries, row key repeated.

    A cell is only ever split when it alone exceeds the budget, and then by words,
    never mid-word. Every continuation carries the header rows and the row's key
    (its first cell), so a number stranded on its own piece still says which row
    and which column it belongs to.
    """
    cells = _table_cells(row)
    shape = _line_shape(row)
    key = cells[0] if cells else row.strip()
    overhead_rows = list(headers) + ([key] if len(cells) > 1 else [])
    overhead = token_count("\n".join(overhead_rows)) if overhead_rows else 0
    room = max(16, budget - overhead)
    if len(cells) == 1:
        pieces = _split_run(cells[0], room)
        if any(token_count(piece) > budget for piece in pieces):
            warnings.append("cell-over-budget")
        stats.bump("table_cells_split")
        return [_render_cells([piece], shape) for piece in pieces] or [row.strip()]
    groups: list[list[str]] = []
    current: list[str] = []
    for cell in cells[1:]:
        candidate = current + [cell]
        if current and token_count(_render_cells([key] + candidate, shape)) > room:
            groups.append(current)
            current = []
        current.append(cell)
    if current:
        groups.append(current)
    if not groups:
        groups = [cells[1:]]
    rows: list[str] = []
    for group in groups:
        rendered = _render_cells([key] + group, shape)
        if token_count(rendered) <= room:
            rows.append(rendered)
            continue
        # One cell is bigger than the budget on its own. It has to break, and it
        # breaks by words - never mid-word - with the row key repeated, because a
        # fragment of a cell with no key is meaningless.
        stats.bump("table_cells_split")
        inner_room = max(16, room - len(key.split()) - 4)
        for part in _split_run(" ".join(group), inner_room):
            rows.append(_render_cells([key, part], shape))
    if not rows:
        rows = [_render_cells([key] + (groups[-1] if groups else []), shape)]
    if len(rows) > 1:
        # Only the row key is charged here: the header rows these pieces carry are
        # charged once per extra piece by the caller, which sees every piece.
        stats.bump("repeated_table_row_keys", len(rows) - 1)
        stats.bump("repeated_words", (len(rows) - 1) * len(_content_words(key)))
    out: list[str] = [("\n".join(list(headers) + [row]) if headers else row)
                      for row in rows]
    if out and len(groups) > 1:
        # The FIRST group carries the key legitimately (it is the row); the others
        # are repeats, counted where they are written below.
        pass
    if any(token_count(piece) > budget for piece in out):
        warnings.append("row-over-budget-after-cell-split")
        stats.bump("table_rows_over_budget")
    return out


def _table_bodies(block: _Block, budget: int, stats: _Stats, warnings: list[str],
                  *, lead: str = "") -> list[str]:
    """A table block as bodies that each fit the budget, whole table if it can.

    ``lead`` is the section heading the table sits under: it rides on the FIRST body,
    and its cost is counted against that body, so a heading is never pushed out of the
    piece it introduces by a table that fills the window.
    """
    rows = [line for line in block.lines if line.strip()]
    lead = lead.strip()
    if not rows:
        return [lead] if lead else []
    caption = block.caption.strip() if block.caption else ""
    whole = "\n".join(([lead] if lead else []) + ([caption] if caption else []) + list(rows))
    if token_count(whole) <= budget:
        return [whole]
    headers = _header_rows(rows, stats)
    data = rows[len(headers):] if headers else rows
    stats.bump("tables_split_by_row")
    prefix = "\n".join(headers)
    prefix_cost = token_count(prefix) if headers else 0
    # The caption rides on the FIRST piece only: it names the table, and a repeat
    # of it would be counted content twice for no gain in retrieval.
    first_overhead = (prefix_cost + (token_count(caption) if caption else 0)
                      + (token_count(lead) if lead else 0))
    later_overhead = prefix_cost
    bodies: list[str] = []
    current: list[str] = []

    def overhead() -> int:
        return first_overhead if not bodies else later_overhead

    def flush() -> None:
        if not current:
            return
        lines = ([lead] if not bodies and lead else []) + (
            [caption] if not bodies and caption else []) + list(headers) + current
        bodies.append("\n".join(lines))
        current.clear()

    for row in data:
        # A row that does not fit ALONGSIDE the repeated header rows is split at
        # cell boundaries: keeping the header is not optional, so the row gives.
        if token_count(row) + overhead() > budget:
            flush()
            room = max(16, (first_overhead if not bodies else later_overhead)
                       and budget - (first_overhead if not bodies else later_overhead))
            for piece in _split_row(row, room, headers, stats, warnings):
                bodies.append(piece)
            continue
        if current and token_count("\n".join(current + [row])) + overhead() > budget:
            flush()
        current.append(row)
    flush()
    if headers and len(bodies) > 1:
        # Every piece of a split table carries the header rows; only the first of
        # them is the original, the rest are repeats made so a number is never
        # stranded away from its column names.
        stats.bump("repeated_table_header_rows", len(headers) * (len(bodies) - 1))
        stats.bump("repeated_words",
                   sum(len(_content_words(row)) for row in headers) * (len(bodies) - 1))
    for body in bodies:
        if token_count(body) > budget:
            warnings.append("table-piece-over-budget(unsplittable-cell)")
            stats.bump("table_pieces_over_budget")
    return bodies


# ── packing blocks into pieces ─────────────────────────────────────────────

def _block_text(block: _Block) -> str:
    """A block as text, caption included: it names the table and must not vanish."""
    parts = [block.caption.strip()] if block.caption and block.caption.strip() else []
    parts += [line for line in block.lines if line.strip()]
    return "\n".join(parts)


def _block_fits(block: _Block, window: int) -> bool:
    """Can this block be handed to a piece whole?"""
    if block.kind == "table":
        # A table counts as "fitting" when its ROWS can be laid out inside the window
        # with the header repeated: the rows are the boundary, not the table.
        return bool(block.lines) and max(
            (token_count(line) for line in block.lines), default=0) <= window
    text = _block_text(block)
    return bool(text) and token_count(text) <= window


@dataclass
class _Unit:
    """One natural unit: a heading and everything nested under it."""
    heading: _Block | None
    body_blocks: list[_Block]
    level: int
    path: tuple[str, ...]

    @property
    def blocks(self) -> list[_Block]:
        return ([self.heading] if self.heading else []) + self.body_blocks

    def token_estimate(self) -> int:
        return sum(token_count(_block_text(block)) for block in self.blocks)


def _build_units(blocks: Sequence[_Block]) -> list[_Unit]:
    """Flat block list -> natural units, one per top-level heading, plus front matter.

    A sub-heading stays INSIDE its parent's unit rather than opening one. That is what
    makes the split order meaningful: if the whole unit fits the window nothing is cut
    at all, and when it does not, the first place it is cut is its shallowest
    sub-heading.
    """
    units: list[_Unit] = []
    current: _Unit | None = None
    front: _Unit | None = None
    for block in blocks:
        if block.kind == "heading":
            level = _heading_level(block.lines[0])
            if current is not None and level > current.level:
                current.body_blocks.append(block)
                continue
            current = _Unit(heading=block, body_blocks=[], level=level, path=block.path)
            units.append(current)
            front = None
            continue
        if current is not None:
            current.body_blocks.append(block)
            continue
        if front is None or front.path != block.path:
            front = _Unit(heading=None, body_blocks=[block], level=0, path=block.path)
            units.append(front)
        else:
            front.body_blocks.append(block)
    return units


def _split_unit(unit: _Unit, window: int, stats: _Stats, warnings: list[str], *,
                depth: int = 0, floor: int = DEFAULT_MIN_PIECE_TOKENS
                ) -> list[tuple[str, str, tuple[str, ...]]]:
    """One natural unit -> (kind, body, path) pieces that each fit the window.

    Cut order, and the order matters: the unit whole, its shallowest sub-headings,
    paragraph and list blocks, table rows (header repeated), and only then a
    sentence boundary inside one block. Nothing is ever cut by token count alone.
    """
    if depth > 24:
        warnings.append("unit-nesting-too-deep")
        return []
    estimate = unit.token_estimate()
    if estimate <= window and all(_block_fits(block, window) for block in unit.blocks):
        return _unit_bodies_whole(unit, stats, warnings)

    children = [block for block in unit.body_blocks if block.kind == "heading"]
    if children:
        shallowest = min(_heading_level(block.lines[0]) for block in children)
        stats.bump("units_split_at_subheadings")
        groups: list[_Unit] = []

        def start(path: tuple[str, ...], level: int, heading: _Block | None) -> None:
            groups.append(_Unit(heading=heading, body_blocks=[], level=level, path=path))

        start(unit.path, unit.level, unit.heading)
        for block in unit.body_blocks:
            if block.kind == "heading" and _heading_level(block.lines[0]) == shallowest:
                start(block.path, _heading_level(block.lines[0]), block)
                continue
            groups[-1].body_blocks.append(block)
        out: list[tuple[str, str, tuple[str, ...]]] = []
        for group in groups:
            if group.heading is None and not group.body_blocks:
                continue
            out.extend(_split_unit(group, window, stats, warnings, depth=depth + 1,
                                   floor=floor))
        return out

    return _split_unit_blocks(unit, window, stats, warnings, floor=floor)


def _unit_bodies_whole(unit: _Unit, stats: _Stats,
                       warnings: list[str]) -> list[tuple[str, str, tuple[str, ...]]]:
    """The unit fits the window, so it is ONE piece: nothing inside it is cut."""
    parts: list[str] = []
    kind = "paragraph"
    for block in unit.blocks:
        text = _block_text(block)
        if not text:
            continue
        if block.kind == "heading":
            kind = "section"
        elif block.kind == "list" and kind == "paragraph":
            kind = "list"
        elif block.kind == "table" and kind == "paragraph":
            kind = "table"
        parts.append(text)
    body = "\n".join(parts)
    if not body.strip():
        return []
    stats.bump("units_whole")
    return [(kind, body, unit.path)]


def _split_unit_blocks(unit: _Unit, window: int, stats: _Stats,
                       warnings: list[str], *,
                       floor: int = DEFAULT_MIN_PIECE_TOKENS
                       ) -> list[tuple[str, str, tuple[str, ...]]]:
    """No sub-headings to use: pack blocks into pieces at their own boundaries."""
    out: list[tuple[str, str, tuple[str, ...]]] = []
    current: list[str] = []
    kind = "paragraph"

    def flush() -> None:
        nonlocal kind
        if not current:
            return
        body = "\n".join(current)
        if body.strip():
            out.append((kind or "paragraph", body, unit.path))
        current.clear()
        kind = "paragraph"

    for block in unit.blocks:
        if block.kind == "table":
            # A heading already pending rides on the table's first body rather than being
            # flushed out as a piece of its own.
            lead = "\n".join(current)
            current.clear()
            for body in _table_bodies(block, window, stats, warnings, lead=lead):
                out.append(("table", body, unit.path))
            continue
        text = _block_text(block)
        if not text:
            continue
        if block.kind == "heading":
            flush()
            current.append(text)
            kind = "section"
            continue
        if block.kind == "list" and kind in ("", "paragraph"):
            kind = "list"
        if current and token_count("\n".join(current + [text])) > window:
            stats.bump("blocks_split_at_paragraphs")
            flush()
        if token_count(text) > window:
            stats.bump("blocks_split_at_sentences")
            for part in _split_oversize(text, window, stats, warnings, floor=floor):
                if token_count(part) > window:
                    warnings.append("piece-over-budget(one-unsplittable-block)")
                    stats.bump("pieces_over_budget")
                out.append((block.kind, part, unit.path))
            continue
        current.append(text)
    flush()
    return out


def _merge_small_pieces(pieces: list[tuple[str, str, tuple[str, ...]]], floor: int,
                        window: int, stats: _Stats, *, until: str = DEFAULT_MERGE_UNTIL
                        ) -> list[tuple[str, str, tuple[str, ...]]]:
    """A floor, not a cap: everything below ``floor`` is welded to its neighbour.

    The window is the only ceiling. This is what turns a page of numbered
    sub-headings into a handful of readable pieces instead of a hundred fragments,
    without ever deciding a piece should be small. Two table bodies are never
    welded together: each already carries the repeated header rows, and a piece
    with the same header twice is worse than two pieces.
    """
    if floor <= 0 or not pieces:
        return pieces
    merged = [pieces[0]]
    for kind, body, path in pieces[1:]:
        prev_kind, prev_body, prev_path = merged[-1]
        prev_cost = token_count(prev_body)
        cost = token_count(body)
        if prev_kind == "table" and kind == "table":
            merged.append((kind, body, path))
            continue
        # until="floor"  : a chain stops as soon as it clears the floor (small siblings
        #                  pair up and the document's own units keep their shape).
        # until="window" : a chain only stops at the window, so a run of small sections
        #                  becomes a few large pieces instead of hundreds of fragments.
        if until == "floor":
            # Stop the chain the moment it clears the floor: small siblings pair up, and a
            # unit that already stands on its own keeps its own shape.
            worth_merging = prev_cost < floor
        else:
            worth_merging = prev_cost < floor or cost < floor
        if worth_merging and prev_cost + 1 + cost <= window:
            joined_kind = "section" if "section" in (prev_kind, kind) else prev_kind
            merged[-1] = (joined_kind, prev_body + "\n" + body,
                          prev_path if len(prev_path) >= len(path) else path)
            stats.bump("pieces_merged_up_to_floor")
            continue
        merged.append((kind, body, path))
    return merged


def _pack_units(units: Sequence[_Unit], window: int, floor: int, stats: _Stats,
                warnings: list[str], *, until: str = DEFAULT_MERGE_UNTIL
                ) -> list[tuple[str, str, tuple[str, ...]]]:
    """Natural units first, merge floor last. The window only ever forces a cut."""
    pieces: list[tuple[str, str, tuple[str, ...]]] = []
    for unit in units:
        pieces.extend(_split_unit(unit, window, stats, warnings, floor=floor))
    stats.bump("natural_units", len(units))
    return _merge_small_pieces(pieces, floor, window, stats, until=until)


# ── transcript path ────────────────────────────────────────────────────────

@dataclass
class _TurnLines:
    speaker: str | None
    lines: list[str] = field(default_factory=list)


def _scan_turns(lines: Sequence[str]) -> tuple:
    """Turn-shaped scan: who speaks, and the ORIGINAL lines of each turn.

    The lines are kept verbatim rather than rebuilt from parsed words, so the
    speaker labels and any cue lines inside a turn survive the trip through the
    chunker - the pieces have to account for every word in the document.

    A monolithic minutes line ("Ana: ... Liang: ... Ana: ...") is split into its turns
    BEFORE anything is counted, so detection counts labels by FREQUENCY rather than by
    how the file happens to be wrapped. The segments are exact substrings of the source,
    so no word is invented, changed or lost.
    """
    turns: list[_TurnLines] = []
    label_counts: Counter[str] = Counter()
    label_lines = 0
    cue_lines = 0
    long_labelled = 0
    # "(They)" and "(You)" are ordinary words a name grammar must refuse, and in prose it
    # is right to refuse them. What makes them speakers is behaviour, and behaviour is a
    # property of the file: two labels heading the lines, strictly alternating, or two
    # enormous labelled turns and nothing else. Both passes are per-document, and stage
    # markers are refused by both.
    uniform_turns = uniform_label_speaker_set(lines)
    accepted = (speaker_set_from_alternation(lines) | monolithic_speaker_set(lines)
                | uniform_turns)
    for raw in lines:
        source = raw.rstrip()
        if not source.strip():
            continue
        if "-->" in source and re.search(r"\d{1,2}:\d{2}", source):
            cue_lines += 1
        for line in (split_inline_turns(source) or [source]):
            parsed = _parse_turn(line, accepted)
            if parsed is not None:
                speaker, _body, _style = parsed
                label_lines += 1
                label_counts[speaker.lower()] += 1
                if len(line) >= _MONOLITHIC_LINE_CHARS:
                    long_labelled += 1
                turns.append(_TurnLines(speaker=speaker, lines=[line]))
                continue
            candidate = _leading_parenthetical(line)
            if candidate is not None and _classify_paren_label(
                    candidate[0], accepted)[1] == "unparseable":
                # Its own turn, no speaker claimed - transcript_prep's rule, for its
                # reason: guessing a speaker is worse than claiming none.
                turns.append(_TurnLines(speaker=None, lines=[line]))
                continue
            if turns:
                turns[-1].lines.append(line)
            else:
                turns.append(_TurnLines(speaker=None, lines=[line]))
    return turns, label_counts, label_lines, cue_lines, long_labelled, bool(uniform_turns)


def _transcript_like(turns: Sequence[_TurnLines], label_counts: Counter[str],
                     label_lines: int, cue_lines: int, nonempty: int,
                     long_labelled: int = 0, uniform_ok: bool = False) -> bool:
    """The same bars as :func:`app.transcript_prep.prepare_transcript`, plus one.

    Shared bars, deliberately: a document the preparer will not touch must not be
    chunked as a transcript here either, or the two modules disagree about what the
    corpus contains. The extra bar is the monolithic shape - two or three lines, each
    one whole turn of thousands of tokens. The preparer rewrites a document it accepts,
    and rewriting a 9,000-token turn is not something to do on this evidence; the
    chunker only chooses boundaries, and a labelled turn of any length has a boundary at
    its head and can be split inside, which an unlabelled block cannot.
    """
    if not turns:
        return False
    distinct = len(label_counts)
    repeated = any(count >= 2 for count in label_counts.values())
    labelled = (label_lines >= 3 and distinct >= 2 and repeated
                and label_lines >= 0.20 * nonempty)
    single_voice = (distinct == 1 and max(label_counts.values(), default=0) >= 4
                    and label_lines >= 0.5 * nonempty)
    captioned = (cue_lines >= 3 and (label_lines + cue_lines) >= 0.30 * nonempty
                 and (label_lines >= 2 or nonempty >= 6))
    monolithic = (label_lines >= 2 and distinct >= 2
                  and long_labelled >= 2 and long_labelled == label_lines)
    # Every line a labelled turn, with substance and a name-shaped label. That rule lives
    # in transcript_prep.uniform_label_speaker_set, which already read these same lines; the
    # bar here only checks that the chunker's own turn scan agrees with it. Two 6,000-token
    # turns become a meeting; "(Intro)" / "(Scope)" / "(Method)" does not.
    uniform = uniform_ok and label_lines >= 2 and distinct >= 2 and label_lines == nonempty
    return bool(labelled or single_voice or captioned or monolithic or uniform)


def _transcript_units(turns: Sequence[_TurnLines], budget: int, floor: int,
                      stats: _Stats, warnings: list[str], *, until: str = DEFAULT_MERGE_UNTIL
                      ) -> list[str]:
    """Bodies cut at turn boundaries, an oversized turn split finer. Never mid-word."""
    bodies: list[str] = []
    current: list[str] = []
    for turn in turns:
        body = "\n".join(turn.lines)
        if not body.strip():
            continue
        if token_count(body) <= budget:
            if current and token_count("\n".join(current + [body])) > budget:
                bodies.append("\n".join(current))
                current = []
            current.append(body)
            continue
        if current:
            bodies.append("\n".join(current))
            current = []
        stats.bump("oversized_turns_split")
        parts = _split_oversize(body, budget, stats, warnings, floor=floor)
        for part in parts:
            if token_count(part) > budget:
                warnings.append("turn-over-budget(one-unsplittable-word)")
                stats.bump("turn_pieces_over_budget")
            bodies.append(part)
    if current:
        bodies.append("\n".join(current))
    merged = _merge_small_pieces([("turn", body, ()) for body in bodies],
                                 min(floor, budget), budget, stats, until=until)
    return [body for _kind, body, _path in merged]


# ── public entry points ────────────────────────────────────────────────────

def _topic_stats(stats: _Stats, report: Any) -> None:
    """Record HOW the topic cuts were found. A fallback cut and a topic cut are different
    facts, and a reader of the state row has to be able to tell them apart."""
    stats.counts["topic_method"] = report.method
    for key in ("blocks", "boundaries", "embedded", "splits_over_window",
                "merges_under_floor"):
        stats.counts[f"topic_{key}"] = getattr(report, key, 0)
    stats.counts["topic_coarsened"] = int(bool(getattr(report, "coarsened", False)))


def chunk_document(text: str, *, filename: str = "",
                   max_piece_tokens: int | None = None,
                   min_piece_tokens: int | None = None,
                   budget_tokens: int | None = None, until_merge: str = DEFAULT_MERGE_UNTIL,
                   header_tokens: int = DEFAULT_HEADER_TOKENS, kind: str = "auto",
                   headers: Any = True,
                   topic_embed: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
                   topic_options: dict[str, Any] | None = None) -> ChunkPlan:
    """Plan every cut in one document. Deterministic; nothing is invented.

    ``max_piece_tokens`` is the window: the ONLY ceiling, and the only reason to cut.
    ``min_piece_tokens`` is the floor: a unit smaller than this is welded to its
    neighbour, which is what keeps a page of sub-headings from becoming a hundred
    fragments. ``budget_tokens`` is the old name for the ceiling, kept for the
    benchmark's capped arms; when both are given ``budget_tokens`` wins.
    ``until_merge``: ``"floor"`` (default) stops a merge chain as soon as the piece clears
    the floor, so a piece stays one natural unit; ``"window"`` lets the chain run to the
    window. A parent section whose whole extent fits the window is ONE unit either way.
    ``kind``: ``auto`` detects, or force ``transcript`` / ``table`` / ``structured``
    / ``plain``. ``headers``: ``True`` for the deterministic context header,
    ``False`` for none, ``"shuffled"`` for the benchmark placebo (same tokens, wrong
    attachment). The ceiling always covers body AND header.
    """
    if kind not in ("auto", "transcript", "table", "structured", "plain"):
        raise StructureChunkerError(f"unknown kind {kind!r}")
    ceiling = (int(budget_tokens) if budget_tokens is not None
               else int(DEFAULT_MAX_PIECE_TOKENS if max_piece_tokens is None
                        else max_piece_tokens))
    budget = max(32, ceiling)
    floor = int(DEFAULT_MIN_PIECE_TOKENS if min_piece_tokens is None else min_piece_tokens)
    floor = max(0, min(floor, budget))
    merge_until = str(until_merge) if until_merge else DEFAULT_MERGE_UNTIL
    if merge_until not in ("window", "floor"):
        raise StructureChunkerError('until_merge must be "window" or "floor"')
    header_budget = max(0, int(header_tokens))
    if not text or not text.strip():
        return ChunkPlan(pieces=[], kind_detected="plain", warnings=("empty-input",),
                         stats={"tokenizer": tokenizer_name(), "budget_tokens": budget,
                                "pieces": 0})

    stats = _Stats()
    warnings: list[str] = []
    # LightRAG sanitises the whole document BEFORE it splits: the C0 controls (except tab
    # and newline), DEL, lone surrogates and zero-width/format characters are gone, and CRLF
    # becomes LF. Planning over that same text is what keeps "the piece we measured" and
    # "the chunk that gets stored" one and the same string - a form feed from a page break
    # otherwise sits in a piece, disappears on the way in, and moves where the piece reads as
    # ending. Counted, never silent.
    cleaned = normalise_for_lightrag(text)
    if cleaned != text:
        report = stripped_characters(text)
        for name, count in report.items():
            # One counter per character: the report the brief asked for. A form feed from a
            # page break is invisible in the stored text and moves where a piece reads as
            # ending, so which characters went, and how many of each, has to be recoverable
            # from the state row.
            stats.bump(f"sanitiser_stripped:{name}", count)
        stats.bump("sanitiser_chars_removed", sum(report.values()) or len(text) - len(cleaned))
        text = cleaned
    entities = entity_expansions(text)
    if entities:
        # LightRAG expands these on the way in (one pass), so the stored chunk is not the text
        # we counted, character for character. Counted here so a reader can price the gap; the
        # delimiter pick and the pre-submit replay are what keep it from inventing a cut.
        stats.bump("html_entities", entities)
    raw_lines = [line for line in text.splitlines() if not _HEADER_RE.match(line.rstrip())]
    stats.bump("lines_in", len([line for line in text.splitlines() if line.strip()]))
    if len(raw_lines) != len(text.splitlines()):
        stats.bump("dropped_prior_headers")

    units: list[tuple[str, str, tuple[str, ...]]] = []
    window = budget
    kind_detected = "plain"
    speakers: list[str] = []
    title, date = _metadata_from_name(filename)

    if kind in ("auto", "transcript"):
        turns, label_counts, label_lines, cue_lines, long_labelled, uniform_ok = (
            _scan_turns(raw_lines))
        nonempty = len([line for line in raw_lines if line.strip()])
        if _transcript_like(turns, label_counts, label_lines, cue_lines, nonempty,
                            long_labelled, uniform_ok):
            # Only a label that heads at least two turns is claimed as a voice: a
            # one-off label ("Meeting:", "Note:") is not a speaker, and writing it
            # into the header of every piece would be a claim this file does not
            # support.
            seen: set[str] = set()
            for turn in turns:
                key = (turn.speaker or "").lower()
                if turn.speaker and key not in seen and label_counts[key] >= 2:
                    seen.add(key)
                    speakers.append(turn.speaker)
            kind_detected = "transcript"
            probe = _compose_header(doc=title, date=date, path=(), part=1, total=1,
                                    meeting=title, speakers=speakers,
                                    header_budget=header_budget, warnings=[])
            reserve = token_count(probe) if headers else 0
            window = max(32, budget - reserve)
            if topic_embed is not None:
                turn_units = ["\n".join(turn.lines) for turn in turns
                              if any(line.strip() for line in turn.lines)]
                bodies, report = topic_segment.segment_bodies(
                    turn_units, tokens=token_count, window=window,
                    floor=min(floor, window), embed=topic_embed,
                    **(topic_options or {}))
                _topic_stats(stats, report)
                stats.bump("oversized_turns_split", report.splits_over_window)
                # A turn bigger than the window still cannot be sent whole.
                bodies = [part for body in bodies
                          for part in ([body] if token_count(body) <= window
                                       else _split_oversize(body, window, stats, warnings,
                                                              floor=floor))]
                for body in bodies:
                    if token_count(body) > window:
                        warnings.append("turn-over-budget(one-unsplittable-word)")
                        stats.bump("turn_pieces_over_budget")
            else:
                bodies = _transcript_units(turns, window, floor, stats, warnings,
                                           until=merge_until)
            units = [("turn", body, ()) for body in bodies]
        elif kind == "transcript":
            warnings.append("transcript-not-detected")
    if not units:
        kept = _drop_running_lines(raw_lines, stats)
        blocks = _scan_blocks(kept, stats)
        probe_path = max((block.path for block in blocks), key=len, default=())
        probe = _compose_header(doc=title, date=date, path=probe_path, part=1, total=1,
                                header_budget=header_budget, warnings=[])
        reserve = token_count(probe) if headers else 0
        window = max(32, budget - reserve)
        natural = _build_units(blocks)
        if topic_embed is not None and all(unit.heading is None for unit in natural):
            # No headings at all: the only boundaries available are topic shifts.
            flat = [text for unit in natural for block in unit.body_blocks
                    if (text := _block_text(block))]
            bodies, report = topic_segment.segment_bodies(
                flat, tokens=token_count, window=window, floor=min(floor, window),
                embed=topic_embed, **(topic_options or {}))
            _topic_stats(stats, report)
            # One unit that is itself over the window gives the topic pass nothing to cut
            # BETWEEN, so it comes back whole. The transcript path has always split such a
            # turn inside itself; the flat path needs the same guard or a long paragraph
            # falls out as one over-window piece and the document falls back.
            bodies = [part for body in bodies
                      for part in _split_oversize(body, window, stats, warnings,
                                                   floor=floor)]
            # No headings means no section path exists to claim.
            units = [("paragraph", body, ()) for body in bodies]
        else:
            units = _pack_units(natural, window, floor, stats, warnings,
                                until=merge_until)
        table_tokens = sum(token_count(body) for kind_name, body, _p in units if kind_name == "table")
        all_tokens = sum(token_count(body) for _k, body, _p in units) or 1
        if table_tokens >= 40 and table_tokens >= 0.4 * all_tokens:
            kind_detected = "table"
        elif any(kind_name == "section" for kind_name, _b, _p in units):
            kind_detected = "structured"

    # The invariant, in one place and for every kind: no unit leaves this function
    # larger than the ceiling. A document must never fall back to LightRAG's default
    # chunker because our own piece was too big - that is the difference between a worse
    # cut and no cut at all. Anything still over after splitting is one unsplittable
    # word, and that is recorded rather than hidden.
    absolute = max(32, budget)
    guarded: list[tuple[str, str, tuple[str, ...]]] = []
    for unit_kind, body, path in units:
        if token_count(body) > absolute:
            warnings.append("unit-over-window(split)")
            guarded.extend((unit_kind, part, path)
                           for part in _split_oversize(body, absolute, stats, warnings,
                                                        floor=floor))
        else:
            guarded.append((unit_kind, body, path))
    units = guarded
    units = _merge_stub_pieces(units, floor, window, stats, warnings)

    pieces: list[Piece] = []
    for unit_kind, body, path in units:
        if not body.strip():
            continue
        pieces.append(Piece(body=body.strip("\n"), header=None, kind=unit_kind,
                            section_path=tuple(path), token_estimate=token_count(body)))
    if not pieces:
        return ChunkPlan(pieces=[], kind_detected=kind_detected,
                         warnings=tuple(sorted(warnings) + ["no-usable-text"]),
                         stats=_finish_stats(stats, 0, budget, floor=floor, until=merge_until))

    if headers == "shuffled":
        for index, piece in enumerate(pieces, 1):
            header = _compose_header(doc=title, date=date, path=piece.section_path,
                                     part=index, total=len(pieces), meeting=title
                                     if kind_detected == "transcript" else None,
                                     speakers=speakers or None,
                                     header_budget=header_budget, warnings=warnings)
            object.__setattr__(piece, "header", header)
            object.__setattr__(piece, "part_index", index)
            object.__setattr__(piece, "part_total", len(pieces))
            object.__setattr__(piece, "token_estimate", token_count(piece.render()))
        _shuffle_headers(pieces, seed_shift=len(pieces) // 3 + 1, stats=stats)
    elif headers:
        for index, piece in enumerate(pieces, 1):
            body_cost = token_count(piece.body)
            room = min(header_budget, max(0, budget - body_cost))
            header = _compose_header(doc=title, date=date, path=piece.section_path,
                                     part=index, total=len(pieces),
                                     meeting=title if kind_detected == "transcript" else None,
                                     speakers=speakers or None, header_budget=room,
                                     warnings=warnings)
            object.__setattr__(piece, "header", header)
            object.__setattr__(piece, "part_index", index)
            object.__setattr__(piece, "part_total", len(pieces))
            object.__setattr__(piece, "token_estimate", token_count(piece.render()))
            if piece.token_estimate > budget:
                warnings.append("piece-over-budget(header-could-not-shrink-further)")
                stats.bump("pieces_over_budget")
    else:
        for index, piece in enumerate(pieces, 1):
            object.__setattr__(piece, "part_index", index)
            object.__setattr__(piece, "part_total", len(pieces))

    for piece in pieces:
        if not piece.body.strip():
            warnings.append("empty-piece")
    return ChunkPlan(pieces=pieces, kind_detected=kind_detected,
                     warnings=tuple(sorted(set(warnings))),
                     stats=_finish_stats(stats, len(pieces), budget,
                            [piece.token_estimate for piece in pieces],
                            floor=floor, until=merge_until))


def _finish_stats(stats: _Stats, pieces: int, budget: int,
                  estimates: Sequence[int] = (), floor: int = 0,
                  until: str = DEFAULT_MERGE_UNTIL) -> dict[str, Any]:
    """Run numbers for the provenance row: counts and percentiles, never text."""
    payload: dict[str, Any] = {"tokenizer": tokenizer_name(), "budget_tokens": budget,
                               "max_piece_tokens": budget, "min_piece_tokens": floor,
                               "merge_until": until,
                               "pieces": pieces}
    if estimates:
        ordered = sorted(estimates)

        def percentile(fraction: float) -> int:
            index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
            return ordered[index]

        payload["tokens_p50"] = percentile(0.5)
        payload["tokens_p95"] = percentile(0.95)
        payload["tokens_max"] = ordered[-1]
    payload.update(dict(stats.counts))
    return payload


def body_words(plan: ChunkPlan) -> list[str]:
    """Every word of every piece body, in order. Used by the coverage invariant."""
    words: list[str] = []
    for piece in plan.pieces:
        words.extend(piece.body.split())
    return words


def coverage_gaps(source_text: str, plan: ChunkPlan) -> dict[str, Any]:
    """Account for every word: source = pieces + declared furniture drops + gaps.

    This is the chunker's promise made checkable: it may move text and drop running
    headers, but it may not silently lose content. The comparison is a word multiset
    (whitespace runs containing a letter), because the chunker rewrites table spacing, adds
    headers, and reorders nothing - so words are the stable unit, not characters.

    Counts only, never text: the caller logs ``unaccounted_word_tokens``, never which words.
    A non-zero gap means either a bug or a document shape the furniture rule misread, and
    the caller decides whether that is worth falling back over.
    """
    expected = Counter(_content_words(source_text))
    got = Counter(word for piece in plan.pieces for word in _content_words(piece.body))
    missing = expected - got
    missing_tokens = sum(missing.values())
    furniture_tokens = int(plan.stats.get("dropped_running_words", 0) or 0)
    unaccounted = max(0, missing_tokens - furniture_tokens)
    source_tokens = sum(expected.values())
    return {
        "source_word_tokens": source_tokens,
        "piece_word_tokens": sum(got.values()),
        "furniture_word_tokens": furniture_tokens,
        "missing_word_tokens": missing_tokens,
        "unaccounted_word_tokens": unaccounted,
        "unaccounted_distinct": len(missing) if unaccounted else 0,
        "covered_pct": round(100.0 * (1.0 - unaccounted / source_tokens), 2) if source_tokens else 100.0,
    }


def pick_delimiter(plan: ChunkPlan, explicit: str | None = None) -> str:
    """A delimiter that cannot occur inside any piece, cannot be stripped, or it fails.

    ``split_by_character_only`` makes the delimiter the ONLY cut LightRAG will make,
    so a delimiter that also appears inside a piece silently produces the wrong
    chunks. Choosing from candidates that are absent, and refusing an explicit one
    that is present, is what keeps the promise that our cut list is the cut list.

    Three refusals were added after the stack2 run. An explicit delimiter built from
    characters the pipeline deletes (``\x1e`` above all) is refused rather than obeyed, because
    it arrives as nothing. A candidate that only appears ONCE THE TEXT IS UNESCAPED is also
    refused: ``sanitize_text_for_encoding`` runs ``html.unescape`` before it splits, so
    ``&#10;&#10;&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;&#10;&#10;`` is a cut in the reader even
    though it is not one in the file. And when every candidate is already in the text, the
    picker builds a longer printable marker from the document's own sha256 - deterministic, so
    re-running one file picks the same delimiter - instead of reaching for a control character.

    This check runs over the piece bodies and their unescaped renders. It is an early refusal,
    not the gate: :func:`pieces_survive` re-runs the same test on the exact payload about to be
    sent, and that is what stops the request.
    """
    joined = "\n".join(piece.body for piece in plan.pieces)
    # What the reader sees before it splits: our renders, entities expanded.
    expanded = "\n".join(html.unescape(piece.render()) for piece in plan.pieces)

    def absent(marker: str) -> bool:
        return marker not in joined and marker not in expanded
    if explicit is not None:
        # One refusal for both shapes of the same mistake. A whitespace-only marker is
        # refused because the pipeline strips the field before splitting it, which is also
        # exactly why \x1e (Python calls it whitespace) killed the stack2 run.
        if not explicit.strip() or not is_survivable_delimiter(explicit):
            raise StructureChunkerError(
                "delimiter would not survive LightRAG's input sanitiser: only newline plus "
                "ASCII letters, digits, '<', '>', '-' get through sanitize_text_for_encoding "
                "intact, and a stripped or whitespace-only delimiter turns the whole document "
                "into one segment")
        if not absent(explicit):
            raise StructureChunkerError(
                "delimiter occurs inside the document - or appears once its HTML entities are "
                "unescaped, which LightRAG does before splitting: split_by_character_only "
                "would cut where this plan did not. Choose another delimiter.")
        return explicit
    digest = hashlib.sha256(joined.encode("utf-8", "replace")).hexdigest()
    for candidate in DELIMITER_CANDIDATES + tuple(
            f"\n\n<<<CHUNK-BREAK-{digest[:length]}>>>\n\n"
            for length in _HASH_DELIMITER_LENGTHS):
        if absent(candidate) and is_conservative_marker(candidate):
            return candidate
    raise StructureChunkerError(
        "no candidate delimiter is absent from this document; refusing to guess")


def lightrag_chunk_token_size(plan: ChunkPlan, margin: int = 0) -> tuple[int, str]:
    """``chunk_token_size`` for the request, in the tokens LIGHTRAG counts with.

    LightRAG sizes chunks with its own tokenizer (the tiktoken cl100k family), not with the
    embedder's, and under ``split_by_character_only`` a chunk over ``chunk_token_size`` is a
    hard ``ChunkTokenLimitExceededError`` - doc08 in the stack2 run: cts 2579 against a piece
    LightRAG measured at 2592. So this number is the largest RENDERED piece (header included)
    in ``tokenizer_name()`` units plus a margin, and it is never the embedder-token count: the
    embedder's count decides whether a piece fits the embedding window, which is a different
    question with a different answer. When tiktoken is not installed the chunker's conservative
    over-count stands in; it over-counts English prose and pipe tables, so it errs high, which
    is the safe direction for this number.
    """
    if not plan.pieces:
        raise StructureChunkerError("cannot size an empty plan")
    largest = max(token_count(piece.render()) for piece in plan.pieces)
    return max(64, largest + max(0, int(margin))), tokenizer_name()


def chunk_token_size_for_payload(payload: str, delimiter: str, margin: int = 0) -> tuple[int, str]:
    """Size ``chunk_token_size`` from the payload's OWN segments, not from the plan.

    The plan says what the pieces are; LightRAG counts what it is handed. Those can differ by
    the whitespace a split leaves behind, and under ``split_by_character_only`` a count above
    ``chunk_token_size`` is a hard ``ChunkTokenLimitExceededError`` rather than a re-cut - the
    doc08 shape from the stack2 run. Counting the segments this function is given is the
    closest offline stand-in for what that image will do, and it can only ever raise the size.
    """
    if not payload.strip():
        raise StructureChunkerError("cannot size an empty payload")
    # A single-piece payload carries no delimiter at all: one segment is the whole document,
    # and that is the case LightRAG accepts whole.
    largest = max(token_count(segment) for segment in payload.split(delimiter)
                  if segment.strip())
    return max(64, largest + max(0, int(margin))), tokenizer_name()


def render_for_lightrag(plan: ChunkPlan, delimiter: str | None = None,
                       *, chunk_token_size: int | None = None
                       ) -> tuple[str, dict[str, Any]]:
    """``(text, chunking_params)``: the delimiter-joined text and the request block.

    The params are exactly the shape LightRAG 1.5.7's ``/documents/text`` accepts:
    ``{"strategy": "fixed_token", "params": {"chunk_token_size": N,
    "split_by_character": <delimiter>, "split_by_character_only": true}}``. Numbers
    are ints and the flag is a real bool because that image answers a bool-as-int or
    a numeric string with a synchronous 422.
    """
    if not plan.pieces:
        raise StructureChunkerError("cannot render an empty plan")
    chosen = pick_delimiter(plan, delimiter)
    # No delimiter at either end of the payload. The stored field is stripped before it is
    # split, which eats the NEWLINES of a leading or trailing marker and leaves the bare
    # word glued to a piece - the payload would then contain a string that is not the
    # delimiter, and split_by_character_only would never find the cut. Between pieces the
    # marker is safe because its newlines belong to neither edge.
    text = chosen.join(piece.render() for piece in plan.pieces)
    ok, why = pieces_survive(text, chosen, [piece.render() for piece in plan.pieces])
    if not ok:
        raise StructureChunkerError(
            f"rendered payload would not survive LightRAG's input sanitiser: {why}")
    size = int(chunk_token_size or plan.stats.get("budget_tokens") or DEFAULT_BUDGET_TOKENS)
    params = {
        "strategy": "fixed_token",
        "params": {
            "chunk_token_size": max(32, size),
            "split_by_character": chosen,
            "split_by_character_only": True,
        },
    }
    return text, params
