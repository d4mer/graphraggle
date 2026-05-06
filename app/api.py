from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from typing import Any

import aiofiles
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile

from .auth import require_bearer
from .config import settings
from .generation import generate_document
from .lightrag_client import client
from .models import Envelope, GenerateDocumentRequest, QueryRequest, ReindexRequest
from .rerank import RERANK_TOP_K, rerank_citations, truncate_citations
from .state_store import (
    build_company_attribution,
    derive_readiness,
    get_document_by_sha256,
    get_documents_by_paths,
    get_summary,
    init_db,
    list_documents,
    normalize_company,
    select_documents_for_reindex,
    update_document_state,
    upsert_document,
)
from .validation import validate_upload

app = FastAPI(title="RAG Gateway", version="0.1.0")

FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")
TRANSCRIPT_QUERY_KEYWORDS = ("transcript", "workshop", "speaker", "meeting minutes", "recording")
WEAK_ANSWER_MARKERS = ("not enough information", "do not have enough information")


@app.on_event("startup")
async def startup() -> None:
    await init_db()


def ok(data):
    return Envelope(ok=True, data=data, meta={"request_id": str(uuid.uuid4())}, error=None)


def sanitize_original_name(filename: str) -> str:
    original = Path(filename).name
    stem = Path(original).stem or "upload"
    sanitized = FILENAME_SANITIZE_RE.sub("-", stem).strip(".-_")
    return sanitized or "upload"


def build_stored_filename(document_id: str, original_filename: str) -> str:
    suffix = Path(original_filename).suffix.lower()
    return f"{document_id}--{sanitize_original_name(original_filename)}{suffix}"


def build_upload_payload(
    *,
    document_id: str,
    source_type: str,
    company: str | None,
    company_source: str,
    company_source_detail: str | None,
    filename: str,
    original_filename: str,
    path: str,
    status: str,
    validation_state: str,
    query_ready: bool,
    validation: dict,
    content_hash: str,
    byte_size: int,
    duplicate_of: dict | None = None,
) -> dict:
    payload = {
        "document_id": document_id,
        "source_type": source_type,
        "company": company,
        "company_source": company_source,
        "company_source_detail": company_source_detail,
        "filename": filename,
        "original_filename": original_filename,
        "path": path,
        "status": status,
        "validation_state": validation_state,
        "query_ready": query_ready,
        "validation": validation,
        "content_hash": content_hash,
        "sha256": content_hash,
        "byte_size": byte_size,
        "duplicate_of": duplicate_of,
        "warnings_json": None,
        "detected_mime": None,
        "detected_encoding": None,
        "page_count": None,
        "source_uri": None,
        "supersedes_document_id": None,
        "ingested_at": None,
    }
    payload["query_ready"], payload["readiness_reason"] = derive_readiness(payload)
    return payload


def extract_citation_path(citation: dict[str, Any]) -> str | None:
    for key in ("file_source", "path", "source", "file_path"):
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def enrich_citation(citation: dict[str, Any], document: dict[str, Any] | None) -> dict[str, Any]:
    enriched = dict(citation)
    citation_path = extract_citation_path(citation)
    enriched["document_id"] = document.get("document_id") if document else None
    enriched["path"] = citation_path or (document.get("path") if document else None)
    enriched["company"] = document.get("company") if document else None
    enriched["company_source"] = document.get("company_source") if document else None
    enriched["company_source_detail"] = document.get("company_source_detail") if document else None
    enriched["source_type"] = document.get("source_type") if document else None
    enriched["query_ready"] = document.get("query_ready") if document else None
    enriched["readiness_reason"] = document.get("readiness_reason") if document else None
    return enriched


