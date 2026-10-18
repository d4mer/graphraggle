"""Structure-aware pre-chunker: OUR code decides every cut, LightRAG obeys it.

Why this module exists
----------------------
Fixed-token cutting is blind. It splits tables between the header row and the
numbers, cuts a speaker turn in half, and leaves every chunk after the first
without any idea which document or section it came from. The embedder makes the
same demand from the other side: mxbai only sees 512 tokens, so a piece has to be
SMALL and COHERENT at once. The offline benchmark (docs/ops/runs/chunk-embed-bench
.md, summarised in docs/09-retrieval-accuracy-plan.md) says pieces of about 350
cl100k tokens work best - far smaller than what today's ingest produces.

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

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

# Reuse ONE transcript parser instead of writing a second one: if this module had
# its own idea of a speaker label, the two would drift and the chunker would start
# cutting turns the preparer never recognised.
from .transcript_prep import (
    _HEADER_RE,
    _classify_paren_label,
    _leading_parenthetical,
    _metadata_from_name,
    _parse_turn,
)

# A conservative over-count. cl100k runs at roughly 4 characters per token for
# prose; 3.6 leaves ~10% headroom there, and the floor of one token per
# whitespace-separated unit covers the shapes the char term under-counts (short
# lines, heavy punctuation, unicode). Measured direction on this repo's fixtures:
# prose over-counts by ~10-15%, pipe tables by up to ~1.7x. That wastes a little
# budget and can never produce an oversize piece, which is the trade wanted.
CHARS_PER_TOKEN = 3.6

DEFAULT_BUDGET_TOKENS = 350
DEFAULT_HEADER_TOKENS = 25

# A short line seen this many times is document furniture, not content.
RUNNING_LINE_REPEATS = 3
_MAX_RUNNING_LINE_CHARS = 90
_MAX_RUNNING_LINE_WORDS = 12

# Delimiter candidates, safest first. Each can stand on its own line without being
# mistaken for content.
DELIMITER_CANDIDATES: tuple[str, ...] = (
    "\n\n\x1e\n\n",                  # ASCII record separator: near-absent in text
    "\n\n<<<CHUNK-BREAK>>>\n\n",
    "\n\n\u279c\n\n",                # heavy round-trip arrow
    "\n\n@@\n\n",
)

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
_HEADING_NUMBERED = re.compile(r"^\d{1,3}(?:\.\d{1,3}){0,4}[.)]?[ \t]+\S")
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


def _is_heading(line: str) -> bool:
    return bool(_HEADING_MD.match(line) or _HEADING_NUMBERED.match(line)
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
        if _TERMINAL.match(word) and token_count(_join(current)) >= budget * 0.5:
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


def _table_bodies(block: _Block, budget: int, stats: _Stats,
                  warnings: list[str]) -> list[str]:
    """A table block as bodies that each fit the budget, whole table if it can."""
    rows = [line for line in block.lines if line.strip()]
    if not rows:
        return []
    caption = block.caption.strip() if block.caption else ""
    whole = "\n".join(([caption] if caption else []) + list(rows))
    if token_count(whole) <= budget:
        return [whole]
    headers = _header_rows(rows, stats)
    data = rows[len(headers):] if headers else rows
    stats.bump("tables_split_by_row")
    prefix = "\n".join(headers)
    prefix_cost = token_count(prefix) if headers else 0
    # The caption rides on the FIRST piece only: it names the table, and a repeat
    # of it would be counted content twice for no gain in retrieval.
    first_overhead = prefix_cost + (token_count(caption) if caption else 0)
    later_overhead = prefix_cost
    bodies: list[str] = []
    current: list[str] = []

    def overhead() -> int:
        return first_overhead if not bodies else later_overhead

    def flush() -> None:
        if not current:
            return
        lead = ([caption] if not bodies and caption else []) + list(headers) + current
        bodies.append("\n".join(lead))
        current.clear()

    for row in data:
        # A row that does not fit ALONGSIDE the repeated header rows is split at
        # cell boundaries: keeping the header is not optional, so the row gives.
        if token_count(row) + overhead() > budget:
            flush()
            room = max(16, (later_overhead and budget - later_overhead) or budget - prefix_cost)
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

def _block_bodies(block: _Block, budget: int, stats: _Stats,
                  warnings: list[str]) -> list[tuple[str, str]]:
    """One block -> (kind, body) units that each fit the budget."""
    if block.kind == "table":
        return [("table", body) for body in _table_bodies(block, budget, stats, warnings)]
    text = "\n".join(line for line in block.lines if line.strip())
    if not text:
        return []
    if token_count(text) <= budget:
        return [(block.kind, text)]
    if block.kind == "heading":
        # A heading longer than the budget is a heading, not prose: it cannot be
        # split without destroying what makes it a heading.
        warnings.append("heading-over-budget")
        return [(block.kind, text)]
    stats.bump("blocks_split")
    return [(block.kind, body) for body in _split_run(text, budget)]


def _pack_blocks(blocks: Sequence[_Block], budget: int, stats: _Stats,
                 warnings: list[str]) -> list[tuple[str, str, tuple[str, ...]]]:
    """Greedily fill pieces to the budget, breaking only where structure allows.

    A heading always starts a piece. A table always stands alone (its header row is
    the whole point of keeping it). A list travels with the paragraph that
    introduced it when there is room, and starts a piece when there is not.
    """
    pieces: list[tuple[str, str, tuple[str, ...]]] = []
    current: list[str] = []
    current_path: tuple[str, ...] = ()
    current_kind = ""

    def flush() -> None:
        if not current:
            return
        body = "\n".join(current)
        if body.strip():
            pieces.append((current_kind or "paragraph", body, current_path))
        current.clear()

    for block in blocks:
        path = block.path
        for kind, body in _block_bodies(block, budget, stats, warnings):
            if kind == "table":
                flush()
                pieces.append(("table", body, path))
                current_path = path
                continue
            if kind == "heading":
                flush()
                current.append(body)
                current_path = path
                current_kind = "section"
                continue
            if not current:
                current.append(body)
                current_path = path
                current_kind = "list" if kind == "list" else "paragraph"
                continue
            candidate = "\n".join(current + [body])
            if token_count(candidate) <= budget:
                current.append(body)
                if current_kind == "section":
                    current_kind = "section"
                continue
            flush()
            current.append(body)
            current_path = path
            current_kind = "list" if kind == "list" else "paragraph"
    flush()
    return pieces


# ── transcript path ────────────────────────────────────────────────────────

@dataclass
class _TurnLines:
    speaker: str | None
    lines: list[str] = field(default_factory=list)


def _scan_turns(lines: Sequence[str]) -> tuple[list[_TurnLines], Counter[str], int, int]:
    """Turn-shaped scan: who speaks, and the ORIGINAL lines of each turn.

    The lines are kept verbatim rather than rebuilt from parsed words, so the
    speaker labels and any cue lines inside a turn survive the trip through the
    chunker - the pieces have to account for every word in the document.
    """
    turns: list[_TurnLines] = []
    label_counts: Counter[str] = Counter()
    label_lines = 0
    cue_lines = 0
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        if "-->" in line and re.search(r"\d{1,2}:\d{2}", line):
            cue_lines += 1
        parsed = _parse_turn(line)
        if parsed is not None:
            speaker, _body, _style = parsed
            label_lines += 1
            label_counts[speaker.lower()] += 1
            turns.append(_TurnLines(speaker=speaker, lines=[line]))
            continue
        candidate = _leading_parenthetical(line)
        if candidate is not None and _classify_paren_label(candidate[0])[1] == "unparseable":
            # Its own turn, no speaker claimed - transcript_prep's rule, for its
            # reason: guessing a speaker is worse than claiming none.
            turns.append(_TurnLines(speaker=None, lines=[line]))
            continue
        if turns:
            turns[-1].lines.append(line)
        else:
            turns.append(_TurnLines(speaker=None, lines=[line]))
    return turns, label_counts, label_lines, cue_lines


def _transcript_like(turns: Sequence[_TurnLines], label_counts: Counter[str],
                     label_lines: int, cue_lines: int, nonempty: int) -> bool:
    """The same bars as :func:`app.transcript_prep.prepare_transcript`.

    Shared bars, deliberately: a document the preparer will not touch must not be
    chunked as a transcript here either, or the two modules disagree about what the
    corpus contains.
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
    return bool(labelled or single_voice or captioned)


