from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .lightrag_client import client
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
from .validation import validate_file

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".html", ".htm"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".json", ".csv"}
TERMINAL_SUCCESS = {"ingested"}
LIGHTRAG_INTERNAL_DIRS = {"__enqueued__"}
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


def should_skip_path(path: Path, root: Path, source_type: str) -> bool:
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


def pick_duplicate_candidate(documents: list[dict[str, Any]], current_path: str) -> dict[str, Any] | None:
    rank = {"ingested": 0, "processing": 1, "submitted": 2, "accepted": 3}
    candidates = [
        doc
        for doc in documents
        if doc.get("path") != current_path and doc.get("status") in ACTIVE_DUPLICATE_STATUSES
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
    if path.suffix.lower() in TEXT_EXTENSIONS:
        try:
            text_payload, detected_encoding = read_text_with_fallbacks(path)
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

    try:
        if text_payload is not None:
            resp = await client.post_json(
                "/documents/text",
                {
                    "text": text_payload,
                    "file_source": str(path),
                },
            )
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
        await update_document_state(
            str(path),
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
        await update_document_state(
            str(path),
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
            await update_document_state(
                doc["path"],
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
    if same_content and existing.get("error_code") == "duplicate_content" and not existing.get("track_id") and not reindex_requested:
        return

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

    duplicate_of = pick_duplicate_candidate(await get_documents_by_sha256(digest), str(path))
    if duplicate_of is not None:
        await mark_duplicate_candidate(path, digest, duplicate_of, existing)
        return

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
            await increment_retry(str(path))
            return
        await increment_retry(str(path))
        existing = await get_document_by_path(str(path)) or existing

    retry_reason = None
    if content_changed:
        retry_reason = "content_changed"
    elif reindex_requested:
        retry_reason = "operator_reindex"
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
            worker_updates=cleared_reindex_updates(),
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
