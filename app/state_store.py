from __future__ import annotations

import aiosqlite
from pathlib import Path
from .config import settings


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
) -> None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            '''
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
            ''',
            (
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
            ),
        )
        await db.commit()


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
) -> None:
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
        return
    assignments.append("updated_at=datetime('now')")
    values.append(path)
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(f"UPDATE documents SET {', '.join(assignments)} WHERE path=?", values)
        await db.commit()


async def update_status(path: str, status: str, last_error: str | None = None) -> None:
    await update_document_state(path, status=status, last_error=last_error, query_ready=(status == "ingested"))


async def increment_retry(path: str) -> int:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            "UPDATE documents SET retry_count=retry_count+1, updated_at=datetime('now') WHERE path=?",
            (path,),
        )
        await db.commit()
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