def _transcript_units(turns: Sequence[_TurnLines], budget: int, stats: _Stats,
                      warnings: list[str]) -> list[str]:
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
        parts = _split_run(body, budget, clause_fallback=True)
        for part in parts:
            if token_count(part) > budget:
                warnings.append("turn-over-budget(one-unsplittable-word)")
                stats.bump("turn_pieces_over_budget")
            bodies.append(part)
    if current:
        bodies.append("\n".join(current))
    return bodies


# ── public entry points ────────────────────────────────────────────────────

def chunk_document(text: str, *, filename: str = "", budget_tokens: int = DEFAULT_BUDGET_TOKENS,
                   header_tokens: int = DEFAULT_HEADER_TOKENS, kind: str = "auto",
                   headers: Any = True) -> ChunkPlan:
    """Plan every cut in one document. Deterministic; nothing is invented.

    ``kind``: ``auto`` detects, or force ``transcript`` / ``table`` / ``structured``
    / ``plain``. ``headers``: ``True`` for the deterministic context header,
    ``False`` for none, ``"shuffled"`` for the benchmark placebo (same tokens, wrong
    attachment). The budget always covers body AND header.
    """
    if kind not in ("auto", "transcript", "table", "structured", "plain"):
        raise StructureChunkerError(f"unknown kind {kind!r}")
    budget = max(32, int(budget_tokens))
    header_budget = max(0, int(header_tokens))
    if not text or not text.strip():
        return ChunkPlan(pieces=[], kind_detected="plain", warnings=("empty-input",),
                         stats={"tokenizer": tokenizer_name(), "budget_tokens": budget,
                                "pieces": 0})

    stats = _Stats()
    warnings: list[str] = []
    raw_lines = [line for line in text.splitlines() if not _HEADER_RE.match(line.rstrip())]
    stats.bump("lines_in", len([line for line in text.splitlines() if line.strip()]))
    if len(raw_lines) != len(text.splitlines()):
        stats.bump("dropped_prior_headers")

    units: list[tuple[str, str, tuple[str, ...]]] = []
    kind_detected = "plain"
    speakers: list[str] = []
    title, date = _metadata_from_name(filename)

    if kind in ("auto", "transcript"):
        turns, label_counts, label_lines, cue_lines = _scan_turns(raw_lines)
        nonempty = len([line for line in raw_lines if line.strip()])
        if _transcript_like(turns, label_counts, label_lines, cue_lines, nonempty):
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
            bodies = _transcript_units(turns, max(32, budget - reserve), stats, warnings)
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
        units = _pack_blocks(blocks, max(32, budget - reserve), stats, warnings)
        table_tokens = sum(token_count(body) for kind_name, body, _p in units if kind_name == "table")
        all_tokens = sum(token_count(body) for _k, body, _p in units) or 1
        if table_tokens >= 40 and table_tokens >= 0.4 * all_tokens:
            kind_detected = "table"
        elif any(kind_name == "section" for kind_name, _b, _p in units):
            kind_detected = "structured"

    pieces: list[Piece] = []
    for unit_kind, body, path in units:
        if not body.strip():
            continue
        pieces.append(Piece(body=body.strip("\n"), header=None, kind=unit_kind,
                            section_path=tuple(path), token_estimate=token_count(body)))
    if not pieces:
        return ChunkPlan(pieces=[], kind_detected=kind_detected,
                         warnings=tuple(sorted(warnings) + ["no-usable-text"]),
                         stats=_finish_stats(stats, 0, budget))

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
                                         [piece.token_estimate for piece in pieces]))


