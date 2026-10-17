from __future__ import annotations

import asyncio
import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .lightrag_client import client
from .pptx_extract import PptxExtractError, cap_extracted_text, extract_pptx_text
from .state_store import (
    build_company_attribution,
    get_document_by_path,
    get_documents_by_sha256,
    increment_retry,
    init_db,
    normalize_company,
    upsert_document,
    update_document_state,
)
from .structure_chunker import (
    DEFAULT_BUDGET_TOKENS,
    DEFAULT_HEADER_TOKENS,
    chunk_document,
    render_for_lightrag,
)
from .transcript_prep import DEFAULT_HEADER_EVERY_TOKENS, prepare_transcript
from .validation import validate_file

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".html", ".htm"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".json", ".csv"}
# packet-27: .pptx is NOT a TEXT_EXTENSION (that means "decode the bytes as
# text"). It is extracted with python-pptx into structured text and then
# submitted through the same /documents/text path.
PPTX_EXTENSIONS = {".pptx"}
TERMINAL_SUCCESS = {"ingested"}
LIGHTRAG_INTERNAL_DIRS = {"__enqueued__"}

# macOS / Windows metadata sidecar files that should never be ingested.
# AppleDouble: "._" prefix on macOS preserves resource forks in non-APFS
# filesystems; they share the suffix of the real file (e.g. "._foo.txt"),
# so the extension check would otherwise let them through.
# .DS_Store and Thumbs.db are directory metadata written by Finder/Explorer.
METADATA_FILE_BASENAMES = frozenset({
    ".DS_Store",
    "._.DS_Store",
    "Thumbs.db",
    "._Thumbs.db",
    "desktop.ini",
})
METADATA_FILE_PREFIXES = ("._",)
# Suffixes that are never ingestion input whichever door they arrive through.
# /upload stages the stream inside UPLOADS_DIR as ".<document_id>.part" and only
# renames it into place once the body is complete.
INGEST_SKIP_SUFFIXES = {".part"}
ACTIVE_DUPLICATE_STATUSES = {"accepted", "submitted", "processing", "ingested"}
NON_RETRYABLE_FAILURE_CODES = {
    "decode_failed",
    "lightrag_processing_failed",
    "submit_terminal_failed",
    "track_status_failed",
}
TRANSIENT_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
TRANSIENT_EXCEPTION_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RequestError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}
METADATA_UNSET = object()


def warn(message: str) -> None:
    import sys

    print(f"WARNING: {message}", file=sys.stderr)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def is_metadata_file(path: Path) -> bool:
    """Return True if path is a metadata sidecar that should be ignored.

    Catches macOS AppleDouble (``._`` prefix) and OS-generated junk
    (``.DS_Store``, ``Thumbs.db``, ``desktop.ini``). Used by both the
    filesystem scanner and the validation gate.
    """
    name = path.name
    if name in METADATA_FILE_BASENAMES:
        return True
    return name.startswith(METADATA_FILE_PREFIXES)


def should_skip_path(path: Path, root: Path, source_type: str) -> bool:
    # Always-on rules, for both doors. An in-flight upload temp file has no
    # supported suffix, so a scan landing mid-stream used to write a `rejected`
    # row (unsupported_extension) for a path that then vanished on rename.
    # Metadata sidecars are junk whatever the door; validate_upload already
    # rejects them at the upload door, this stops the scanner inventing a row.
    if path.suffix.lower() in INGEST_SKIP_SUFFIXES:
        return True
    if is_metadata_file(path):
        return True
    if source_type != "filesystem":
        return False
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in LIGHTRAG_INTERNAL_DIRS for part in relative_parts)


def infer_company_from_path(path: Path, root: Path, source_type: str) -> tuple[str | None, str, str]:
    if source_type != "filesystem":
        return build_company_attribution()
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return build_company_attribution()
    inferred_company = normalize_company(relative_parts[0]) if len(relative_parts) > 1 else None
    return build_company_attribution(inferred_company=inferred_company)


async def refresh_company_attribution(path: Path, source_type: str, root: Path, existing: dict[str, Any]) -> dict[str, Any]:
    if source_type != "filesystem":
        return existing
    company, company_source, company_source_detail = infer_company_from_path(path, root, source_type)
    if (
        existing.get("company") == company
        and existing.get("company_source") == company_source
        and existing.get("company_source_detail") == company_source_detail
    ):
        return existing

    await update_document_state(
        str(path),
        company=company,
        company_source=company_source,
        company_source_detail=company_source_detail,
    )
    return await get_document_by_path(str(path)) or existing


