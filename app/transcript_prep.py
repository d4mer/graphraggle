"""Transcript-shaped text preparation, run BEFORE text is handed to LightRAG.

Why this module exists
----------------------
LightRAG 1.5.7 slices text into fixed-size token chunks. For transcripts that
cuts mid-sentence, separates a decision from its reasoning, and leaves every
chunk after the first with no idea what meeting it came from. Chunk size, TOP_K,
reranking and token budgets were tuned and did not move accuracy, so the lever
is the TEXT we hand over: this module makes every chunk LightRAG produces
self-describing and aligned to speaker turns, without touching LightRAG.

Contract
--------
1. Conservative detection. A document is only touched when it really looks like
   a transcript (speaker-label lines or subtitle cues). Three label shapes are
   recognised: ``Name: text`` / ``Name 0:12`` / ``[00:01:02] Name:`` (colon
   family) and ``(Label) text`` (the parenthesised family). The parenthesised
   family carries the real dialect: initials and titles with periods
   (``(Dr. Lee)``, ``(J. Smith)``), a comma-separated role or company
   (``(Ana, Acme Legal)``), one level of nesting (``(Ana (Acme))``), digits inside
   the label (``(Speaker 2)``), and a timestamp or cue index before the bracket
   (``00:12 (Ana) text``). When unsure, the input is returned unchanged with
   ``detected=False``. Specs, policies and notes are never altered, and prose
   parentheses (``(see above)``, ``(1)``, ``(laughter)``, ``(Fig 3)``,
   ``(and then we agreed)``) are not mistaken for speakers.
2. Words are never changed, only placed. A turn keeps the label style the
   document already uses - ``(Ana) text`` stays ``(Ana) text``, ``Alice: text``
   stays ``Alice: text`` - consecutive lines of one speaker are merged (a
   label-less continuation line belongs to the previous turn), and lines that
   carry no words at all (timestamps, subtitle cues, separators) are dropped.
   Original word order and wording are preserved exactly - no summarising, no
   spelling fixes, no normalising of garbled speech-to-text.
   A line that OPENS with a parenthetical whose label is a known non-speaker
   marker is content and joins the previous turn; a line whose label is *trying*
   to be a name and cannot be read is never merged onto someone else's turn - it
   stands as its own speakerless turn, and if too many lines are like that the
   whole document is left alone with ``reason="unparseable-labels(n)"``.
   Guessing a speaker is worse than claiming none.
3. A one-line context header is re-inserted before every block of roughly
   ``header_every_tokens`` tokens, e.g.
   ``[Meeting: design review | Date: 2026-10-09 | Speakers: Alice Chen, Bob | Part 2/5]``.
   Title and date come ONLY from the file-name stem, never from the content.
   Missing fields are omitted; participants are only ever the names observed as
   speaker labels.
4. Idempotent: running it on its own output gives the same output (our own
   headers are stripped and regenerated).
5. Pure and deterministic: no I/O, no clock, no randomness, no settings import,
   so ``scripts/overnight_loop.py`` can use it without loading app.config.

Token counting
--------------
LightRAG tokenizes with tiktoken (cl100k_base), which is NOT installed in this
repo's venv (LightRAG runs in its own image), so :func:`estimate_tokens`
approximates: about 4 characters per token with a floor of one token per word.
That is close enough because the cadence is far below the chunk size - at the
shipped default (200) versus production's 1200-token chunks there is a 6x margin,
so a large tokenizer error still leaves every chunk carrying a header.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Sequence

# Roughly cl100k's real rate for English prose (~4 characters per token). The
# word floor in estimate_tokens() keeps it from under-counting text with long
# words or heavy punctuation. Coverage does not depend on being conservative:
# the default cadence (200) sits 6x below production's 1200-token chunks, so even
# a 2x tokenizer error leaves several headers inside every chunk.
CHARS_PER_TOKEN = 4.0

# Default header cadence. Production LightRAG runs with its default chunk size
# (1200 tokens, 100 overlap); a header every ~200 estimated tokens puts several
# headers inside every chunk LightRAG cuts.
DEFAULT_HEADER_EVERY_TOKENS = 200

_MAX_HEADER_SPEAKERS = 8

_NAME = r"[^\W\d_][\w'’.\-]*(?:[ \t]+[^\W\d_][\w'’.\-]*){0,2}"
_TS = r"\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d{1,3})?"
_OPT_TS = rf"(?:\(?{_TS}\)?)?"

# "(Ana) text" / "(Speaker 1) text" / "(Dr. Lee, Acme Legal) text" - the family
# every real transcript in this corpus uses. Hermes' shape report (F3) showed it
# is wider than the original flat regex: a timestamp or cue index before the "(",
# nested parentheses, initials with periods, a comma-separated role or company,
# digits inside the label. The line is cut with a balanced-paren scan
# (:func:`_leading_parenthetical`) so nesting works, and the label is validated by
# :func:`_classify_paren_label`, which now says WHY a label was refused.
_PAREN_PREFIX = re.compile(rf"^(?:{_TS}|\[{_TS}\]|[0-9]{{1,4}}[.)]?)[ \t]+")
_MAX_LABEL_CHARS = 60
_MAX_LABEL_WORDS = 8
_MAX_LABEL_PARTS = 4
_MAX_NESTED_WORDS = 3

# A period inside a speaker label is legitimate only as an initial or as one of
# these abbreviations. Anywhere else the parenthetical is a sentence, not a name
# (this is the rule that threw away "(Dr. Lee)" in F2).
_LABEL_ABBREVIATIONS = frozenset({
    "dr", "prof", "mr", "mrs", "ms", "rev", "fr", "sr", "jr", "esq", "sen",
    "rep", "gov", "pres", "ceo", "cto", "cfo", "coo", "vp", "hr", "md", "phd",
    "eng", "ing", "adv", "atty", "mgr", "lt", "capt", "st", "no", "de",
})

# Words that make a parenthetical prose rather than a name, checked on every word
# of every comma-separated part: "(Ana, Acme Legal)" survives, "(and then we
# agreed)" never becomes a speaker.
_STOPWORD_LABELS = frozenset({
    "the", "a", "an", "and", "or", "but", "nor", "of", "for", "to", "in", "on",
    "at", "by", "with", "without", "within", "we", "you", "they", "it", "its",
    "this", "that", "these", "those", "is", "are", "was", "were", "be", "been",
    "being", "he", "she", "his", "her", "their", "there", "here", "ours", "yours",
    "not", "no", "yes", "if", "so", "as", "from", "who", "what", "when", "where",
    "why", "how", "about", "into", "over", "under", "before", "after", "because",
    "then", "than", "while", "during", "per", "vs", "versus", "etc", "see",
    "continued", "continues", "above", "below", "same", "both", "each", "again",
    "some", "any", "all", "more", "less", "most", "other", "others",
})

# "Ana, Acme Legal" - comma-separated parts. A trailing "", "3)" style enum is
# handled upstream, so the split can be a plain comma.
_LABEL_SPLIT = re.compile(r",")
_NESTED_PAREN = re.compile(r"^\((?P<inner>[^()]+)\)$")
_LABEL_WORD = re.compile(r"[^\W\d_][\w'’.–\-]*|[0-9]{1,4}")
_CROSSREF_LABEL = re.compile(r"([^()\s]+)(?:[ \t]+([0-9]{1,3}))?[ \t]*\.?")

# Speaker label shapes, tried in order.
_TURN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "[00:01:02] Alice: text" / "[00:01:02] - Alice: text"
    re.compile(rf"^\[{_TS}\][ \t]*-?[ \t]*(?P<name>{_NAME})[ \t]*:[ \t]*(?P<body>.*)$"),
    # "Alice [00:01:02]: text"
    re.compile(rf"^(?P<name>{_NAME})[ \t]+\[{_TS}\][ \t]*:[ \t]*(?P<body>.*)$"),
    # "00:01:02 Alice: text"
    re.compile(rf"^{_TS}[ \t]+(?P<name>{_NAME})[ \t]*:[ \t]*(?P<body>.*)$"),
    # "Alice 0:12" or "Alice Chen 00:01:02" (label + timestamp, body on next line)
    re.compile(rf"^(?P<name>{_NAME})[ \t]+{_TS}[ \t]*:?[ \t]*(?P<body>.*)$"),
    # "Alice: text"
    re.compile(r"^(?P<name>[^\W\d_][\w'’.\-]*(?:[ \t]+[^\W\d_][\w'’.\-]*){0,2})[ \t]*:[ \t](?P<body>.*)$"),
    # ">> Alice: text"
    re.compile(r"^>>[ \t]*(?P<name>{})[ \t]*:[ \t]*(?P<body>.*)$".format(_NAME)),
)

# Lines that carry no words at all: cues, ids, bare timestamps, separators.
_NOISE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(rf"^\s*(?:{_TS}[ \t]*-->[ \t]*{_TS})\s*$"),          # VTT/SRT cue range
    re.compile(r"^\s*\d{1,6}\s*$"),                                   # SRT cue index
    re.compile(r"^\s*WEBVTT(?:[ \t].*)?\s*$", re.IGNORECASE),
    re.compile(r"^\s*(?:Kind|Language)[ \t]*:.*$", re.IGNORECASE),
    re.compile(r"^\s*(?:NOTE|NOTE\s+\S+)[ \t]*:?\s*$", re.IGNORECASE),
    re.compile(rf"^\s*\(?{ _TS }\)?\s*$"),                            # bare timestamp
    re.compile(r"^\s*[-=_*·•>]{2,}\s*$"),                             # separators
    re.compile(r"^\s*>>\s*$"),
    re.compile(r"^\s*<v[^>]*>\s*$"),                                  # VTT voice tag alone
)

# Words that look like "Name:" but are document furniture, not speakers.
_NON_SPEAKER_LABELS = frozenset({
    "note", "notes", "notebook", "warning", "caution", "important", "attention",
    "todo", "fixme", "asdf", "step", "rule", "rule 1", "summary", "agenda",
    "action", "actions", "answer", "question", "q", "a", "example", "e.g", "i.e",
    "see", "fig", "figure", "table", "chapter", "section", "version", "date",
    "time", "subject", "re", "from", "to", "cc", "http", "https", "ftp", "www",
    "source", "reference", "references", "copyright", "license", "warning:",
    " ingredients", "ingredients", "directions", "instructions", "materials",
})

# A turn label in parentheses that is a stage direction or a prose
# parenthetical, not a speaker. Counted as content, never as a speaker label.
_NON_SPEAKER_PARENS = frozenset({
    "inaudible", "unintelligible", "unclear", "silence", "pause", "laughter",
    "applause", "crosstalk", "overlap", "background noise", "noise", "music",
    "off mic", "on mic", "unknown", "unknown speaker", "speaker unknown",
    "see above", "see below", "above", "below", "optional", "continued",
    "continues", "cont", "footnote", "ibid", "sic", "emphasis", "original",
    "translation", "trans", "approx", "circa", "n a", "na", "example",
    "quote", "quotation", "excerpt", "end", "to be continued", "note",
    "notes", "warning", "figure", "table", "chapter", "section", "version",
    "date", "time", "source", "reference", "references",
})

# Nouns that, followed by a number, are a cross-reference rather than a speaker.
_CROSSREF_NOUNS = frozenset({
    "fig", "figure", "figs", "figures", "table", "tables", "tab", "chart",
    "image", "images", "photo", "chapter", "chapters", "section", "sections",
    "sec", "appendix", "page", "pages", "line", "lines", "verse", "verses",
    "note", "notes", "footnote", "footnotes", "example", "examples", "ex",
    "equation", "equations", "eq", "step", "steps", "rule", "rules", "item",
    "items", "point", "points", "part", "parts", "paragraph", "paragraphs",
})

# F2/F3: when a material share of the paren-shaped label lines cannot be parsed,
# the parser is not reading this document's dialect. Prepare nothing rather than
# write half-guessed attribution into every chunk of a real corpus.
UNPARSEABLE_REFUSE_MIN = 3
UNPARSEABLE_REFUSE_FRACTION = 0.20

# A date in the file name, ISO-ish. Only the name is read - never the content.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})"),
    re.compile(r"(?P<date>\d{4}_\d{2}_\d{2})"),
    re.compile(r"(?P<date>\d{8})(?=[ _.\-]|$)"),
)

# Matches a header this module inserted. "Meeting:" is optional: a file named
# "transcript.txt" yields no title, and every field can be absent, so the one
# field that is always there is the part counter.
_HEADER_RE = re.compile(
    r"^\[(?:[^\]\[]*\| )*Part \d+/\d+\]\s*$"
)
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

# Subtitle-format markers that are letters but are file furniture, not content.
# They are stripped with the cues, and :func:`word_tokens` does not count them as
# words, which is what makes the reversibility check honest rather than strict.
FORMAT_MARKER_WORDS = frozenset({"webvtt", "kind", "language", "note"})


@dataclass(frozen=True)
class PrepResult:
    """Outcome of one preparation pass. ``text`` is always safe to submit."""

    text: str
    detected: bool
    headers_inserted: int = 0
    speakers: tuple[str, ...] = ()
    title: str | None = None
    date: str | None = None
    reason: str = "not-checked"
    turns: int = 0
    header_tokens_estimate: int = 0
    # Detector counts - numbers, never text - so a skipped document says WHY in a
    # way an operator can act on. F3: "no-recurring-speaker(2 labels)" said
    # nothing about how many labelled lines the parser had rejected.
    lines_nonempty: int = 0
    labels_parsed: int = 0
    labels_rejected: int = 0      # paren-shaped lines not accepted as a speaker
    labels_unparseable: int = 0   # ... of which were trying to be a name (F2/F3)

    @property
    def applied(self) -> bool:
        return self.detected and self.headers_inserted > 0


@dataclass
class _Turn:
    speaker: str | None
    words: list[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Approximation of LightRAG's cl100k token count (see module docstring)."""
    if not text:
        return 0
    by_chars = int(len(text) / CHARS_PER_TOKEN) + 1
    words = len(_WORD_RE.findall(text))
    return max(by_chars, words)


