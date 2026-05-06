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
from .multi_query import (
    build_multi_query_metadata,
    generate_rewrites_via_bypass,
    has_weak_answer_signal,
    merge_and_dedupe_citations,
    parse_keywords_csv,
    should_trigger_multi_query,
)
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

    # Parse transcript keywords for multi-query trigger
    mq_transcript_keywords = parse_keywords_csv(settings.multi_query_transcript_keywords)

    async def execute_query_pass(
        mode: str,
        query_text: str,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str | None]:
        """Execute a single retrieval pass and return (answer, citations, query_scope, rerank_meta, weak_reason)."""
        result = await client.post_json(
            "/query",
            {
                "query": query_text,
                "mode": mode,
                "top_k": top_k,
                "include_references": True,
                "include_chunk_content": True,
            },
        )
        answer = str(result.get("response", ""))
        # For multi-query, scope is applied after merge, so we return raw references
        # The scope_flag indicates whether to scope now or later
        citations_raw = result.get("references", [])
        # Return raw citations without scoping for multi-query merge
        # The caller will decide whether to scope
        answer_only = answer

        rerank_meta: dict[str, Any] = {
            "rerank_enabled": settings.rerank_enabled and settings.rerank_binding_host is not None,
            "rerank_applied": False,
            "rerank_error": None,
            "rerank_input_count": 0,
            "rerank_output_count": 0,
        }

        # Compute weak signal for trigger decision
        weak_reason = None
        if len(citations_raw) < 2:
            weak_reason = "citations_below_threshold"
        elif has_weak_answer_signal(answer):
            weak_reason = "weak_answer_signal"

        return answer_only, citations_raw, {}, rerank_meta, weak_reason

    async def apply_scope_rerank_truncate(
        citations: list[dict[str, Any]],
        query_text: str,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Apply company scope filter, rerank, and top-5 truncation."""
        scoped_citations, query_scope = await scope_query_citations(citations, requested_company=requested_company)

        rerank_meta: dict[str, Any] = {
            "rerank_enabled": settings.rerank_enabled and settings.rerank_binding_host is not None,
            "rerank_applied": False,
            "rerank_error": None,
            "rerank_input_count": len(scoped_citations),
            "rerank_output_count": 0,
        }
        if settings.rerank_enabled and settings.rerank_binding_host:
            reranked_citations, rerank_meta = await rerank_citations(query_text, scoped_citations, settings.rerank_binding_host)
            if rerank_meta.get("rerank_applied"):
                scoped_citations = reranked_citations

        scoped_citations = truncate_citations(scoped_citations, top_k=RERANK_TOP_K)
        return scoped_citations, rerank_meta

    fallback_used = False
    fallback_reason = None

    # ── First pass: original query ──────────────────────────────────────
    answer, all_citations, _, _, first_weak_reason = await execute_query_pass(retrieval_mode, req.query)

    # Compute weak signal from first pass
    first_weak_signal = first_weak_reason is not None

    # ── Multi-query trigger decision ────────────────────────────────────
    mq_enabled = settings.multi_query_enabled
    mq_triggered = False
    mq_trigger_reason = "none"
    mq_error = None
    mq_rewrite_count = 0

    if mq_enabled:
        mq_triggered, mq_trigger_reason = should_trigger_multi_query(
            req.query,
            transcript_keywords=mq_transcript_keywords,
            long_query_words=settings.multi_query_long_query_words,
            weak_signal=first_weak_signal,
        )

    # ── Multi-query expansion (if triggered and enabled) ────────────────
    merged_citations = all_citations  # default: single-query path

    if mq_triggered:
        groups = [all_citations]
        total_before_dedupe = len(all_citations)
        total_after_dedupe = len(all_citations)

        # Generate rewrites via bypass mode
        rewrites = await generate_rewrites_via_bypass(
            client,
            req.query,
            settings.multi_query_rewrite_count,
        )
        mq_rewrite_count = len(rewrites)

        if rewrites:
            # Retrieve citations for each rewrite
            for rw_query in rewrites:
                try:
                    _, rw_citations, _, _, _ = await execute_query_pass(retrieval_mode, rw_query)
                    groups.append(rw_citations)
                except Exception:
                    mq_error = "rewrite_retrieval_failed"
                    break
            if mq_error:
                # Fail open to original single-query path
                merged_citations = all_citations
                total_before_dedupe = len(all_citations)
                total_after_dedupe = len(all_citations)
            else:
                # Merge and dedupe citations (stable order: original -> rewrite1 -> rewrite2)
                total_before_dedupe = sum(len(g) for g in groups)
                merged_citations = merge_and_dedupe_citations(groups)
                total_after_dedupe = len(merged_citations)
        else:
            mq_error = "rewrite_generation_failed"
            # Fail open to original single-query path
            merged_citations = all_citations
            total_before_dedupe = len(all_citations)
            total_after_dedupe = len(all_citations)
    else:
        total_before_dedupe = len(all_citations)
        total_after_dedupe = len(all_citations)

    # ── Fallback logic (only for single-query or fail-open path) ────────
    weak_reason = None
    if first_weak_reason and retrieval_mode != "bypass":
        fallback_used = True
        fallback_reason = first_weak_reason
        retrieval_mode = "naive"
        # Re-run first pass with naive mode
        answer, all_citations_fallback, _, _, _ = await execute_query_pass(retrieval_mode, req.query)
        # If multi-query was triggered, re-merge with fallback
        if mq_triggered and not mq_error:
            groups_fallback = [all_citations_fallback]
            for rw_query in (rewrites if rewrites else []):
                try:
                    _, rw_citations, _, _, _ = await execute_query_pass(retrieval_mode, rw_query)
                    groups_fallback.append(rw_citations)
                except Exception:
                    mq_error = "rewrite_retrieval_failed"
                    groups_fallback.append([])
                    break
            merged_citations = merge_and_dedupe_citations(groups_fallback)
        else:
            merged_citations = all_citations_fallback

    # ── Apply scope filter, rerank, truncate on merged citations ────────
    final_citations, rerank_meta = await apply_scope_rerank_truncate(merged_citations, req.query)

    # ── Build query_scope metadata ──────────────────────────────────────
    query_scope: dict[str, Any] = {
        "company": requested_company,
        "mode": "company" if requested_company is not None else "unscoped",
        "policy": {
            "scoped_query_behavior": "exact_company_match_only" if requested_company is not None else "include_all_upstream_citations",
            "unscoped_docs_in_scoped_queries": False,
            "unscoped_docs_in_unscoped_queries": True,
            "enforcement": "gateway_citation_filter" if requested_company is not None else "none",
            "hard_multi_tenant_isolation": False,
        },
        "retrieval_mode_used": retrieval_mode,
        "fallback_used": fallback_used,
        "fallback_reason": fallback_reason,
        "explicit_mode": explicit_mode,
        "transcript_like_query": transcript_like,
        "top_k_used": top_k,
        "rerank_enabled": rerank_meta["rerank_enabled"],
        "rerank_applied": rerank_meta["rerank_applied"],
        "rerank_error": rerank_meta["rerank_error"],
        "rerank_input_count": rerank_meta["rerank_input_count"],
        "rerank_output_count": rerank_meta["rerank_output_count"],
    }

    # Add multi-query metadata
    mq_meta = build_multi_query_metadata(
        enabled=mq_enabled,
        triggered=mq_triggered,
        trigger_reason=mq_trigger_reason,
        rewrite_count=mq_rewrite_count,
        error=mq_error,
        candidate_count_before_dedupe=total_before_dedupe,
        candidate_count_after_dedupe=total_after_dedupe,
    )
    query_scope.update(mq_meta)

    if requested_company is not None:
        query_scope["warning"] = "Company scoping is enforced on gateway-returned citations only; this packet does not claim hard isolation inside LightRAG itself"

    return ok({"answer": answer, "citations": final_citations, "query_scope": query_scope})


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