def read_text_with_fallbacks(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def normalize_text_submission_payload(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(r"[ \t]+\n", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    normalized = re.sub(r"\[(?:\d{1,2}:)?\d{1,2}:\d{2}\]\s*", "", normalized)
    normalized = re.sub(r"(?:^|\n)(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*", "\n", normalized)
    normalized = re.sub(r"\n{3,}", "\n\n", normalized)
    return normalized.strip()


def prepare_transcript_payload(path: Path, text: str) -> tuple[str, dict[str, Any]]:
    """Optional transcript preparation, behind ``TRANSCRIPT_PREP_ENABLED``.

    Returns the payload to submit and the worker metadata to record on the row, so
    a later reindex can tell a prepared document from an unprepared one. Detection
    is conservative inside :mod:`app.transcript_prep`: text that is not
    transcript-shaped comes back byte-identical with ``detected=False``. The
    content hash used for dedupe is never taken from this text - it is computed
    from the file bytes before this call.
    """
    if not settings.transcript_prep_enabled:
        return text, {}
    result = prepare_transcript(
        text,
        filename=path.name,
        header_every_tokens=int(
            settings.transcript_prep_header_every_tokens or DEFAULT_HEADER_EVERY_TOKENS
        ),
    )
    if result.applied:
        return result.text, {
            "transcript_prep": "applied",
            "transcript_prep_headers": result.headers_inserted,
            "transcript_prep_turns": result.turns,
            "transcript_prep_speakers": list(result.speakers),
            "transcript_prep_reason": None,
            # Counts only, never text: how much of the file the label parser read,
            # and how much of it it could not. F3 - a skipped document used to be
            # un-diagnosable from the row alone.
            "transcript_prep_lines": result.lines_nonempty,
            "transcript_prep_labels_parsed": result.labels_parsed,
            "transcript_prep_labels_rejected": result.labels_rejected,
            "transcript_prep_labels_unparseable": result.labels_unparseable,
        }
    return text, {
        "transcript_prep": "skipped",
        "transcript_prep_headers": None,
        "transcript_prep_turns": None,
        "transcript_prep_speakers": None,
        "transcript_prep_reason": result.reason,
        "transcript_prep_lines": result.lines_nonempty,
        "transcript_prep_labels_parsed": result.labels_parsed,
        "transcript_prep_labels_rejected": result.labels_rejected,
        "transcript_prep_labels_unparseable": result.labels_unparseable,
    }


def prepare_structure_payload(
    path: Path, text: str
) -> tuple[str, dict[str, Any] | None, dict[str, Any]]:
    """Optional structure-aware chunking, behind ``STRUCTURE_CHUNKING_ENABLED``.

    Returns ``(payload, chunking_params, worker_metadata)``. When the flag is off the
    payload comes back byte-identical with no params, so the request body is exactly
    what it is today. When it is on, :mod:`app.structure_chunker` chooses every cut and
    the params tell LightRAG to cut ONLY there (``fixed_token`` +
    ``split_by_character_only``) - the mechanism 1.5.7 offers instead of a custom
    chunker, which it does not accept.

    Nothing here can damage a document: if the planner raises, or produces no pieces,
    the ORIGINAL text is returned with the reason recorded, and the request goes out
    without chunking params. The dedupe hash is taken from the file bytes before this
    call, so chunking can never change a document's identity. Metadata is counts and
    names only - no piece text, no corpus words - exactly as transcript_prep records.
    """
    if not settings.structure_chunking_enabled:
        return text, None, {}
    try:
        plan = chunk_document(
            text,
            filename=path.name,
            budget_tokens=int(settings.structure_chunking_budget_tokens or DEFAULT_BUDGET_TOKENS),
            header_tokens=int(settings.structure_chunking_header_tokens or DEFAULT_HEADER_TOKENS),
            headers=bool(settings.structure_chunking_headers),
        )
        if plan.empty:
            raise ValueError("chunk plan had no pieces")
        payload, params = render_for_lightrag(plan)
    except Exception as exc:  # the ingest must never depend on this module
        return text, None, {
            "structure_chunking": "failed",
            "structure_chunking_reason": f"{type(exc).__name__}: {str(exc)[:160]}",
        }
    return payload, params, {
        "structure_chunking": "applied",
        "structure_chunking_kind": plan.kind_detected,
        "structure_chunking_pieces": len(plan.pieces),
        "structure_chunking_budget_tokens": plan.stats.get("budget_tokens"),
        "structure_chunking_tokens_p50": plan.stats.get("tokens_p50"),
        "structure_chunking_tokens_p95": plan.stats.get("tokens_p95"),
        "structure_chunking_tokens_max": plan.stats.get("tokens_max"),
        "structure_chunking_dropped_lines": plan.stats.get("dropped_running_lines", 0),
        "structure_chunking_repeated_words": plan.stats.get("repeated_words", 0),
        "structure_chunking_tokenizer": plan.stats.get("tokenizer"),
        "structure_chunking_warnings": list(plan.warnings),
        "structure_chunking_reason": None,
    }


def uses_pptx_extract(path: Path) -> bool:
    """True when this file is extracted by the worker instead of uploaded."""
    return bool(settings.worker_pptx_extract_enabled) and path.suffix.lower() in PPTX_EXTENSIONS


def extract_pptx_payload(path: Path) -> str:
    """Byte-guard, extract, normalise and cap a .pptx submission payload.

    Raises :class:`PptxExtractError` with the worker ``error_code``. python-pptx
    loads the whole package into memory, so ``WORKER_PPTX_MAX_BYTES`` is the
    guard against the zip-bomb class of files.
    """
    limit = int(settings.worker_pptx_max_bytes or 0)
    if limit:
        size = path.stat().st_size
        if size > limit:
            raise PptxExtractError(
                f"PPTX too large to extract: {size} bytes exceeds WORKER_PPTX_MAX_BYTES={limit}",
                error_code="pptx_extract_failed",
            )
    text = normalize_text_submission_payload(extract_pptx_text(path))
    if not text.strip():
        raise PptxExtractError(
            "PPTX extraction produced no text; nothing to submit",
            error_code="pptx_empty",
        )
    return cap_extracted_text(text, int(settings.worker_pptx_max_chars or 0) or None)


def parse_warning_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"legacy_warning": raw}
    return parsed if isinstance(parsed, dict) else {}


def merge_warning_metadata(
    raw: str | None,
    *,
    duplicate_of: dict[str, Any] | None | object = METADATA_UNSET,
    worker_updates: dict[str, Any] | None = None,
) -> str:
    meta = parse_warning_metadata(raw)
    if duplicate_of is not METADATA_UNSET:
        if duplicate_of is None:
            meta.pop("duplicate_of", None)
        else:
            meta["duplicate_of"] = duplicate_of
    if worker_updates:
        worker = meta.get("worker")
        if not isinstance(worker, dict):
            worker = {}
        for key, value in worker_updates.items():
            if value is None:
                worker.pop(key, None)
            else:
                worker[key] = value
        if worker:
            meta["worker"] = worker
        else:
            meta.pop("worker", None)
    return json.dumps(meta, sort_keys=True)


def is_reindex_requested(doc: dict[str, Any] | None) -> bool:
    if not doc:
        return False
    worker = parse_warning_metadata(doc.get("warnings_json")).get("worker")
    return isinstance(worker, dict) and bool(worker.get("reindex_requested"))


def cleared_reindex_updates(worker_updates: dict[str, Any] | None = None) -> dict[str, Any]:
    updates = {
        "reindex_requested": None,
        "reindex_force": None,
        "requested_from_status": None,
        "requested_from_validation_state": None,
    }
    if worker_updates:
        updates.update(worker_updates)
    return updates


def build_duplicate_reference(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": doc.get("document_id"),
        "path": doc.get("path"),
        "original_filename": doc.get("original_filename"),
        "company": doc.get("company"),
        "status": doc.get("status"),
        "validation_state": doc.get("validation_state"),
    }


def pick_duplicate_candidate(
    documents: list[dict[str, Any]],
    current_path: str,
    *,
    company: str | None = None,
) -> dict[str, Any] | None:
    """Pick the active row whose SHA-256 matches, or None if nothing suppresses it.

    Company scoping is applied only when ``COMPANY_AWARE_DEDUPE`` is on; with it
    off the result is exactly what it always was, whatever ``company`` says. When
    it is on, a candidate may suppress the new document only if the new document
    is unscoped (``company`` NULL - unscoped queries see every company's
    citations, so the content is still reachable) or the candidate belongs to the
    same company. A scoped new document is never suppressed by another company's
    row or by an unscoped row: its citations would be attributed to that other
    row by path (``enrich_citation`` / ``scope_query_citations`` in api.py), so
    suppressing it lost the document silently.

    The comparison is ``normalize_company(a) == normalize_company(b)`` - strip
    only, deliberately NO case folding, mirroring ``scope_query_citations``,
    which compares the raw stored company against a stripped request. "Acme Corp"
    and "acme corp" are different companies everywhere in this stack. If that
    ever changes it must change in both places in the same commit: folding case
    only here would submit documents whose citations are then filtered out at
    query time.
    """
    if settings.company_aware_dedupe:
        new_company = normalize_company(company)
        if new_company is not None:
            documents = [
                doc for doc in documents
                if normalize_company(doc.get("company")) == new_company
            ]
    rank = {"ingested": 0, "processing": 1, "submitted": 2, "accepted": 3}
    candidates = [
        doc
        for doc in documents
        if doc.get("path") != current_path
        and doc.get("status") in ACTIVE_DUPLICATE_STATUSES
        and doc.get("error_code") != "duplicate_content"
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda doc: (
            rank.get(str(doc.get("status")), 99),
            str(doc.get("updated_at", "")),
            str(doc.get("path", "")),
            str(doc.get("document_id", "")),
        ),
        reverse=False,
    )
    duplicate_of = build_duplicate_reference(candidates[0])
    duplicate_of["match_count"] = len(candidates)
    duplicate_of["match_paths"] = sorted(str(doc.get("path", "")) for doc in candidates)
    duplicate_of["selection_basis"] = "sha256_status_updated_at_path"
    return duplicate_of


def cross_company_guard_updates(match: dict[str, Any] | None) -> dict[str, Any]:
    """worker_updates for the cross-company marker.

    Set when ``COMPANY_AWARE_DEDUPE`` let a document through that the company-blind
    picker would have suppressed (same SHA, different company). It is the guard's
    blast radius: only those rows are checked in ``poll_track``. Pass None to
    clear it.
    """
    if match is None:
        return {
            "cross_company_guard": None,
            "cross_company_duplicate_path": None,
            "cross_company_duplicate_company": None,
        }
    return {
        "cross_company_guard": "verify_file_path_on_process",
        "cross_company_duplicate_path": match.get("path"),
        "cross_company_duplicate_company": match.get("company"),
    }


def cross_company_guard_mismatch(
    raw: str | None,
    submitted_path: str,
    docs: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """For marked rows only: did upstream actually index OUR path?

    Returns None when there is no marker (every unmarked row keeps today's path
    through ``poll_track`` untouched) or when a returned ``documents[].file_path``
    matches what we submitted. Otherwise returns the evidence for a visible
    failure. A same-content second submit may be deduplicated upstream while the
    track still reports PROCESSED; that would otherwise mark our row
    ``ingested``/``query_ready`` while no chunk carries our path, i.e. a healthy
    row that can never be cited. ``file_path`` is compared exactly, and by
    basename as a fallback because ``/documents/upload`` tags the uploaded
    filename rather than the path we handed it.
    """
    worker = parse_warning_metadata(raw).get("worker")
    if not isinstance(worker, dict) or worker.get("cross_company_guard") != "verify_file_path_on_process":
        return None
    submitted = Path(submitted_path)
    returned = sorted(
        str(doc.get("file_path")) for doc in docs if isinstance(doc.get("file_path"), str)
    )
    for value in returned:
        if value == str(submitted) or Path(value).name == submitted.name:
            return None
    return {
        "expected_path": str(submitted),
        "returned_paths": returned,
        "suppressed_by_path": worker.get("cross_company_duplicate_path"),
        "suppressed_by_company": worker.get("cross_company_duplicate_company"),
    }


def stale_track_worker_updates(track_id: str, reason: str) -> dict[str, Any]:
    return {
        "failure_class": "transient",
        "retryable": True,
        "retry_reason": "stale_track_id",
        "last_polled_track_id": track_id,
        "stale_track_id": track_id,
        "stale_track_marker": True,
        "stale_track_reason": reason,
        "track_id_policy": "clear_and_resubmit",
        "track_poll_payload": None,
    }


def response_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_transient_exception(exc: Exception) -> bool:
    status_code = response_status_code(exc)
    if status_code in TRANSIENT_HTTP_STATUS_CODES:
        return True
    return exc.__class__.__name__ in TRANSIENT_EXCEPTION_NAMES


def is_retryable_failure(doc: dict[str, Any]) -> bool:
    error_code = doc.get("error_code")
    return bool(error_code) and error_code not in NON_RETRYABLE_FAILURE_CODES


async def mark_duplicate_candidate(path: Path, digest: str, duplicate_of: dict[str, Any], existing: dict[str, Any] | None) -> None:
    warnings_json = merge_warning_metadata(
        existing.get("warnings_json") if existing else None,
        duplicate_of=duplicate_of,
        worker_updates=cleared_reindex_updates({
            "dedupe_basis": duplicate_of.get("selection_basis"),
            "dedupe_action": "skip_duplicate_content",
            "duplicate_match_count": duplicate_of.get("match_count"),
            "duplicate_match_paths": duplicate_of.get("match_paths"),
            "duplicate_status": duplicate_of.get("status"),
            "last_submitted_track_id": None,
            "cross_company_guard": None,
            "cross_company_duplicate_path": None,
            "cross_company_duplicate_company": None,
            "stale_track_id": None,
            "stale_track_marker": None,
            "stale_track_reason": None,
            "track_id_policy": None,
            "track_poll_payload": None,
        }),
    )
    await update_document_state(
        str(path),
        sha256=digest,
        status="accepted",
        validation_state="warn",
        error_stage="validation",
        error_code="duplicate_content",
        error_message="Duplicate content already represented by another document",
        error_detail=(
            f"Duplicate content matches {duplicate_of.get('path')} "
            f"among {duplicate_of.get('match_count')} active SHA matches"
        ),
        last_error=None,
        track_id=None,
        query_ready=False,
        content_hash=digest,
        byte_size=path.stat().st_size,
        ingested_at=None,
        retry_count=0,
        warnings_json=warnings_json,
    )
    warn(f"Skipping duplicate content for {path}: {duplicate_of.get('path')}")


async def submit_file(path: Path, source_type: str, digest: str, *, retry_reason: str | None = None) -> None:
    existing = await get_document_by_path(str(path))
    company = existing.get("company") if existing else None
    original_filename = existing.get("original_filename") if existing else None
    filename = existing.get("filename") if existing else None
    byte_size = path.stat().st_size

    verdict = validate_file(path, sha256=digest, file_size=byte_size)
    warnings_json = existing.get("warnings_json") if existing else None

    if verdict.verdict == "reject":
        await update_document_state(
            str(path),
            sha256=digest,
            status="rejected",
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            last_error=verdict.meta.get("error_message"),
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                worker_updates=cleared_reindex_updates(),
            ),
        )
        warn(f"Validation rejected {path}: {verdict.meta.get('error_code')}")
        return

    if verdict.verdict == "auto_split":
        await update_document_state(
            str(path),
            sha256=digest,
            status="accepted",
            validation_state="auto_split",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates=cleared_reindex_updates({
                    "dedupe_action": None,
                    "submission_blocked": "auto_split",
                    "duplicate_match_count": None,
                    "duplicate_match_paths": None,
                    "duplicate_status": None,
                }),
            ),
        )
        warn(f"Auto-split flagged for {path}: {verdict.meta.get('error_code')}")
        return

    detected_encoding = None
    text_payload = None
    prep_updates: dict[str, Any] = {}
    structure_params: dict[str, Any] | None = None
    structure_updates: dict[str, Any] = {}
    if path.suffix.lower() in TEXT_EXTENSIONS:
        try:
            text_payload, detected_encoding = read_text_with_fallbacks(path)
            text_payload = normalize_text_submission_payload(text_payload)
            text_payload, prep_updates = prepare_transcript_payload(path, text_payload)
            # Order matters: transcript_prep labels turns and inserts its headers,
            # then the structure chunker decides where the cuts fall in that text.
            # Binary uploads (.pdf/.docx) never reach here: LightRAG parses those.
            text_payload, structure_params, structure_updates = prepare_structure_payload(
                path, text_payload
            )
        except Exception as exc:
            await update_document_state(
                str(path),
                sha256=digest,
                status="failed",
                validation_state=existing.get("validation_state") if existing else "accept",
                error_stage="decode",
                error_code="decode_failed",
                error_message="Worker failed to decode text content",
                error_detail=str(exc),
                last_error=str(exc),
                track_id=None,
                query_ready=False,
                content_hash=digest,
                byte_size=byte_size,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    duplicate_of=None,
                    worker_updates=cleared_reindex_updates({
                        "dedupe_action": None,
                        "failure_class": "terminal",
                        "submission_blocked": None,
                        "retryable": False,
                    }),
                ),
            )
            return
    elif uses_pptx_extract(path):
        # packet-27: extract the deck (slides, tables, groups, notes) and submit
        # it as text. LightRAG's own pptx extractor drops all of that.
        try:
            text_payload = extract_pptx_payload(path)
        except PptxExtractError as exc:
            await update_document_state(
                str(path),
                sha256=digest,
                status="failed",
                validation_state=existing.get("validation_state") if existing else "accept",
                error_stage="extract",
                error_code=exc.error_code,
                error_message="Worker failed to extract PPTX content",
                error_detail=str(exc),
                last_error=str(exc),
                track_id=None,
                query_ready=False,
                content_hash=digest,
                byte_size=byte_size,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    duplicate_of=None,
                    worker_updates=cleared_reindex_updates({
                        "dedupe_action": None,
                        "failure_class": "terminal",
                        "submission_blocked": None,
                        "retryable": False,
                    }),
                ),
            )
            warn(f"PPTX extraction failed for {path}: {exc.error_code}: {exc}")
            return

    if verdict.verdict == "warn":
        warnings_json = merge_warning_metadata(
            warnings_json,
            duplicate_of=None,
            worker_updates=cleared_reindex_updates({
                "dedupe_action": None,
                "validation_warning": verdict.meta.get("error_code"),
                "detected_encoding": detected_encoding,
                "submission_blocked": None,
            }),
        )
        await update_document_state(
            str(path),
            sha256=digest,
            validation_state="warn",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            warnings_json=warnings_json,
        )
        warn(f"Validation warning for {path}: {verdict.meta.get('error_code')}")
    else:
        warnings_json = merge_warning_metadata(
            warnings_json,
            duplicate_of=None,
            worker_updates=cleared_reindex_updates({
                "dedupe_action": None,
                "detected_encoding": detected_encoding,
                "submission_blocked": None,
            }),
        )

    if prep_updates:
        # Recorded before the HTTP call so both the success upsert and the failure
        # write-backs carry it: "was this document submitted prepared?"
        warnings_json = merge_warning_metadata(warnings_json, worker_updates=prep_updates)
    if structure_updates:
        # Same reason, one step later: "was this document submitted pre-chunked, and
        # what did the plan look like?" Counts and names only, never text.
        warnings_json = merge_warning_metadata(warnings_json, worker_updates=structure_updates)

    try:
        if text_payload is not None:
            body: dict[str, Any] = {
                "text": text_payload,
                "file_source": str(path),
            }
            if structure_params is not None:
                # With the flag off this key is absent, so the request body is
                # byte-identical to what it was before the feature existed.
                body["chunking"] = structure_params
            resp = await client.post_json("/documents/text", body)
        else:
            resp = await client.post_file("/documents/upload", str(path))
    except Exception as exc:
        retryable = is_transient_exception(exc)
        error_code = "submit_transient_failed" if retryable else "submit_terminal_failed"
        error_message = (
            "Transient LightRAG submit failure; worker will retry"
            if retryable
            else "Terminal LightRAG submit failure"
        )
        # CAS: the failure verdict describes OUR attempt, which only stands while
        # the row is still the "accepted" one this scan wrote. If the API (an
        # operator reindex) moved the row in the meantime, that newer state wins
        # and this write is dropped with a log line - otherwise the stale
        # warnings_json copy here would erase the operator's reindex marker.
        await update_document_state(
            str(path),
            expect_status="accepted",
            sha256=digest,
            status="failed",
            validation_state="warn" if verdict.verdict == "warn" else "accept",
            error_stage="submit",
            error_code=error_code,
            error_message=error_message,
            error_detail=str(exc),
            last_error=str(exc),
            track_id=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates=cleared_reindex_updates({
                    "dedupe_action": None,
                    "failure_class": "transient" if retryable else "terminal",
                    "retryable": retryable,
                    "retry_reason": retry_reason,
                    "submission_blocked": None,
                }),
            ),
        )
        return

    track_id = resp.get("track_id") or None
    if not track_id:
        # CAS for the same reason as the submit-failure write above.
        await update_document_state(
            str(path),
            expect_status="accepted",
            sha256=digest,
            status="failed",
            validation_state="warn" if verdict.verdict == "warn" else "accept",
            error_stage="submit",
            error_code="submit_missing_track_id",
            error_message="LightRAG submit response missing track_id; worker will retry",
            error_detail=json.dumps(resp, sort_keys=True),
            last_error="missing track_id",
            track_id=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates=cleared_reindex_updates({
                    "dedupe_action": None,
                    "failure_class": "transient",
                    "retryable": True,
                    "retry_reason": retry_reason,
                    "submission_blocked": None,
                }),
            ),
        )
        return

    warnings_json = merge_warning_metadata(
        warnings_json,
        duplicate_of=None,
        worker_updates=cleared_reindex_updates({
            "dedupe_action": None,
            "duplicate_match_count": None,
            "duplicate_match_paths": None,
            "duplicate_status": None,
            "last_submitted_track_id": track_id,
            "last_polled_track_id": track_id,
            "retry_reason": retry_reason,
            "submission_blocked": None,
            "stale_track_id": None,
            "stale_track_marker": None,
            "stale_track_reason": None,
            "track_id_policy": None,
            "track_poll_payload": None,
        }),
    )
    # No CAS here on purpose: this is the write that records a live upstream
    # track. Nothing else can know the track_id yet (it exists only in this
    # local until this write), so no newer submission can be corrupted by it -
    # and dropping it would strand a LightRAG track with no row pointing at it,
    # which is worse than the stale-warning overwrite it does still carry
    # (warnings_json is rewritten from this snapshot; see hand-back).
    await upsert_document(
        document_id=str(uuid.uuid4()),
        path=str(path),
        source_type=source_type,
        company=company,
        company_source=existing.get("company_source") if existing else "unscoped",
        company_source_detail=existing.get("company_source_detail") if existing else None,
        filename=filename or path.name,
        original_filename=original_filename or path.name,
        sha256=digest,
        status="submitted",
        track_id=track_id,
        last_error=None,
        validation_state=verdict.verdict if verdict.verdict in ("accept", "warn") else "accept",
        error_stage="validation" if verdict.verdict == "warn" else None,
        error_code=verdict.meta.get("error_code") if verdict.verdict == "warn" else None,
        error_message=verdict.meta.get("error_message") if verdict.verdict == "warn" else None,
        error_detail=None,
        query_ready=False,
        content_hash=digest,
        detected_encoding=detected_encoding,
        byte_size=byte_size,
        warnings_json=warnings_json,
    )
    await update_document_state(str(path), retry_count=0)