def _is_noise(line: str) -> bool:
    return any(pattern.match(line) for pattern in _NOISE_PATTERNS)


def _clean_name(raw: str) -> str | None:
    name = " ".join(raw.strip().split())
    if not name or len(name) > 40:
        return None
    if name.lower() in _NON_SPEAKER_LABELS:
        return None
    # A label that ends in punctuation or contains a path/URL fragment is not a name.
    if "://" in name or name.endswith((",", ";", ".")) or "?" in name:
        return None
    if not re.match(r"[^\W\d_]", name):
        return None
    return name


def _leading_parenthetical(line: str) -> tuple[str, str] | None:
    """Split a line that OPENS with a balanced parenthetical into ``(inner, rest)``.

    An optional timestamp or cue index may come first ("00:12 (Ana) here"), the
    shape that made the old anchored regex reject whole documents (F3). A paren
    that opens mid-sentence is never a candidate, so prose stays prose. Nesting is
    handled by counting depth, so "(Ana (Acme)) text" yields "Ana (Acme)".
    """
    offset = 0
    prefix = _PAREN_PREFIX.match(line)
    if prefix:
        offset = prefix.end()
    if not line[offset:].startswith("("):
        return None
    depth = 0
    for position in range(offset, len(line)):
        char = line[position]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                rest = line[position + 1:]
                if rest and not rest[0].isspace():
                    return None      # "(word)word" is a token, not a label
                return line[offset + 1:position], rest.strip()
    return None                      # unbalanced: not our shape


