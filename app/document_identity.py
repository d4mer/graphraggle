"""One join from a citation to the store rows that own it.

A citation carries a name and nothing else. LightRAG rewrites that name at store time: it strips
directories from a `/documents/text` `file_source` and keeps the upload name for
`/documents/upload`, so the `file_path` that comes back on a citation is a **bare basename** while
`documents.path` is absolute. An exact-path join therefore matches nothing on the filesystem door, and
that is not hypothetical: on stack2 R1/R2 every citation was unattributable (1046 of them, kept 0), and
production's `doc_status` values are bare filenames too.

So the join is ordered and explicit, and it is the same for every door:

1. ``submitted_source`` - the exact name this gateway sent upstream, when it recorded one. This is the
   only key that cannot drift, and it is the one a company-qualified ingest populates.
2. exact ``path`` (upload-door citations and tests), then exact ``filename`` / ``original_filename``.
3. the **basename** of the citation, matched against those same columns and against the tail of
   ``path`` - what makes an existing index attributable without re-ingesting.
4. the shared content hash, which widens a legitimately shared file to all of its owners rather than
   the one company the index happened to keep.

A basename can match several rows. The rule is stated where it can be tested, not assumed: if those
rows agree on the bytes (same ``sha256``), or belong to only one company, the citation is owned by all
of them; if they hold **different bytes for different companies**, the citation is genuinely ambiguous
and is never attributed to anyone - it is counted ``ambiguous`` and left out. Guessing here is how a
cross-company answer gets credited to the company the user asked about.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Iterable, Sequence

from . import state_store

# The names a citation may carry a document under. These are the four app/api.py already tolerates;
# no fifth is invented, because a wrong guess here is silent (the citation simply looks unowned).
PATH_KEYS: tuple[str, ...] = ("file_source", "path", "source", "file_path")


def citation_path(citation: Any) -> str:
    if not isinstance(citation, dict):
        return ""
    for key in PATH_KEYS:
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def company_slug(company: str) -> str:
    """A filesystem-safe, joinable form of a company name.

    Names arrive with spaces, ampersands and accents; LightRAG keeps whatever name it is given and the
    join has to reproduce it exactly, so the qualified name is built from one rule, in one place.
    """
    slug = "".join(character.lower() if character.isalnum() else "-" for character in company)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def qualified_submitted_source(path_name: str, company: str | None) -> str:
    """The name to submit upstream so two companies cannot share one stored basename.

    LightRAG strips directories, so the name it keeps for a filesystem document is the file's own
    basename - and `Contoso Pharma/q3.pdf` and `Northwind Chemicals/q3.pdf` arrive at the query side as
    the same indistinguishable `q3.pdf`. Prefixing the submitted name with the company makes the name
    LightRAG keeps unique, which is the only way the join stops being a guess for NEW ingests. Existing
    documents are unaffected (they keep resolving through the basename rule) and nothing about the
    bytes, the chunking or the state row's path changes - only the label upstream stores.
    """
    if not company:
        return path_name
    slug = company_slug(company)
    if not slug:
        return path_name
    return f"{slug}--{path_name}"


def basename_of(path: str) -> str:
    """Last path segment, for both POSIX and Windows-style separators.

    LightRAG keeps whatever it was given after stripping directories, and a gateway on another host may
    have submitted a Windows path. Taking the segment after either separator avoids attributing a
    citation to nothing because of a separator the store never used.
    """
    name = path.replace("\\", "/").rstrip("/").split("/")[-1]
    return name


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


@dataclass
class Ownership:
    """What the store can prove about one citation name."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    companies: set[str] = field(default_factory=set)
    via: str = "none"            # submitted_source | path | filename | basename | none
    ambiguous: bool = False
    content_hashes: tuple[str, ...] = ()


def _row_value(row: dict[str, Any], key: str) -> str:
    value = row.get(key)
    return str(value).strip() if value is not None else ""