async def poll_track(doc: dict[str, Any]) -> None:
    """Apply one LightRAG track poll to the row.

    ``doc`` is the snapshot the scan read before this HTTP call, so the row may
    have moved on by the time the result arrives. Every write-back here is
    compare-and-set on the track_id and status that snapshot carried: if the
    document was resubmitted meanwhile (new track_id) a result for the old
    track matches no row and is dropped with a log line, instead of overwriting
    the new submission with a stale verdict or ``track_id=None``. The same guard
    makes a replayed stale event a no-op rather than a second downgrade, and it
    does not touch retry bookkeeping.
    """
    track_id = doc.get("track_id")
    if not track_id:
        return
    warnings_json = doc.get("warnings_json")
    try:
        result = await client.get(f"/documents/track_status/{track_id}")
        docs = result.get("documents", [])
        statuses = {str(d.get("status", "")).upper() for d in docs if d.get("status")}
        if not docs:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="stale_track_id",
                error_message="LightRAG track_id no longer resolves; worker will resubmit",
                error_detail=f"Track {track_id} returned no documents",
                last_error="stale track_id",
                track_id=None,
                query_ready=False,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates=stale_track_worker_updates(track_id, "empty_documents"),
                ),
            )
            return
        if not statuses:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="track_status_transient",
                error_message="Transient LightRAG track poll failure; worker will retry",
                error_detail=f"Track {track_id} returned documents without statuses",
                last_error="track status missing",
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "transient",
                        "retryable": True,
                        "retry_reason": "track_status_transient",
                        "last_polled_track_id": track_id,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": "missing_status",
                    },
                ),
            )
            return
        if "FAILED" in statuses:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="lightrag_processing_failed",
                error_message="LightRAG reported failure",
                error_detail=f"Track {track_id} reported FAILED",
                last_error="LightRAG reported failure",
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "terminal",
                        "last_polled_track_id": track_id,
                        "retryable": False,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": None,
                    },
                ),
            )
        elif "PROCESSED" in statuses:
            # Cross-company guard. Only rows that COMPANY_AWARE_DEDUPE released
            # past a SHA match in another company are checked; every other row
            # takes exactly the path it takes today. A deduplicated second submit
            # can report PROCESSED without ever tagging a chunk with our path, so
            # writing ingested/query_ready there would replace a visible
            # duplicate_content row with a healthy row that can never be cited.
            mismatch = cross_company_guard_mismatch(warnings_json, str(doc["path"]), docs)
            if mismatch is not None:
                await update_document_state(
                    doc["path"],
                    expect_track_id=track_id,
                    expect_status=doc.get("status"),
                    status="failed",
                    error_stage="track_poll",
                    error_code="cross_company_not_indexed",
                    error_message=(
                        "LightRAG reported the track processed but no indexed "
                        "document carries this path; the content is indexed under "
                        "another company's document"
                    ),
                    error_detail=json.dumps(mismatch, sort_keys=True),
                    last_error="cross-company content not indexed under this path",
                    query_ready=False,
                    ingested_at=None,
                    warnings_json=merge_warning_metadata(
                        warnings_json,
                        worker_updates={
                            "last_polled_track_id": track_id,
                            "last_track_status": "PROCESSED",
                            "cross_company_guard": "path_mismatch",
                            "failure_class": "terminal",
                            "retryable": False,
                            "stale_track_id": None,
                            "stale_track_marker": None,
                            "stale_track_reason": None,
                            "track_id_policy": None,
                            "track_poll_payload": "cross_company_path_mismatch",
                        },
                    ),
                )
                warn(
                    f"Track {track_id} processed without our path for {doc['path']}: "
                    f"indexed under {mismatch['suppressed_by_path']}"
                )
                return
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="ingested",
                error_stage=None,
                error_code=None,
                error_message=None,
                error_detail=None,
                last_error=None,
                query_ready=True,
                retry_count=0,
                ingested_at="datetime('now')",
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "last_polled_track_id": track_id,
                        "last_track_status": "PROCESSED",
                        "cross_company_guard": None,
                        "cross_company_duplicate_path": None,
                        "cross_company_duplicate_company": None,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": None,
                    },
                ),
            )
        else:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="processing",
                error_stage=None,
                error_code=None,
                error_message=None,
                error_detail=None,
                last_error=None,
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "last_polled_track_id": track_id,
                        "last_track_status": sorted(statuses),
                        "track_poll_payload": None,
                    },
                ),
            )
    except Exception as exc:
        status_code = response_status_code(exc)
        if status_code == 404:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="stale_track_id",
                error_message="LightRAG track_id no longer resolves; worker will resubmit",
                error_detail=str(exc),
                last_error=str(exc),
                track_id=None,
                query_ready=False,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates=stale_track_worker_updates(track_id, "http_404"),
                ),
            )
        elif is_transient_exception(exc):
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="track_status_transient",
                error_message="Transient LightRAG track poll failure; worker will retry",
                error_detail=str(exc),
                last_error=str(exc),
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "transient",
                        "last_polled_track_id": track_id,
                        "retryable": True,
                        "retry_reason": "track_status_transient",
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                    },
                ),
            )
        else:
            await update_document_state(
                doc["path"],
                expect_track_id=track_id,
                expect_status=doc.get("status"),
                status="failed",
                error_stage="track_poll",
                error_code="track_status_failed",
                error_message="Failed to poll LightRAG track status",
                error_detail=str(exc),
                last_error=str(exc),
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "terminal",
                        "last_polled_track_id": track_id,
                        "retryable": False,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                    },
                ),
            )