def _label_words(part: str) -> list[str]:
    return _LABEL_WORD.findall(part)


def _word_is_name_like(word: str) -> bool:
    """A label word: letters, optionally an initial or abbreviation ending in a
    period. A bare number is not name-like on its own (a part may not START with
    one, which is what keeps "(3)" and "(1st)" out)."""
    if word.isdigit():
        return False
    bare = word.replace("\u2019", "'")
    if bare.endswith(".") and bare.count(".") > 1:
        return False                                  # "e.g." / "www."
    if bare.endswith("."):
        stem = bare[:-1]
        return bool(re.fullmatch(r"[^\W\d_][\w'’\-]*", stem)) and (
            len(stem) <= 2 or stem.lower() in _LABEL_ABBREVIATIONS
        )
    return bool(re.fullmatch(r"[^\W\d_][\w'’.\-]*", bare))


def _name_like_part(part: str) -> bool:
    """Letters, and digits only in a non-leading position: "(Speaker 1)" and
    "(Team 12)" are speakers, "(3)" and "(1st)" are enumerations."""
    words = _label_words(part)
    if not words or words[0].isdigit():
        return False
    return all(_word_is_name_like(word) or word.isdigit() for word in words)


def _classify_paren_label(raw: str) -> tuple[str | None, str]:
    """Validate a ``(Label)`` prefix, returning ``(label or None, kind)``.

    ``kind`` is ``"ok"``, ``"non-speaker"`` (stage direction, enumeration,
    numbered cross-reference or prose parenthetical - content, merged as before)
    or ``"unparseable"`` (it is reaching for a speaker name this parser cannot
    read). The third kind is what F2 silently glued onto the previous speaker, so
    it is named, counted and reported instead of merged.
    """
    label = " ".join(raw.strip().split())
    if not label:
        return None, "non-speaker"
    lowered = label.lower()
    if lowered in _NON_SPEAKER_LABELS or lowered in _NON_SPEAKER_PARENS:
        return None, "non-speaker"
    # "(a)", "(1)", "(i)", "(iii)": an enumeration or a bare marker.
    if len(label) < 2 or not re.match(r"[^\W\d_]", label):
        return None, "non-speaker"
    # "(Fig 3)", "(Table 2)", "(Note 1)": a numbered cross-reference, which prose
    # repeats often enough to look like a recurring speaker.
    crossref = _CROSSREF_LABEL.fullmatch(label)
    if crossref and crossref.group(2) and crossref.group(1).rstrip(".").lower() in _CROSSREF_NOUNS:
        return None, "non-speaker"
    if any(char in label for char in ("?", "!", "…", ":", ";", "/", "\\")):
        return None, "unparseable"
    if len(label) > _MAX_LABEL_CHARS:
        return None, "unparseable"

    parts = [part.strip() for part in _LABEL_SPLIT.split(label) if part.strip()]
    if not parts or len(parts) > _MAX_LABEL_PARTS:
        return None, "unparseable"
    total_words = 0
    for part in parts:
        words = _label_words(part)
        if any(word.lower() in _STOPWORD_LABELS for word in words):
            return None, "non-speaker"          # prose, not a name
        nested = _NESTED_PAREN.search(part)
        if nested:
            inner_words = _label_words(nested.group("inner"))
            if (not inner_words or len(inner_words) > _MAX_NESTED_WORDS
                    or any(word.lower() in _STOPWORD_LABELS for word in inner_words)):
                return None, "unparseable"
            flattened = _NESTED_PAREN.sub(lambda m: " " + m.group("inner") + " ", part)
            if not _name_like_part(flattened.strip()):
                return None, "unparseable"
        elif not _name_like_part(part):
            return None, "unparseable"
        total_words += len(words)
    if total_words > _MAX_LABEL_WORDS:
        return None, "unparseable"
    return label, "ok"


