from __future__ import annotations

import aiosqlite
import logging
from pathlib import Path
from .config import settings


logger = logging.getLogger("gateway.state_store")

ACTIVE_SUPERSEDING_STATUSES = {"accepted", "submitted", "processing", "ingested"}
COMPANY_SOURCE_EXPLICIT = "explicit"
COMPANY_SOURCE_PATH_INFERRED = "path_inferred"
COMPANY_SOURCE_UNSCOPED = "unscoped"


SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    source_type TEXT NOT NULL,
    company TEXT,
    company_source TEXT NOT NULL DEFAULT 'unscoped',
    company_source_detail TEXT,
    filename TEXT NOT NULL,
    original_filename TEXT,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    validation_state TEXT NOT NULL DEFAULT 'not_run',
    error_stage TEXT,
    error_code TEXT,
    error_message TEXT,
    error_detail TEXT,
    query_ready INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    track_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT,
    detected_mime TEXT,
    detected_encoding TEXT,
    byte_size INTEGER,
    page_count INTEGER,
    source_uri TEXT,
    supersedes_document_id TEXT,
    ingested_at TEXT,
    warnings_json TEXT
);
'''

MIGRATIONS: dict[str, str] = {
    "validation_state": "ALTER TABLE documents ADD COLUMN validation_state TEXT NOT NULL DEFAULT 'not_run'",
    "error_stage": "ALTER TABLE documents ADD COLUMN error_stage TEXT",
    "error_code": "ALTER TABLE documents ADD COLUMN error_code TEXT",
    "error_message": "ALTER TABLE documents ADD COLUMN error_message TEXT",
    "error_detail": "ALTER TABLE documents ADD COLUMN error_detail TEXT",
    "query_ready": "ALTER TABLE documents ADD COLUMN query_ready INTEGER NOT NULL DEFAULT 0",
    "content_hash": "ALTER TABLE documents ADD COLUMN content_hash TEXT",
    "detected_mime": "ALTER TABLE documents ADD COLUMN detected_mime TEXT",
    "detected_encoding": "ALTER TABLE documents ADD COLUMN detected_encoding TEXT",
    "byte_size": "ALTER TABLE documents ADD COLUMN byte_size INTEGER",
    "page_count": "ALTER TABLE documents ADD COLUMN page_count INTEGER",
    "source_uri": "ALTER TABLE documents ADD COLUMN source_uri TEXT",
    "supersedes_document_id": "ALTER TABLE documents ADD COLUMN supersedes_document_id TEXT",
    "ingested_at": "ALTER TABLE documents ADD COLUMN ingested_at TEXT",
    "warnings_json": "ALTER TABLE documents ADD COLUMN warnings_json TEXT",
    "original_filename": "ALTER TABLE documents ADD COLUMN original_filename TEXT",
    "company_source": "ALTER TABLE documents ADD COLUMN company_source TEXT NOT NULL DEFAULT 'unscoped'",
    "company_source_detail": "ALTER TABLE documents ADD COLUMN company_source_detail TEXT",
}

UNSET = object()


# ── Lifecycle transitions ───────────────────────────────────────────────────
# The nine statuses of ADR 0001 (docs/decisions/0001-lifecycle-model.md). This
# table is the single definition of which status edges a row may travel; the
# writer functions below enforce it, so a slow poll, a retry or a re-scan can no
# longer downgrade a row silently. Illegal writes are dropped with a log line -
# never raised - because every caller here is a background loop or an API
# request that must keep going.
STATUSES = frozenset({
    "pending",
    "validating",
    "accepted",
    "submitted",
    "processing",
    "ingested",
    "failed",
    "rejected",
    "superseded",
})

# `superseded` is never written as a stored status anywhere in app/: supersession
# is derived at read time by annotate_documents(), which marks a row superseded
# when another ACTIVE row names it in supersedes_document_id, and
# derive_readiness() additionally tolerates a legacy row that carries the value.
# It stays in STATUSES and in the table below so such a row is not wedged (it can
# be re-validated or resubmitted) and so the code's vocabulary matches ADR 0001.
# Anyone adding a writer has to say so here first.
READ_DERIVED_STATUSES = frozenset({"superseded"})

# source -> statuses it may move to. A status is always reachable from itself
# (see is_legal_transition) so idempotent replays of the same event are no-ops
# rather than illegal transitions. Read the denials, not the allowances:
#   ingested   can go back to work (force-reindex, content change, supersede) and
#              can be rejected when the content on disk no longer validates, but
#              it is never downgraded in place to processing or failed. The late
#              write-back that used to do that is stopped by the poll
#              compare-and-set (worker.poll_track's expect_track_id/expect_status),
#              not by this table: every failed write in the pipeline is either a
#              CAS'd poll (which can only land on submitted/processing/failed
#              rows), a submit_file write onto the row the scan had just set to
#              "accepted", or scan_once's exception handler - and that one is
#              refused on purpose, so a queryable row stays queryable and the
#              error is logged instead.
#   rejected / superseded must be re-validated (pending/validating/accepted/
#              submitted) before they can be processing or ingested again.
#   ingested is only reachable from a status that has an upstream track: a row
#              that never went upstream cannot become query-ready.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"validating", "accepted", "submitted", "processing", "failed", "rejected", "superseded"}),
    "validating": frozenset({"pending", "accepted", "submitted", "processing", "failed", "rejected", "superseded"}),
    "accepted": frozenset({"pending", "validating", "submitted", "processing", "failed", "rejected", "superseded"}),
    "submitted": frozenset({"pending", "validating", "accepted", "processing", "ingested", "failed", "rejected", "superseded"}),
    "processing": frozenset({"pending", "validating", "accepted", "submitted", "ingested", "failed", "rejected", "superseded"}),
    "ingested": frozenset({"pending", "validating", "accepted", "submitted", "rejected", "superseded"}),
    "failed": frozenset({"pending", "validating", "accepted", "submitted", "processing", "ingested", "rejected", "superseded"}),
    "rejected": frozenset({"pending", "validating", "accepted", "submitted", "superseded"}),
    "superseded": frozenset({"pending", "validating", "accepted", "submitted"}),
}


def is_legal_transition(from_status: str | None, to_status: str) -> bool:
    """True when a row in ``from_status`` may be written to ``to_status``.

    ``from_status`` of None means "the row does not exist yet" (an insert).
    An unknown ``from_status`` in an existing row is allowed out - a value this
    build does not know about should not wedge the pipeline - but an unknown
    ``to_status`` is never allowed: that is a typo, not a lifecycle state.
    """
    if to_status not in STATUSES:
        return False
    if from_status is None or from_status == to_status:
        return True
    targets = LEGAL_TRANSITIONS.get(str(from_status))
    if targets is None:
        return True
    return to_status in targets


def legal_sources_for(to_status: str) -> list[str]:
    """Existing-row statuses that may legally be written to ``to_status``."""
    return sorted(status for status in STATUSES if is_legal_transition(status, to_status))


def _status_transition_clause(to_status: str) -> tuple[str, list[object]]:
    """SQL guard applied to an existing row when ``to_status`` is written.

    The ``status NOT IN (<all statuses>)`` half lets a row whose status this
    build does not recognise be written through, matching is_legal_transition.
    """
    sources = legal_sources_for(to_status)
    known = sorted(STATUSES)
    return (
        f"(status IN ({', '.join('?' for _ in sources)})"
        f" OR status NOT IN ({', '.join('?' for _ in known)}))",
        [*sources, *known],
    )


def _row_identity(path: str, row: dict | None) -> str:
    if row is None:
        return f"path={path} no row"
    return f"path={path} status={row.get('status')} track_id={row.get('track_id')}"


def _cas_clause(
    *,
    expect_track_id: object = UNSET,
    expect_status: object = UNSET,
) -> tuple[str, list[object]]:
    """Compare-and-set fragment: only touch a row that still matches what the
    caller read. ``track_id IS ?`` matches NULL, so a caller can require "no
    track in flight". Empty fragment when nothing was expected."""
    fragments: list[str] = []
    params: list[object] = []
    if expect_track_id is not UNSET:
        fragments.append("track_id IS ?")
        params.append(expect_track_id)
    if expect_status is not UNSET:
        fragments.append("status=?")
        params.append(expect_status)
    return (" AND ".join(fragments), params)


def normalize_company(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def build_company_attribution(
    *,
    explicit_company: str | None = None,
    inferred_company: str | None = None,
) -> tuple[str | None, str, str]:
    normalized_explicit = normalize_company(explicit_company)
    if normalized_explicit is not None:
        return normalized_explicit, COMPANY_SOURCE_EXPLICIT, "request.company"

    normalized_inferred = normalize_company(inferred_company)
    if normalized_inferred is not None:
        return normalized_inferred, COMPANY_SOURCE_PATH_INFERRED, "source_docs.first_directory"

    return None, COMPANY_SOURCE_UNSCOPED, "unscoped"


def derive_readiness(doc: dict, superseded_ids: set[str] | None = None) -> tuple[bool, str]:
    superseded_ids = superseded_ids or set()
    status = str(doc.get("status") or "")
    validation_state = str(doc.get("validation_state") or "not_run")
    document_id = str(doc.get("document_id") or "")

    if status == "superseded" or (document_id and document_id in superseded_ids):
        return False, "superseded"
    if validation_state == "reject" or status == "rejected":
        return False, "validation_rejected"
    if validation_state == "auto_split":
        return False, "split_required"
    if status == "ingested":
        return True, "ingested"
    if status in {"submitted", "processing"}:
        return False, "processing_upstream"
    if status == "failed":
        return False, "upstream_failed"
    if status in {"pending", "validating"} and validation_state == "not_run":
        return False, "pending_validation"
    if status in {"pending", "accepted", "validating"}:
        return False, "awaiting_submit"
    return False, "pending_validation"


def annotate_documents(documents: list[dict]) -> list[dict]:
    superseded_ids = {
        str(doc.get("supersedes_document_id"))
        for doc in documents
        if doc.get("supersedes_document_id") and doc.get("status") in ACTIVE_SUPERSEDING_STATUSES
    }
    annotated: list[dict] = []
    for doc in documents:
        item = dict(doc)
        query_ready, readiness_reason = derive_readiness(item, superseded_ids)
        item["query_ready"] = query_ready
        item["readiness_reason"] = readiness_reason
        annotated.append(item)
    return annotated


async def _fetch_documents(query: str, params: tuple[object, ...] = ()) -> list[dict]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def init_db() -> None:
    Path(settings.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.executescript(SCHEMA)
        cur = await db.execute("PRAGMA table_info(documents)")
        rows = await cur.fetchall()
        existing = {row[1] for row in rows}
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                await db.execute(statement)
        await db.commit()


async def upsert_document(
    document_id: str,
    path: str,
    source_type: str,
    company: str | None,
    filename: str,
    original_filename: str | None,
    sha256: str,
    status: str,
    track_id: str | None,
    last_error: str | None = None,
    validation_state: str = "not_run",
    error_stage: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_detail: str | None = None,
    query_ready: bool = False,
    content_hash: str | None = None,
    detected_mime: str | None = None,
    detected_encoding: str | None = None,
    byte_size: int | None = None,
    page_count: int | None = None,
    source_uri: str | None = None,
    supersedes_document_id: str | None = None,
    ingested_at: str | None = None,
    warnings_json: str | None = None,
    company_source: str = COMPANY_SOURCE_UNSCOPED,
    company_source_detail: str | None = None,
) -> bool:
    """Insert a row, or update it on a path conflict. Returns True if written.

    The conflict update is guarded by the lifecycle table: a re-scan or a
    re-submit can no longer overwrite a row along an illegal edge, so an
    ingested row is never knocked back to failed/processing by a late or
    unrelated write (it may still be rejected when its content no longer
    validates). A refused overwrite is a no-op plus a log line.
    """
    if status not in STATUSES:
        logger.warning("refusing upsert for %s: unknown status %r", path, status)
        return False
    guard_sql, guard_params = _status_transition_clause(status)
    async with aiosqlite.connect(settings.state_db_path) as db:
        cur = await db.execute(
            f'''
            INSERT INTO documents (
              document_id, path, source_type, company, company_source, company_source_detail,
              filename, original_filename, sha256, status,
              validation_state, error_stage, error_code, error_message, error_detail,
              query_ready, retry_count, track_id, last_error, created_at, updated_at,
              content_hash, detected_mime, detected_encoding, byte_size, page_count,
              source_uri, supersedes_document_id, ingested_at, warnings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, datetime('now'), datetime('now'),
                ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              company=excluded.company,
              company_source=excluded.company_source,
              company_source_detail=excluded.company_source_detail,
              filename=excluded.filename,
              original_filename=excluded.original_filename,
              sha256=excluded.sha256,
              status=excluded.status,
              validation_state=excluded.validation_state,
              error_stage=excluded.error_stage,
              error_code=excluded.error_code,
              error_message=excluded.error_message,
              error_detail=excluded.error_detail,
              query_ready=excluded.query_ready,
              track_id=excluded.track_id,
              last_error=excluded.last_error,
              content_hash=excluded.content_hash,
              detected_mime=excluded.detected_mime,
              detected_encoding=excluded.detected_encoding,
              byte_size=excluded.byte_size,
              page_count=excluded.page_count,
              source_uri=excluded.source_uri,
              supersedes_document_id=excluded.supersedes_document_id,
              ingested_at=excluded.ingested_at,
              warnings_json=excluded.warnings_json,
              updated_at=datetime('now')
            WHERE {guard_sql}
            ''',
            [
                document_id,
                path,
                source_type,
                company,
                company_source,
                company_source_detail,
                filename,
                original_filename,
                sha256,
                status,
                validation_state,
                error_stage,
                error_code,
                error_message,
                error_detail,
                int(query_ready),
                track_id,
                last_error,
                content_hash,
                detected_mime,
                detected_encoding,
                byte_size,
                page_count,
                source_uri,
                supersedes_document_id,
                ingested_at,
                warnings_json,
                *guard_params,
            ],
        )
        await db.commit()
        if cur.rowcount == 0:
            existing = await _fetch_row(db, path)
            logger.warning(
                "upsert of %s as %s ignored: illegal transition from the row as it is now (%s)",
                path,
                status,
                _row_identity(path, existing),
            )
            return False
        return True


async def _fetch_row(db: aiosqlite.Connection, path: str) -> dict | None:
    """Read one row for a log line; only used after a write was refused."""
    db.row_factory = aiosqlite.Row
    cur = await db.execute("SELECT status, track_id FROM documents WHERE path=?", (path,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_document_state(
    path: str,
    *,
    sha256: str | object = UNSET,
    company: str | object = UNSET,
    company_source: str | object = UNSET,
    company_source_detail: str | object = UNSET,
    status: str | object = UNSET,
    validation_state: str | object = UNSET,
    error_stage: str | object = UNSET,
    error_code: str | object = UNSET,
    error_message: str | object = UNSET,
    error_detail: str | object = UNSET,
    last_error: str | object = UNSET,
    track_id: str | object = UNSET,
    query_ready: bool | object = UNSET,
    content_hash: str | object = UNSET,
    detected_mime: str | object = UNSET,
    detected_encoding: str | object = UNSET,
    byte_size: int | object = UNSET,
    page_count: int | object = UNSET,
    source_uri: str | object = UNSET,
    supersedes_document_id: str | object = UNSET,
    ingested_at: str | object = UNSET,
    warnings_json: str | object = UNSET,
    original_filename: str | object = UNSET,
    retry_count: int | object = UNSET,
    expect_track_id: str | None | object = UNSET,
    expect_status: str | object = UNSET,
) -> bool:
    """Apply the given columns to the row for ``path``. Returns True if written.

    Two guards decide whether the write lands:

    1. Lifecycle. When a ``status`` is written, the row's *current* status must
       be a legal source for it (LEGAL_TRANSITIONS). The check is part of the
       UPDATE's WHERE clause, so it is atomic with the write - the row cannot
       change between the check and the update.
    2. Compare-and-set. ``expect_track_id`` / ``expect_status`` restrict the
       update to a row that still matches what the caller read. A poll result
       for a track that has since been replaced matches no row and is dropped,
       rather than overwriting the new submission.

    A refused write is a no-op plus a log line; callers keep going.
    """
    assignments: list[str] = []
    values: list[object] = []
    updates = {
        "sha256": sha256,
        "company": company,
        "company_source": company_source,
        "company_source_detail": company_source_detail,
        "status": status,
        "validation_state": validation_state,
        "error_stage": error_stage,
        "error_code": error_code,
        "error_message": error_message,
        "error_detail": error_detail,
        "last_error": last_error,
        "track_id": track_id,
        "content_hash": content_hash,
        "detected_mime": detected_mime,
        "detected_encoding": detected_encoding,
        "byte_size": byte_size,
        "page_count": page_count,
        "source_uri": source_uri,
        "supersedes_document_id": supersedes_document_id,
        "warnings_json": warnings_json,
        "original_filename": original_filename,
        "retry_count": retry_count,
    }
    for column, value in updates.items():
        if value is not UNSET:
            assignments.append(f"{column}=?")
            values.append(value)
    if query_ready is not UNSET:
        assignments.append("query_ready=?")
        values.append(int(query_ready))
    if ingested_at is not UNSET:
        if ingested_at is None:
            assignments.append("ingested_at=NULL")
        else:
            assignments.append("ingested_at=datetime('now')")
    if not assignments:
        return True
    assignments.append("updated_at=datetime('now')")

    where = ["path=?"]
    where_params: list[object] = [path]
    if status is not UNSET:
        if status not in STATUSES:
            logger.warning("refusing status=%r for %s: not a lifecycle status", status, path)
            return False
        guard_sql, guard_params = _status_transition_clause(status)
        where.append(guard_sql)
        where_params.extend(guard_params)
    if expect_track_id is not UNSET or expect_status is not UNSET:
        cas_sql, cas_params = _cas_clause(expect_track_id=expect_track_id, expect_status=expect_status)
        where.append(cas_sql)
        where_params.extend(cas_params)

    async with aiosqlite.connect(settings.state_db_path) as db:
        cur = await db.execute(
            f"UPDATE documents SET {', '.join(assignments)} WHERE {' AND '.join(where)}",
            [*values, *where_params],
        )
        await db.commit()
        if cur.rowcount == 0:
            row = await _fetch_row(db, path)
            logger.warning(
                "write for %s ignored: status=%s expect_track_id=%s expect_status=%s, row now (%s)",
                path,
                status if status is not UNSET else "-",
                expect_track_id if expect_track_id is not UNSET else "-",
                expect_status if expect_status is not UNSET else "-",
                _row_identity(path, row),
            )
            return False
        return True


async def increment_retry(
    path: str,
    *,
    expect_track_id: str | None | object = UNSET,
    expect_status: str | object = UNSET,
) -> int:
    """``retry_count+1``, optionally compare-and-set on the row the caller read.

    The worker decides to retry from a snapshot it read earlier. If the row has
    moved to a different track_id or status since, the retry belongs to a
    submission that no longer exists, so it must not be counted: the update then
    matches no row, logs, and returns the count unchanged.
    """
    cas_sql, cas_params = _cas_clause(expect_track_id=expect_track_id, expect_status=expect_status)
    where = "path=?" + (f" AND {cas_sql}" if cas_sql else "")
    async with aiosqlite.connect(settings.state_db_path) as db:
        cur = await db.execute(
            f"UPDATE documents SET retry_count=retry_count+1, updated_at=datetime('now') WHERE {where}",
            [path, *cas_params],
        )
        await db.commit()
        if cur.rowcount == 0:
            row = await _fetch_row(db, path)
            logger.warning(
                "retry not counted for %s: expect_track_id=%s expect_status=%s, row now (%s)",
                path,
                expect_track_id if expect_track_id is not UNSET else "-",
                expect_status if expect_status is not UNSET else "-",
                _row_identity(path, row),
            )
        cur = await db.execute("SELECT retry_count FROM documents WHERE path=?", (path,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_document_by_path(path: str) -> dict | None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM documents WHERE path=?", (path,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_document_by_sha256(sha256: str) -> dict | None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM documents WHERE sha256=? ORDER BY updated_at DESC LIMIT 1",
            (sha256,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_documents_by_sha256(sha256: str) -> list[dict]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM documents WHERE sha256=? ORDER BY updated_at DESC",
            (sha256,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def get_documents_by_paths(paths: list[str]) -> list[dict]:
    normalized_paths = [path for path in dict.fromkeys(paths) if path]
    if not normalized_paths:
        return []

    placeholders = ", ".join("?" for _ in normalized_paths)
    documents = await _fetch_documents(
        f"SELECT * FROM documents WHERE path IN ({placeholders})",
        tuple(normalized_paths),
    )
    return annotate_documents(documents)


async def list_documents() -> list[dict]:
    return annotate_documents(await _fetch_documents("SELECT * FROM documents ORDER BY updated_at DESC"))


async def select_documents_for_reindex(
    *,
    document_id: str | None = None,
    path: str | None = None,
    status: str | None = None,
    validation_state: str | None = None,
    company: str | None = None,
    source_type: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    params: list[object] = []

    if document_id:
        clauses.append("document_id=?")
        params.append(document_id)
    if path:
        clauses.append("path=?")
        params.append(path)
    if status:
        clauses.append("status=?")
        params.append(status)
    if validation_state:
        clauses.append("validation_state=?")
        params.append(validation_state)
    if company:
        clauses.append("company=?")
        params.append(company)
    if source_type:
        clauses.append("source_type=?")
        params.append(source_type)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    documents = await _fetch_documents(f"SELECT * FROM documents{where} ORDER BY updated_at DESC", tuple(params))
    return annotate_documents(documents)


async def get_summary() -> dict[str, dict[str, int]]:
    documents = annotate_documents(await _fetch_documents("SELECT * FROM documents ORDER BY updated_at DESC"))
    summary = {
        "by_status": {},
        "by_validation_state": {},
        "by_query_ready": {"true": 0, "false": 0},
        "by_readiness_reason": {},
        "by_error_stage": {},
        "by_company": {},
        "by_company_source": {},
    }
    for doc in documents:
        status = doc.get("status")
        validation_state = doc.get("validation_state")
        readiness_reason = doc.get("readiness_reason")
        error_stage = doc.get("error_stage")
        company = doc.get("company")
        company_source = doc.get("company_source")
        readiness_key = "true" if doc.get("query_ready") else "false"

        if status is not None:
            summary["by_status"][status] = summary["by_status"].get(status, 0) + 1
        if validation_state is not None:
            summary["by_validation_state"][validation_state] = summary["by_validation_state"].get(validation_state, 0) + 1
        summary["by_query_ready"][readiness_key] = summary["by_query_ready"].get(readiness_key, 0) + 1
        if readiness_reason is not None:
            summary["by_readiness_reason"][readiness_reason] = summary["by_readiness_reason"].get(readiness_reason, 0) + 1
        if error_stage is not None:
            summary["by_error_stage"][error_stage] = summary["by_error_stage"].get(error_stage, 0) + 1
        if company is not None:
            summary["by_company"][company] = summary["by_company"].get(company, 0) + 1
        if company_source is not None:
            summary["by_company_source"][company_source] = summary["by_company_source"].get(company_source, 0) + 1
    return summary