def _matched_by(path: str, rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    """Rows equal to this exact citation name, and which key proved it (best key wins)."""
    matched: list[dict[str, Any]] = []
    via = "none"
    order = ("submitted_source", "path", "filename", "original_filename")
    for row in rows:
        hits = [key for key in order if _row_value(row, key) == path]
        if not hits:
            continue
        matched.append(row)
        key = hits[0]
        if via == "none" or order.index(key) < order.index(via):
            via = key
    return matched, via


def _basename_matched(path: str, basename: str,
                      rows: Sequence[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    matched: list[dict[str, Any]] = []
    via = "none"
    for row in rows:
        for key in ("submitted_source", "filename", "original_filename"):
            if _row_value(row, key) == basename:
                matched.append(row)
                via = key
                break
        else:
            stored_path = _row_value(row, "path")
            if stored_path and basename_of(stored_path) == basename:
                matched.append(row)
                if via == "none":
                    via = "basename"
    # A row can match on two keys; keep each row once.
    unique: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in matched:
        key = _row_value(row, "document_id") or _row_value(row, "path")
        if key not in seen:
            seen.add(key)
            unique.append(row)
    return unique, via


def _hashes(rows: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    return tuple(sorted({value for row in rows
                         if (value := _row_value(row, "sha256")) and value != "pending"}))


def _companies(rows: Sequence[dict[str, Any]]) -> set[str]:
    return {value for row in rows if (value := _row_value(row, "company"))}


def decide_ownership(matched: Sequence[dict[str, Any]], via: str,
                     extra_rows: Sequence[dict[str, Any]] = ()) -> Ownership:
    """The basename rule, stated once.

    Different bytes under one name for different companies is the only case that cannot be decided from
    a citation, so it is the only case that refuses. Everything else attributes to the union, and the
    content-hash widening is applied to that union.
    """
    hashes = _hashes(matched)
    companies = _companies(matched)
    if len(hashes) > 1 and len(companies) > 1:
        # Genuinely ambiguous: the name alone cannot say which company's document this was. No
        # company is claimed, and the caller counts it as `ambiguous` rather than as "no owner".
        return Ownership(rows=list(matched), companies=set(), via=via, ambiguous=True,
                         content_hashes=hashes)
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in list(matched) + list(extra_rows):
        key = _row_value(row, "document_id") or _row_value(row, "path")
        if key not in seen:
            seen.add(key)
            merged.append(row)
    return Ownership(rows=merged, companies=_companies(merged), via=via,
                     content_hashes=hashes + _hashes(extra_rows))


async def ownership_for_paths(paths: Iterable[str]) -> dict[str, Ownership]:
    """Resolve citation names to store rows: submitted name, exact path, basename, then hash.

    All SQL lives in app/state_store.py so every reader gets annotated rows (`query_ready`,
    `readiness_reason`) and one place to change.
    """
    wanted = [path for path in dict.fromkeys(paths) if path]
    if not wanted:
        return {}

    exact_rows = await state_store.get_documents_by_exact_names(wanted)
    unresolved = [path for path in wanted if not _matched_by(path, exact_rows)[0]]
    basename_rows = (await state_store.get_documents_by_basenames(
        [name for name in dict.fromkeys(basename_of(path) for path in unresolved) if name])
        if unresolved else [])

    ownership: dict[str, Ownership] = {}
    for path in wanted:
        matched, via = _matched_by(path, exact_rows)
        if not matched:
            matched, via = _basename_matched(path, basename_of(path), basename_rows)
        if not matched:
            ownership[path] = Ownership()
            continue
        # Widen by content hash: with COMPANY_AWARE_DEDUPE (the production state) identical bytes are
        # indexed once, so the citation carries one company's name while both companies own the file.
        extra = [row for content_hash in _hashes(matched)
                 for row in await state_store.get_documents_by_sha256(content_hash)]
        ownership[path] = decide_ownership(matched, via, extra)
    return ownership