def _parse_turn(line: str) -> tuple[str, str, str] | None:
    """Return ``(speaker, body, style)`` when the line starts a speaker turn.

    ``style`` is ``"paren"`` for ``(Ana) text`` and ``"colon"`` for the
    ``Ana: text`` family. The style is remembered so a document is written back
    in the shape it arrived in.
    """
    candidate = _leading_parenthetical(line.rstrip())
    if candidate is not None:
        inner, rest = candidate
        name, kind = _classify_paren_label(inner)
        if name is not None:
            return name, rest, "paren"
    for pattern in _TURN_PATTERNS:
        match = pattern.match(line.rstrip())
        if not match:
            continue
        name = _clean_name(match.group("name"))
        if name is None:
            continue
        body = (match.group("body") or "").strip()
        return name, body, "colon"
    return None


def _metadata_from_name(filename: str | None) -> tuple[str | None, str | None]:
    """Title and date from the file-name stem only. Never from content."""
    if not filename:
        return None, None
    stem = str(filename)
    for suffix in (".vtt", ".srt", ".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm"):
        if stem.lower().endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    date: str | None = None
    for pattern in _DATE_PATTERNS:
        match = pattern.search(stem)
        if match:
            raw = match.group("date")
            date = raw.replace("_", "-")
            if len(raw) == 8:
                date = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
            stem = stem[: match.start()] + " " + stem[match.end():]
            break
    words = [word for word in re.split(r"[ _.\-]+", stem) if word]
    drop = {"transcript", "transcripto", "recording", "meeting", "call", "minutes"}
    # A name that is only generic words ("transcript", "meeting-2026-10-09") gives
    # no title: there is nothing to say, and inventing one would be a lie.
    title_words = [word for word in words if word.lower() not in drop]
    title = " ".join(title_words).strip() or None
    return title, date