async def scope_query_citations(
    citations: list[dict[str, Any]],
    *,
    requested_company: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    citation_paths = [path for citation in citations if (path := extract_citation_path(citation))]
    documents = await get_documents_by_paths(citation_paths)
    documents_by_path = {str(doc.get("path")): doc for doc in documents if doc.get("path")}

    included: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for citation in citations:
        citation_path = extract_citation_path(citation)
        document = documents_by_path.get(citation_path) if citation_path else None
        enriched = enrich_citation(citation, document)

        if requested_company is None or (document and document.get("company") == requested_company):
            included.append(enriched)
        else:
            excluded.append(enriched)

    scope = {
        "company": requested_company,
        "mode": "company" if requested_company is not None else "unscoped",
        "policy": {
            "scoped_query_behavior": "exact_company_match_only" if requested_company is not None else "include_all_upstream_citations",
            "unscoped_docs_in_scoped_queries": False,
            "unscoped_docs_in_unscoped_queries": True,
            "enforcement": "gateway_citation_filter" if requested_company is not None else "none",
            "hard_multi_tenant_isolation": False,
        },
        "included_citation_count": len(included),
        "excluded_citation_count": len(excluded),
        "excluded_citations": excluded,
    }
    if requested_company is not None:
        scope["warning"] = "Company scoping is enforced on gateway-returned citations only; this packet does not claim hard isolation inside LightRAG itself"
    return included, scope


def parse_metadata(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"legacy_warning": raw}
    return parsed if isinstance(parsed, dict) else {}


def build_reindex_warnings_json(raw: str | None, *, force: bool, scope: dict, doc: dict) -> str:
    meta = parse_metadata(raw)
    worker_meta = meta.get("worker")
    if not isinstance(worker_meta, dict):
        worker_meta = {}
    worker_meta.update(
        {
            "reindex_requested": True,
            "reindex_force": force,
            "requested_from_status": doc.get("status"),
            "requested_from_validation_state": doc.get("validation_state"),
        }
    )
    meta["worker"] = worker_meta
    meta["reindex"] = {
        "force": force,
        "scope": scope,
        "requested_from_status": doc.get("status"),
        "requested_from_validation_state": doc.get("validation_state"),
    }
    return json.dumps(meta, sort_keys=True)


def reindex_block_reason(doc: dict, *, force: bool) -> tuple[str, str] | None:
    if force:
        return None
    if doc.get("query_ready"):
        return "ingested", "Document is already query-ready; use force=true to resubmit intentionally"
    if doc.get("status") in {"submitted", "processing"}:
        return "processing_upstream", "Document is already in the upstream ingestion workflow; use force=true to replace that submission"
    if doc.get("status") == "rejected" or doc.get("validation_state") == "reject":
        return "validation_rejected", "Document is validation-rejected; use force=true to send it back through worker validation"
    if doc.get("validation_state") == "auto_split":
        return "split_required", "Document is blocked for manual splitting; use force=true only if you want the worker to re-evaluate the same file"
    return None


def is_transcript_like_query(query: str) -> bool:
    lowered = query.lower()
    return any(keyword in lowered for keyword in TRANSCRIPT_QUERY_KEYWORDS)


def has_weak_answer_signal(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in WEAK_ANSWER_MARKERS)


@app.get("/health", response_model=Envelope)
async def health():
    try:
        data = await client.get("/health")
        return ok({"status": "ok", "lightrag": data})
    except Exception as exc:
        return Envelope(
            ok=False,
            data=None,
            meta={"request_id": str(uuid.uuid4())},
            error={"code": "upstream_error", "message": str(exc)},
        )


@app.get("/ingest/status", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def ingest_status():
    return ok({"summary": await get_summary(), "documents": await list_documents()})


@app.get("/documents", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def documents():
    return ok({"documents": await list_documents()})


@app.get("/documents/{document_id}", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def document_detail(document_id: str):
    docs = await list_documents()
    for doc in docs:
        if doc["document_id"] == document_id:
            return ok({"document": doc})
    raise HTTPException(status_code=404, detail="Document not found")


@app.post("/upload", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def upload(file: UploadFile = File(...), company: str | None = Form(default=None)):
    original_filename = file.filename or "unknown"
    document_id = str(uuid.uuid4())
    stored_filename = build_stored_filename(document_id, original_filename)
    filename = stored_filename
    normalized_company, company_source, company_source_detail = build_company_attribution(explicit_company=company)

    # Use the actual file size from the UploadFile so that
    # validate_upload can distinguish empty from non-empty uploads.
    preflight_size = file.size if file.size is not None else 1
    verdict = validate_upload(original_filename, preflight_size)
    rejected_path = f"rejected/{stored_filename}"

    if verdict.verdict == "reject":
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            path=rejected_path,
            status="rejected",
            validation_state="reject",
            query_ready=False,
            validation=verdict.meta,
            content_hash="pending",
            byte_size=file.size if file.size is not None else 0,
        )
        await upsert_document(
            document_id=document_id,
            path=rejected_path,
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            sha256="pending",
            status="rejected",
            track_id=None,
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
        )
        return Envelope(
            ok=False,
            data=payload,
            meta={"request_id": str(uuid.uuid4())},
            error={
                "code": verdict.meta.get("error_code", "validation_error"),
                "message": verdict.meta.get("error_message", "Validation failed"),
            },
        )

    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / stored_filename
    temp_dest = upload_dir / f".{document_id}.part"
    digest = hashlib.sha256()
    async with aiofiles.open(temp_dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            digest.update(chunk)
            await f.write(chunk)

    file_size = temp_dest.stat().st_size
    verdict = validate_upload(original_filename, file_size)
    sha256 = digest.hexdigest()

    if verdict.verdict == "reject":
        temp_dest.unlink(missing_ok=True)
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            path=rejected_path,
            status="rejected",
            validation_state="reject",
            query_ready=False,
            validation=verdict.meta,
            content_hash=sha256,
            byte_size=file_size,
        )
        await upsert_document(
            document_id=document_id,
            path=rejected_path,
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            sha256=sha256,
            status="rejected",
            track_id=None,
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            byte_size=file_size,
            content_hash=sha256,
        )
        return Envelope(
            ok=False,
            data=payload,
            meta={"request_id": str(uuid.uuid4())},
            error={
                "code": verdict.meta.get("error_code", "validation_error"),
                "message": verdict.meta.get("error_message", "Validation failed"),
            },
        )

    temp_dest.replace(dest)

    duplicate_doc = await get_document_by_sha256(sha256)
    duplicate_of = None
    warnings_json = None

    if duplicate_doc and duplicate_doc.get("path") != str(dest):
        duplicate_of = {
            "document_id": duplicate_doc.get("document_id"),
            "path": duplicate_doc.get("path"),
            "original_filename": duplicate_doc.get("original_filename"),
            "company": duplicate_doc.get("company"),
        }
        verdict.meta = {
            **verdict.meta,
            "duplicate_of": duplicate_of,
        }
        warnings_json = json.dumps({"duplicate_of": duplicate_of}, sort_keys=True)
        if verdict.verdict in {"accept", "warn"}:
            verdict = type(verdict)("warn", {
                **verdict.meta,
                "error_code": verdict.meta.get("error_code", "duplicate_content"),
                "error_message": verdict.meta.get(
                    "error_message",
                    "Duplicate content detected at upload time",
                ),
            })

    if verdict.verdict == "auto_split":
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            path=str(dest),
            status="pending",
            validation_state="auto_split",
            query_ready=False,
            validation=verdict.meta,
            content_hash=sha256,
            byte_size=file_size,
            duplicate_of=duplicate_of,
        )
        payload["warnings_json"] = warnings_json
        await upsert_document(
            document_id=document_id,
            path=str(dest),
            source_type="upload",
            company=normalized_company,
            company_source=company_source,
            company_source_detail=company_source_detail,
            filename=filename,
            original_filename=original_filename,
            sha256=sha256,
            status="pending",
            track_id=None,
            validation_state="auto_split",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            byte_size=file_size,
            content_hash=sha256,
            warnings_json=warnings_json,
        )
        return ok(payload)

    validation_state = "warn" if verdict.verdict == "warn" else "accept"
    payload = build_upload_payload(
        document_id=document_id,
        source_type="upload",
        company=normalized_company,
        company_source=company_source,
        company_source_detail=company_source_detail,
        filename=filename,
        original_filename=original_filename,
        path=str(dest),
        status="pending",
        validation_state=validation_state,
        query_ready=False,
        validation=verdict.meta,
        content_hash=sha256,
        byte_size=file_size,
        duplicate_of=duplicate_of,
    )
    payload["warnings_json"] = warnings_json
    await upsert_document(
        document_id=document_id,
        path=str(dest),
        source_type="upload",
        company=normalized_company,
        company_source=company_source,
        company_source_detail=company_source_detail,
        filename=filename,
        original_filename=original_filename,
        sha256=sha256,
        status="pending",
        track_id=None,
        validation_state=validation_state,
        error_stage="validation" if verdict.verdict == "warn" else None,
        error_code=verdict.meta.get("error_code") if verdict.verdict == "warn" else None,
        error_message=verdict.meta.get("error_message") if verdict.verdict == "warn" else None,
        error_detail=None,
        query_ready=False,
        byte_size=file_size,
        content_hash=sha256,
        warnings_json=warnings_json,
    )
    return ok(payload)


@app.post("/query", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def query(req: QueryRequest):
    requested_company = normalize_company(req.company)
    explicit_mode = req.mode is not None
    retrieval_mode = req.mode or "hybrid"
    transcript_like = is_transcript_like_query(req.query)
    top_k = max(req.top_k, 24) if transcript_like else req.top_k

    async def execute_query_pass(mode: str) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str | None]:
        result = await client.post_json(
            "/query",
            {
                "query": req.query,
                "mode": mode,
                "top_k": top_k,
                "include_references": True,
                "include_chunk_content": True,
            },
        )
        answer = str(result.get("response", ""))
        citations, query_scope = await scope_query_citations(result.get("references", []), requested_company=requested_company)

        rerank_meta: dict[str, Any] = {
            "rerank_enabled": settings.rerank_enabled and settings.rerank_binding_host is not None,
            "rerank_applied": False,
            "rerank_error": None,
            "rerank_input_count": len(citations),
            "rerank_output_count": 0,
        }
        if settings.rerank_enabled and settings.rerank_binding_host:
            reranked_citations, rerank_meta = await rerank_citations(req.query, citations, settings.rerank_binding_host)
            if rerank_meta.get("rerank_applied"):
                citations = reranked_citations

        citations = truncate_citations(citations, top_k=RERANK_TOP_K)

        weak_reason = None
        if len(citations) < 2:
            weak_reason = "citations_below_threshold"
        elif has_weak_answer_signal(answer):
            weak_reason = "weak_answer_signal"

        return answer, citations, query_scope, rerank_meta, weak_reason

    fallback_used = False
    fallback_reason = None

    answer, citations, query_scope, rerank_meta, weak_reason = await execute_query_pass(retrieval_mode)

    if weak_reason and retrieval_mode != "bypass":
        fallback_used = True
        fallback_reason = weak_reason
        retrieval_mode = "naive"
        answer, citations, query_scope, rerank_meta, _ = await execute_query_pass(retrieval_mode)

    query_scope["retrieval_mode_used"] = retrieval_mode
    query_scope["fallback_used"] = fallback_used
    query_scope["fallback_reason"] = fallback_reason
    query_scope["explicit_mode"] = explicit_mode
    query_scope["transcript_like_query"] = transcript_like
    query_scope["top_k_used"] = top_k
    query_scope["rerank_enabled"] = rerank_meta["rerank_enabled"]
    query_scope["rerank_applied"] = rerank_meta["rerank_applied"]
    query_scope["rerank_error"] = rerank_meta["rerank_error"]
    query_scope["rerank_input_count"] = rerank_meta["rerank_input_count"]
    query_scope["rerank_output_count"] = rerank_meta["rerank_output_count"]
    return ok({"answer": answer, "citations": citations, "query_scope": query_scope})


@app.post("/generate-document", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def generate(req: GenerateDocumentRequest):
    return ok(await generate_document(req.query, req.document_type))


@app.post("/ingest/reindex", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def reindex(req: ReindexRequest):
    scope = req.model_dump(exclude_none=True)
    selectors = {key: value for key, value in scope.items() if key != "force"}
    if not selectors:
        raise HTTPException(status_code=400, detail="Reindex requires at least one selector")

    documents = await select_documents_for_reindex(
        document_id=req.document_id,
        path=req.path,
        status=req.status,
        validation_state=req.validation_state,
        company=req.company,
        source_type=req.source_type,
    )
    if not documents:
        raise HTTPException(status_code=404, detail="No documents matched the reindex scope")

    queued: list[dict] = []
    blocked: list[dict] = []

    for doc in documents:
        block = reindex_block_reason(doc, force=req.force)
        if block is not None:
            reason, message = block
            blocked.append(
                {
                    "document_id": doc.get("document_id"),
                    "path": doc.get("path"),
                    "status": doc.get("status"),
                    "validation_state": doc.get("validation_state"),
                    "query_ready": doc.get("query_ready"),
                    "readiness_reason": reason,
                    "message": message,
                }
            )
            continue

        await update_document_state(
            str(doc.get("path")),
            status="pending",
            error_stage="reindex",
            error_code="reindex_requested",
            error_message="Operator requested reindex; worker will resubmit through normal flow",
            error_detail=json.dumps({"force": req.force, "scope": selectors}, sort_keys=True),
            last_error=None,
            track_id=None,
            query_ready=False,
            retry_count=0,
            ingested_at=None,
            warnings_json=build_reindex_warnings_json(doc.get("warnings_json"), force=req.force, scope=selectors, doc=doc),
        )
        queued.append(
            {
                "document_id": doc.get("document_id"),
                "path": doc.get("path"),
                "previous_status": doc.get("status"),
                "previous_validation_state": doc.get("validation_state"),
                "previous_readiness_reason": doc.get("readiness_reason"),
                "queued_status": "pending",
                "worker_action": "rescan_and_resubmit",
            }
        )

    return ok(
        {
            "queued": bool(queued),
            "scope": selectors,
            "force": req.force,
            "selection_count": len(documents),
            "queued_count": len(queued),
            "blocked_count": len(blocked),
            "queued_documents": queued,
            "blocked_documents": blocked,
        }
    )


@app.get("/api/version")
async def api_version():
    resp = await client.proxy("GET", "/api/version")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.get("/api/tags")
async def api_tags():
    resp = await client.proxy("GET", "/api/tags")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.get("/api/ps")
async def api_ps():
    resp = await client.proxy("GET", "/api/ps")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.post("/api/generate")
async def api_generate(request: Request):
    body = await request.body()
    resp = await client.proxy("POST", "/api/generate", body=body, content_type=request.headers.get("content-type"))
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.body()
    resp = await client.proxy("POST", "/api/chat", body=body, content_type=request.headers.get("content-type"))
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))