async def handle_candidate(path: Path, source_type: str, root: Path) -> None:
    if should_skip_path(path, root, source_type):
        return

    if str(path).startswith("rejected/"):
        return

    existing = await get_document_by_path(str(path))
    if not existing:
        company, company_source, company_source_detail = infer_company_from_path(path, root, source_type)
        await upsert_document(
            document_id=str(uuid.uuid4()),
            path=str(path),
            source_type=source_type,
            company=company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=path.name,
            original_filename=path.name,
            sha256="pending",
            status="validating",
            track_id=None,
            validation_state="not_run",
            query_ready=False,
            byte_size=path.stat().st_size,
        )

    digest = sha256_of(path)
    existing = await get_document_by_path(str(path))
    if existing is None:
        return
    existing = await refresh_company_attribution(path, source_type, root, existing)

    status = existing.get("status")
    reindex_requested = is_reindex_requested(existing)
    current_sha256 = existing.get("sha256")
    same_content = current_sha256 == digest
    content_changed = current_sha256 not in {None, "", "pending", "error"} and current_sha256 != digest

    if same_content and status in TERMINAL_SUCCESS and not reindex_requested:
        return
    if same_content and status == "rejected" and not reindex_requested:
        return
    if same_content and existing.get("validation_state") == "auto_split" and not reindex_requested:
        return
    duplicate_row = bool(
        same_content
        and existing.get("error_code") == "duplicate_content"
        and not existing.get("track_id")
        and not reindex_requested
    )

    if content_changed:
        await update_document_state(
            str(path),
            sha256=digest,
            status="validating",
            error_stage=None,
            error_code=None,
            error_message=None,
            error_detail=None,
            last_error=None,
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=path.stat().st_size,
            retry_count=0,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                existing.get("warnings_json"),
                duplicate_of=None,
                worker_updates={
                    "dedupe_action": None,
                    "duplicate_match_count": None,
                    "duplicate_match_paths": None,
                    "duplicate_status": None,
                    "previous_sha256": current_sha256,
                    "previous_track_id": existing.get("track_id"),
                    "retry_reason": "content_changed",
                    "stale_track_id": None,
                    "stale_track_marker": None,
                    "stale_track_reason": None,
                    "submission_blocked": None,
                    "track_id_policy": "replace_on_content_change",
                    "track_poll_payload": None,
                },
            ),
        )
        existing = await get_document_by_path(str(path)) or existing
        status = existing.get("status")
        reindex_requested = is_reindex_requested(existing)

    sha_matches = await get_documents_by_sha256(digest)
    duplicate_of = pick_duplicate_candidate(
        sha_matches, str(path), company=(existing or {}).get("company")
    )
    # The marker is what the company-blind picker WOULD have suppressed us by.
    # Only rows COMPANY_AWARE_DEDUPE released past another company's SHA match
    # carry it, and poll_track's file_path check runs for those rows and no
    # others - so the guard cannot touch ordinary ingestion.
    cross_company_match: dict[str, Any] | None = None
    if settings.company_aware_dedupe and duplicate_of is None:
        blind_match = pick_duplicate_candidate(sha_matches, str(path))
        if blind_match is not None:
            cross_company_match = {
                "path": blind_match.get("path"),
                "company": blind_match.get("company"),
            }
    if duplicate_of is not None:
        if duplicate_row:
            return
        await mark_duplicate_candidate(path, digest, duplicate_of, existing)
        return

    orphaned_duplicate = False
    if duplicate_row:
        await update_document_state(
            str(path),
            sha256=digest,
            status="validating",
            error_stage=None,
            error_code=None,
            error_message=None,
            error_detail=None,
            last_error=None,
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=path.stat().st_size,
            retry_count=0,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                existing.get("warnings_json"),
                duplicate_of=None,
                worker_updates=cleared_reindex_updates({
                    "dedupe_action": None,
                    "dedupe_basis": None,
                    "duplicate_match_count": None,
                    "duplicate_match_paths": None,
                    "duplicate_status": None,
                    "previous_duplicate_status": existing.get("status"),
                    "retry_reason": "duplicate_original_removed",
                    "stale_track_id": None,
                    "stale_track_marker": None,
                    "stale_track_reason": None,
                    "submission_blocked": None,
                    "track_id_policy": None,
                    "track_poll_payload": None,
                }),
            ),
        )
        existing = await get_document_by_path(str(path)) or existing
        status = existing.get("status")
        orphaned_duplicate = True

    if status in {"submitted", "processing"} and existing.get("track_id") and not reindex_requested:
        await poll_track(existing)
        refreshed = await get_document_by_path(str(path))
        if refreshed and refreshed.get("status") in TERMINAL_SUCCESS:
            return
        if refreshed and refreshed.get("status") in {"submitted", "processing"}:
            return
        existing = refreshed or existing
        status = existing.get("status")

    if status == "failed" and existing.get("track_id") and existing.get("error_code") == "track_status_transient" and not reindex_requested:
        await poll_track(existing)
        refreshed = await get_document_by_path(str(path))
        if refreshed and refreshed.get("status") in TERMINAL_SUCCESS:
            return
        if refreshed and refreshed.get("status") in {"submitted", "processing"}:
            return
        existing = refreshed or existing
        status = existing.get("status")

    if status == "failed" and not reindex_requested:
        if not is_retryable_failure(existing):
            return
        if int(existing.get("retry_count") or 0) >= settings.ingest_max_retries:
            return
        if existing.get("track_id") and existing.get("error_code") == "track_status_transient":
            # CAS: this retry counts only against the row this scan read. If a
            # newer submission or an operator reindex moved it on (new track_id,
            # different status), the retry belongs to a submission that no longer
            # exists and must not burn one of ingest_max_retries.
            await increment_retry(
                str(path),
                expect_track_id=existing.get("track_id"),
                expect_status=existing.get("status"),
            )
            return
        await increment_retry(
            str(path),
            expect_track_id=existing.get("track_id"),
            expect_status=existing.get("status"),
        )
        existing = await get_document_by_path(str(path)) or existing

    retry_reason = None
    if content_changed:
        retry_reason = "content_changed"
    elif reindex_requested:
        retry_reason = "operator_reindex"
    elif orphaned_duplicate:
        retry_reason = "duplicate_original_removed"
    elif status == "failed":
        retry_reason = str(existing.get("error_code") or "retry")

    await update_document_state(
        str(path),
        sha256=digest,
        status="accepted",
        query_ready=False,
        content_hash=digest,
        byte_size=path.stat().st_size,
        ingested_at=None,
        warnings_json=merge_warning_metadata(
            existing.get("warnings_json"),
            worker_updates=cleared_reindex_updates(cross_company_guard_updates(cross_company_match)),
        ),
    )
    await submit_file(path, source_type, digest, retry_reason=retry_reason)