def _header_line(
    title: str | None,
    date: str | None,
    speakers: Sequence[str],
    part: int,
    total: int,
) -> str:
    fields = []
    if title:
        fields.append(f"Meeting: {title}")
    if date:
        fields.append(f"Date: {date}")
    if speakers:
        shown = list(speakers[:_MAX_HEADER_SPEAKERS])
        extra = len(speakers) - len(shown)
        listing = ", ".join(shown) + (f", +{extra} more" if extra > 0 else "")
        fields.append(f"Speakers: {listing}")
    fields.append(f"Part {part}/{total}")
    return "[" + " | ".join(fields) + "]"


def _split_long_turn(turn: _Turn, budget_tokens: int) -> list[_Turn]:
    """Split ONE turn when it is longer than two header budgets.

    A turn that long is going to be cut mid-sentence by LightRAG anyway, so
    letting it stand unsplit means the chunks inside it carry no context at all.
    Splitting prefers sentence terminators, and only when the turn alone already
    blows the budget twice over - short turns are never broken. When there is no
    terminator to use - the degenerate speech-to-text shape: one 7,000-token run
    with no punctuation - it falls back to packing whole words up to the budget,
    which breaks at whitespace and never inside a word, because words come from a
    whitespace split. A terminator-bounded piece that is itself oversized is packed
    the same way, so a header-free stretch cannot survive either path. Every piece
    keeps the real speaker's label, so a chunk cut inside a monologue still says
    who is talking. Word order is preserved exactly in both paths.
    """
    if not turn.words:
        return [turn]
    total = estimate_tokens(" ".join(turn.words))
    if total <= 2 * max(32, budget_tokens):
        return [turn]
    budget = max(32, int(budget_tokens))
    pieces: list[list[str]] = []
    current: list[str] = []
    for word in turn.words:
        current.append(word)
        if word.endswith((".", "!", "?", "\u2026")) and estimate_tokens(" ".join(current)) >= budget:
            pieces.append(current)
            current = []
    if current:
        pieces.append(current)
    if len(pieces) <= 1:
        pieces = _pack_words(turn.words, budget)
    else:
        pieces = [piece if estimate_tokens(" ".join(piece)) <= 2 * budget
                  else _pack_words(piece, budget) for piece in pieces]
    pieces = [piece for piece in pieces if piece]
    if len(pieces) <= 1:
        return [turn]
    return [_Turn(speaker=turn.speaker, words=piece) for piece in pieces]


