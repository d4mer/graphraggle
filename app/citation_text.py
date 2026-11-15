"""Reading chunk text out of a LightRAG reference, in one place.

Why this exists
---------------
Four readers walk the same reference dicts and they did not agree. ``app/rerank.py``,
``app/graphrag.py`` and (for the generation prompt) ``app/generation.py`` accept the four field
names upstream has been seen to use, and accept a LIST of strings as well as a string. The M1
grounding gate read only ``content``, and only when it was a string — so a response whose text
arrived under ``text`` or as a list produced an evidence string of ``""`` and a verdict of
``no_evidence`` for a question the model had every reason to answer. The narrowest reader in the
repo was deciding whether the store supports an answer.

One tuple and one accessor means they cannot drift again; ``tests/test_grounding_gate.py`` pins the
tuple and pins both readers' behaviour on a reference that carries text only under an alternative
key.

Field names come from what upstream actually sends: ``include_chunk_content`` is what the gateway
asks for, and LightRAG's response shape has appeared as ``content`` (list of chunk strings in the
generation path, string elsewhere). The alternatives are what other gateway callers already accept.
"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

# Order matters: `content` is what the gateway asks LightRAG for; the rest are tolerated because
# every other reader in this repo already tolerates them.
CITATION_TEXT_KEYS: tuple[str, ...] = ("content", "text", "chunk_text", "body")


def citation_content(citation: Any, keys: Sequence[str] = CITATION_TEXT_KEYS) -> str | None:
    """The text of one reference, or ``None`` when it carries none.

    A string value is used as-is (whitespace trimmed). A list value is joined with a blank line,
    which is what ``app/generation.py`` does when it builds a prompt from ``content``. Empty and
    whitespace-only values count as no text: a reference that carries an empty list is not evidence,
    and treating it as evidence would make "supported" cheaper to reach than "not supported".
    """
    if not isinstance(citation, dict):
        return None
    for key in keys:
        value = citation.get(key)
        if isinstance(value, str):
            if value.strip():
                return value.strip()
        elif isinstance(value, list):
            parts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
            if parts:
                return "\n\n".join(parts)
    return None


def citation_content_shape(citation: Any, keys: Sequence[str] = CITATION_TEXT_KEYS) -> str:
    """What kind of content this reference carries, for a count that can go in a log line.

    ``"string"`` / ``"list"``: text found, under which kind of value.
    ``"empty"``: at least one of the known keys is present but carries no text — the shape that
    turns a grounding verdict into a false refusal, so it is worth distinguishing from absent.
    ``"absent"``: none of the known keys is present (or none holds a string or list).
    """
    if not isinstance(citation, dict):
        return "absent"
    saw_known_key = False
    for key in keys:
        value = citation.get(key)
        if isinstance(value, str):
            saw_known_key = True
            if value.strip():
                return "string"
        elif isinstance(value, list):
            saw_known_key = True
            if any(isinstance(item, str) and item.strip() for item in value):
                return "list"
    return "empty" if saw_known_key else "absent"


def content_shape_counts(citations: Iterable[Any],
                         keys: Sequence[str] = CITATION_TEXT_KEYS) -> dict[str, int]:
    """Per-shape counts over a reference list. All four shapes are always present as keys, so a
    log line can be compared across runs without guessing which shapes were missing."""
    counts = {"string": 0, "list": 0, "empty": 0, "absent": 0}
    for citation in citations or []:
        counts[citation_content_shape(citation, keys)] += 1
    return counts