def _finish_stats(stats: _Stats, pieces: int, budget: int,
                  estimates: Sequence[int] = ()) -> dict[str, Any]:
    """Run numbers for the provenance row: counts and percentiles, never text."""
    payload: dict[str, Any] = {"tokenizer": tokenizer_name(), "budget_tokens": budget,
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
    """A delimiter that cannot occur inside any piece, or a loud failure.

    ``split_by_character_only`` makes the delimiter the ONLY cut LightRAG will make,
    so a delimiter that also appears inside a piece silently produces the wrong
    chunks. Choosing from candidates that are absent, and refusing an explicit one
    that is present, is what keeps the promise that our cut list is the cut list.
    """
    joined = "\n".join(piece.body for piece in plan.pieces)
    if explicit is not None:
        if not explicit.strip():
            raise StructureChunkerError("delimiter must not be empty or whitespace only")
        if explicit in joined:
            raise StructureChunkerError(
                "delimiter occurs inside the document: split_by_character_only would cut "
                "where this plan did not. Choose another delimiter.")
        return explicit
    for candidate in DELIMITER_CANDIDATES:
        if candidate not in joined:
            return candidate
    raise StructureChunkerError(
        "no candidate delimiter is absent from this document; refusing to guess")


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
    text = chosen.join(piece.render() for piece in plan.pieces) + chosen
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