def _pack_words(words: Sequence[str], budget_tokens: int) -> list[list[str]]:
    """Chunk a word list into runs of at most ``budget_tokens`` estimated tokens.

    Used when a turn has no usable sentence boundary. The break always falls
    between words - never inside one - so the words survive intact and in order.
    A single word longer than the budget cannot be broken and stands alone.
    """
    packed: list[list[str]] = []
    current: list[str] = []
    for word in words:
        if current and estimate_tokens(" ".join(current + [word])) > budget_tokens:
            packed.append(current)
            current = []
        current.append(word)
    if current:
        packed.append(current)
    return packed


def _segment_turns(turns: Sequence[_Turn], header_every_tokens: int, header_tokens: int) -> list[list[_Turn]]:
    """Group turns into blocks of about ``header_every_tokens`` tokens.

    Breaks between turns, so a speaker turn is not split unless it is longer than
    two budgets on its own (see :func:`_split_long_turn`). A single turn longer
    than the budget gets its own block; nothing is ever truncated.
    """
    budget = max(32, int(header_every_tokens))
    expanded: list[_Turn] = []
    for turn in turns:
        expanded.extend(_split_long_turn(turn, budget))
    segments: list[list[_Turn]] = []
    current: list[_Turn] = []
    current_tokens = 0
    for turn in expanded:
        turn_tokens = estimate_tokens(" ".join(turn.words))
        if current and current_tokens + turn_tokens + header_tokens > budget:
            segments.append(current)
            current, current_tokens = [], 0
        current.append(turn)
        current_tokens += turn_tokens
    if current:
        segments.append(current)
    return segments