async def scan_once() -> None:
    for base, source_type in ((Path(settings.source_docs_dir), "filesystem"), (Path(settings.uploads_dir), "upload")):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            # Before the unsupported-extension branch: a staged upload temp file
            # must produce no row at all, not a `rejected` one.
            if should_skip_path(path, base, source_type):
                continue
            company, company_source, company_source_detail = infer_company_from_path(path, base, source_type)
            if path.suffix.lower() not in SUPPORTED:
                await upsert_document(
                    document_id=str(uuid.uuid4()),
                    path=str(path),
                    source_type=source_type,
                    company=company,
                    company_source=company_source,
                    company_source_detail=company_source_detail,
                    filename=path.name,
                    original_filename=path.name,
                    sha256="pending",
                    status="rejected",
                    track_id=None,
                    validation_state="reject",
                    error_stage="validation",
                    error_code="unsupported_extension",
                    error_message="File extension is not supported",
                    error_detail=None,
                    query_ready=False,
                )
                continue
            try:
                await handle_candidate(path, source_type, base)
            except Exception as exc:
                existing = await get_document_by_path(str(path))
                if existing:
                    await update_document_state(
                        str(path),
                        status="failed",
                        error_stage="submit",
                        error_code="ingest_execution_failed",
                        error_message="Worker failed while handling candidate",
                        error_detail=str(exc),
                        last_error=str(exc),
                        query_ready=False,
                        warnings_json=merge_warning_metadata(
                            existing.get("warnings_json"),
                            worker_updates={
                                "failure_class": "transient",
                                "retryable": True,
                            },
                        ),
                    )
                else:
                    await upsert_document(
                        document_id=str(uuid.uuid4()),
                        path=str(path),
                        source_type=source_type,
                        company=company,
                        company_source=company_source,
                        company_source_detail=company_source_detail,
                        filename=path.name,
                        original_filename=path.name,
                        sha256="error",
                        status="failed",
                        track_id=None,
                        validation_state="not_run",
                        error_stage="submit",
                        error_code="ingest_execution_failed",
                        error_message="Worker failed while handling candidate",
                        error_detail=str(exc),
                        query_ready=False,
                        last_error=str(exc),
                        warnings_json=merge_warning_metadata(
                            None,
                            worker_updates={
                                "failure_class": "transient",
                                "retryable": True,
                            },
                        ),
                    )


async def main() -> None:
    await init_db()
    while True:
        await scan_once()
        await asyncio.sleep(settings.ingest_scan_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
