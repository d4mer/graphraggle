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
   a transcript (speaker-label lines or subtitle cues). When unsure, the input is
   returned unchanged with ``detected=False``. Specs, policies and notes are
   never altered.
2. Words are never changed, only placed. Turns become ``Speaker: text`` blocks,
   consecutive lines of one speaker are merged, and lines that carry no words at
   all (timestamps, subtitle cues, separators) are dropped. Original word order
   and wording are preserved exactly - no summarising, no spelling fixes, no
   normalising of garbled speech-to-text.
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

# A date in the file name, ISO-ish. Only the name is read - never the content.
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?P<date>\d{4}-\d{2}-\d{2})"),
    re.compile(r"(?P<date>\d{4}_\d{2}_\d{2})"),
    re.compile(r"(?P<date>\d{8})(?=[ _.\-]|$)"),
)

_HEADER_RE = re.compile(
    r"^\[Meeting:[^\n]*?\| Part \d+/\d+\]\s*$"
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

    @property
    def applied(self) -> bool:
        return self.detected and self.headers_inserted > 0


@dataclass
class _Turn:
    speaker: str | None
    words: list[str] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """Over-count of LightRAG's cl100k tokens (see module docstring)."""
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


def _parse_turn(line: str) -> tuple[str | None, str] | None:
    """Return ``(speaker, body)`` when the line starts a speaker turn."""
    for pattern in _TURN_PATTERNS:
        match = pattern.match(line.rstrip())
        if not match:
            continue
        name = _clean_name(match.group("name"))
        if name is None:
            continue
        body = (match.group("body") or "").strip()
        return name, body
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
    title_words = [word for word in words if word.lower() not in drop] or words
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
    Splitting happens at sentence terminators only, and only when the turn alone
    already blows the budget twice over - short turns are never broken. Every
    piece keeps the real speaker's label, so a chunk cut inside a monologue still
    says who is talking. Word order is preserved exactly; a turn with no sentence
    terminators cannot be split and is left whole (a coverage gap, not a break).
    """
    if not turn.words:
        return [turn]
    total = estimate_tokens(" ".join(turn.words))
    if total <= 2 * max(32, budget_tokens):
        return [turn]
    pieces: list[list[str]] = []
    current: list[str] = []
    for word in turn.words:
        current.append(word)
        if word.endswith((".", "!", "?", "\u2026")) and estimate_tokens(" ".join(current)) >= budget_tokens:
            pieces.append(current)
            current = []
    if current:
        pieces.append(current)
    if len(pieces) <= 1:
        return [turn]
    return [(_Turn(speaker=turn.speaker, words=piece))
            for index, piece in enumerate(pieces)]


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
    label_counts: dict[str, int] = {}
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
            speaker, body = parsed
            label_lines += 1
            label_counts[speaker.lower()] = label_counts.get(speaker.lower(), 0) + 1
            if body:
                turns.append(_Turn(speaker=speaker, words=body.split()))
            else:
                turns.append(_Turn(speaker=speaker))
            continue
        if _is_noise(line):
            continue
        if turns:
            turns[-1].words.extend(line.split())
        else:
            turns.append(_Turn(speaker=None, words=line.split()))

    subtitle_cues = cue_lines >= 3
    distinct = len(label_counts)
    repeated = any(count >= 2 for count in label_counts.values())
    # Detection is deliberately conservative; a document that fails any of these
    # is handed to LightRAG untouched.
    labelled_transcript = (
        label_lines >= 3
        and distinct >= 2
        and repeated
        and label_lines >= 0.30 * nonempty
    )
    # One voice, repeated enough to be a recording rather than a labelled list.
    single_voice = (
        distinct == 1
        and max(label_counts.values(), default=0) >= 4
        and label_lines >= 0.5 * nonempty
    )
    captioned = (
        subtitle_cues
        and (label_lines + cue_lines) >= 0.30 * nonempty
        and (label_lines >= 2 or nonempty >= 6)
    )
    detected = bool(turns) and (labelled_transcript or single_voice or captioned)
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
        return PrepResult(text=text, detected=False, reason=reason, turns=len(turns),
                          speakers=tuple(label_counts))

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

    blocks: list[str] = []
    total = len(segments)
    for index, segment in enumerate(segments, start=1):
        header = _header_line(title, date, speakers, index, total)
        rendered = [
            (f"{turn.speaker}: " if turn.speaker else "") + " ".join(turn.words)
            for turn in segment
        ]
        blocks.append(header + "\n" + "\n\n".join(rendered))
    prepared = "\n\n".join(blocks)

    return PrepResult(
        text=prepared,
        detected=True,
        headers_inserted=total,
        speakers=tuple(speakers),
        title=title,
        date=date,
        reason="detected"
        + ("-subtitle-cues" if subtitle_cues and not any(t.speaker for t in merged) else ""),
        turns=len(merged),
        header_tokens_estimate=header_tokens,
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