def prepare_transcript(
    text: str,
    *,
    filename: str | None = None,
    header_every_tokens: int = DEFAULT_HEADER_EVERY_TOKENS,
) -> PrepResult:
    """Return prepared text, or the input unchanged when it is not a transcript.

    ``filename`` is used ONLY for the header's title and date (its stem). No file
    is read, no clock is read, and the same input always gives the same output.
    """
    if not text or not text.strip():
        return PrepResult(text=text, detected=False, reason="empty")

    lines = text.splitlines()
    # Idempotency: drop headers this module inserted, then regenerate them.
    lines = [line for line in lines if not _HEADER_RE.match(line.rstrip())]

    turns: list[_Turn] = []
    label_lines = 0
    paren_label_lines = 0
    rejected_labels = 0
    unparseable_labels = 0
    label_counts: dict[str, int] = {}
    paren_counts: dict[str, int] = {}
    cue_lines = 0
    nonempty = 0
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        nonempty += 1
        if re.match(rf"^\s*{_TS}[ \t]*-->[ \t]*{_TS}\s*$", line):
            cue_lines += 1
        parsed = _parse_turn(line)
        if parsed is not None:
            speaker, body, style = parsed
            label_lines += 1
            label_counts[speaker.lower()] = label_counts.get(speaker.lower(), 0) + 1
            if style == "paren":
                paren_label_lines += 1
                paren_counts[speaker.lower()] = paren_counts.get(speaker.lower(), 0) + 1
            if body:
                turns.append(_Turn(speaker=speaker, words=body.split()))
            else:
                turns.append(_Turn(speaker=speaker))
            continue
        candidate = _leading_parenthetical(line)
        if candidate is not None:
            kind = _classify_paren_label(candidate[0])[1]
            if kind != "ok":
                rejected_labels += 1      # every paren line that is not a speaker
            if kind == "unparseable":
                unparseable_labels += 1
                # F2. A "(Label)" line whose label this parser cannot read is NOT
                # a continuation: gluing it onto the previous speaker writes the
                # wrong person into every chunk that covers it. It stands as its
                # own turn, word for word, with no speaker claimed.
                turns.append(_Turn(speaker=None, words=line.split()))
                continue
        if _is_noise(line):
            continue
        if turns:
            turns[-1].words.extend(line.split())
        else:
            turns.append(_Turn(speaker=None, words=line.split()))

    subtitle_cues = cue_lines >= 3
    distinct = len(label_counts)
    # The parenthesised family is the one prose can imitate ("(see above)",
    # "(1) first"), so it still has to clear a bar of its own: the SAME label must
    # head at least two lines. F3 lowered the old bar of three, which threw away
    # real two-voice recordings, and the label grammar widened to match the corpus.
    render_style = "paren" if paren_label_lines >= max(3, label_lines - paren_label_lines) else "colon"
    repeat_required = 2
    repeated = any(count >= repeat_required for count in label_counts.values())
    # Detection is deliberately conservative; a document that fails any of these
    # is handed to LightRAG untouched. The density gate is 20% of non-empty lines
    # (was 30%), which is what lets a transcript with long label-less continuation
    # paragraphs through.
    labelled_transcript = (
        label_lines >= 3
        and distinct >= 2
        and repeated
        and label_lines >= 0.20 * nonempty
    )
    # One voice, repeated enough to be a recording rather than a labelled list.
    single_voice = (
        distinct == 1
        and max(label_counts.values(), default=0) >= (4 if render_style == "paren" else 4)
        and label_lines >= 0.5 * nonempty
    )
    captioned = (
        subtitle_cues
        and (label_lines + cue_lines) >= 0.30 * nonempty
        and (label_lines >= 2 or nonempty >= 6)
    )
    detected = bool(turns) and (labelled_transcript or single_voice or captioned)
    counts = dict(lines_nonempty=nonempty, labels_parsed=label_lines,
                  labels_rejected=rejected_labels, labels_unparseable=unparseable_labels)
    coverage = f"+unparseable({unparseable_labels})" if unparseable_labels else ""
    if not detected:
        reason = "no-speaker-labels"
        if label_lines and distinct < 2:
            reason = f"one-speaker-only({label_lines} labels)"
        elif label_lines and not repeated:
            reason = f"no-recurring-speaker({label_lines} labels)"
        elif label_lines:
            reason = f"labels-below-threshold({label_lines}/{nonempty})"
        elif cue_lines:
            reason = f"cues-below-threshold({cue_lines}/{nonempty})"
        elif unparseable_labels:
            reason = f"unparseable-labels({unparseable_labels})"
        return PrepResult(text=text, detected=False, reason=reason + coverage,
                          turns=len(turns), speakers=tuple(label_counts), **counts)
    if (unparseable_labels >= UNPARSEABLE_REFUSE_MIN
            and unparseable_labels > UNPARSEABLE_REFUSE_FRACTION * (label_lines + unparseable_labels)):
        # F2/F3: too many label lines this parser cannot read. The dialect is not
        # understood, so the document is better left exactly as it is than given
        # headers built on guessed speakers.
        return PrepResult(text=text, detected=False, turns=len(turns),
                          speakers=tuple(label_counts),
                          reason=f"unparseable-labels({unparseable_labels})", **counts)

    # Merge consecutive turns of the same speaker (blank lines and intervening
    # noise must not split one person's run of speech).
    merged: list[_Turn] = []
    for turn in turns:
        if merged and turn.speaker is not None and merged[-1].speaker == turn.speaker:
            merged[-1].words.extend(turn.words)
        else:
            merged.append(turn)
    # A turn with no words left is pure noise.
    merged = [turn for turn in merged if turn.words]
    if not merged:
        return PrepResult(text=text, detected=False, reason="no-words")

    speakers: list[str] = []
    seen: set[str] = set()
    for turn in merged:
        if turn.speaker and turn.speaker.lower() not in seen:
            seen.add(turn.speaker.lower())
            speakers.append(turn.speaker)

    title, date = _metadata_from_name(filename)
    probe = _header_line(title, date, speakers, 1, 1)
    header_tokens = estimate_tokens(probe)
    segments = _segment_turns(merged, header_every_tokens, header_tokens)

    def render(turn: _Turn) -> str:
        body = " ".join(turn.words)
        if not turn.speaker:
            return body
        if render_style == "paren":
            return f"({turn.speaker}) {body}"
        return f"{turn.speaker}: {body}"

    blocks: list[str] = []
    total = len(segments)
    for index, segment in enumerate(segments, start=1):
        header = _header_line(title, date, speakers, index, total)
        blocks.append(header + "\n" + "\n\n".join(render(turn) for turn in segment))
    prepared = "\n\n".join(blocks)

    return PrepResult(
        text=prepared,
        detected=True,
        headers_inserted=total,
        speakers=tuple(speakers),
        title=title,
        date=date,
        reason="detected"
        + ("-paren-labels" if render_style == "paren" else "")
        + ("-subtitle-cues" if subtitle_cues and not any(t.speaker for t in merged) else "")
        + coverage,
        turns=len(merged),
        header_tokens_estimate=header_tokens,
        **counts,
    )


def word_tokens(text: str, *, include_format_markers: bool = False) -> list[str]:
    """Word tokens for the reversibility check: sequences containing letters.

    Subtitle markers (``WEBVTT``, ``Kind``, ``Language``, ``NOTE``) are excluded by
    default because they are stripped with the cues; a bare timestamp is already
    excluded (no letters). This is the definition the tests use - stated here so
    the claim "every word survives" is precise rather than loose.
    """
    found = _WORD_RE.findall(text)
    if include_format_markers:
        return found
    return [word for word in found if word.lower() not in FORMAT_MARKER_WORDS]


def headers_only(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if _HEADER_RE.match(line.rstrip())]
