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
from .graphrag import (
    build_graph_metadata,
    compute_adaptive_hops,
    expand_graph_neighbors,
    extract_entities_via_llm,
    filter_graph_neighbors_for_query,
    merge_graph_expansion,
)
from .graph_native import build_graph_native_metadata, fetch_graph_native_evidence
from .graph_fusion import fuse_graph_and_vector_evidence
from .graph_synthesis import is_graph_synthesis_answer_usable, synthesize_graph_aware_answer
from .rerank import rerank_citations, truncate_citations
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
GRAPH_QUERY_KEYWORDS = (
    "process",
    "shipment",
    "code orange",
    "ecommit",
    "firm horizon",
    "dashboard",
    "exception",
    "logistics",
    "cmo",
    "consolidation",
    "transport",
    "freight",
    "policy",
)
WEAK_ANSWER_MARKERS = ("not enough information", "do not have enough information")
OLLAMA_BRIDGE_MODEL = "lightrag:latest"
# Marker separating an answer body from the appended Sources block. Defined
# once; used by build_sources_block and by history extraction so prior-turn
# Sources blocks are stripped before being sent back upstream.
SOURCES_MARKER = "\n\n---\n\n**Sources**\n\n"
OPENWEBUI_TASK_PREFIX = "### Task:"

# Recognized reasoning boundary pairs (opening, closing). Built by
# concatenation so the literal ChatML control tokens do not appear verbatim
# in this source file. The strip rule is strict: a region is removed ONLY
# when a complete, recognized pair is present. An isolated opening marker
# with no matching close is left alone — guessing where reasoning ends
# risks cutting real answer text.
_REASONING_OPEN = "<" + "think" + ">"
_REASONING_CLOSE = "<" + "/think" + ">"
REASONING_BOUNDARY_PAIRS: tuple[tuple[str, str], ...] = (
    (_REASONING_OPEN, _REASONING_CLOSE),
)
MAX_HISTORY_MESSAGE_CHARS = 2000


def normalize_ollama_stream_response(raw_body: bytes, *, response_key: str) -> bytes:
    text = raw_body.decode("utf-8", errors="replace")
    chunks: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            return raw_body
        if isinstance(parsed, dict):
            chunks.append(parsed)
    if not chunks:
        return raw_body

    final_chunk = dict(chunks[-1])
    aggregated = ""
    if response_key == "message":
        for chunk in chunks:
            message = chunk.get("message")
            if isinstance(message, dict):
                aggregated += str(message.get("content", ""))
        final_message = dict(final_chunk.get("message") or {})
        final_message["content"] = aggregated
        final_chunk["message"] = final_message
    else:
        for chunk in chunks:
            aggregated += str(chunk.get(response_key, ""))
        final_chunk[response_key] = aggregated
    final_chunk["done"] = True
    return (json.dumps(final_chunk) + "\n").encode("utf-8")


def request_wants_stream(raw_body: bytes) -> bool:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return False
    return bool(payload.get("stream")) if isinstance(payload, dict) else False


def parse_json_body(raw_body: bytes) -> dict[str, Any] | None:
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def extract_ollama_prompt(payload: dict[str, Any]) -> str:
    prompt = payload.get("prompt")
    if isinstance(prompt, str) and prompt.strip():
        return prompt.strip()

    messages = payload.get("messages")
    if isinstance(messages, list):
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if message.get("role") != "user":
                continue
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return ""


def build_sources_block(citations: list[dict[str, Any]]) -> str:
    """Render citations as a markdown Sources block for the OpenWebUI bridge.

    The bridge returns a plain string, so citations are appended to the answer
    body. Returns "" when there is nothing to show.
    """
    seen: set[str] = set()
    lines: list[str] = []
    for citation in citations:
        if not isinstance(citation, dict):
            continue
        path = extract_citation_path(citation)
        if not path:
            continue
        label = path.rsplit("/", 1)[-1]
        if label in seen:
            continue
        seen.add(label)
        company = citation.get("company")
        suffix = f" — {company}" if isinstance(company, str) and company.strip() else ""
        lines.append(f"{len(lines) + 1}. {label}{suffix}")
    if not lines:
        return ""
    return SOURCES_MARKER + "\n".join(lines)


# ── Direct LightRAG bridge (production hot-patch, now settings-driven) ───
# Production has run this variant of the bridge since a direct hot-patch; it
# was imported verbatim in the previous commit. Its parameters are now
# settings, with defaults equal to the hot-patch values so the deployed
# payload is byte-for-byte what production sends today, plus three opt-out
# additions: task short-circuit (GRAG-12), conversation history (GRAG-11),
# and an appended Sources block. Each is behind its own setting.

def build_ollama_bridge_query_payload(prompt: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    return {
        "query": prompt,
        "mode": "mix",
        "top_k": settings.bridge_top_k,
        "chunk_top_k": settings.bridge_chunk_top_k,
        "max_entity_tokens": settings.bridge_max_entity_tokens,
        "max_relation_tokens": settings.bridge_max_relation_tokens,
        "max_total_tokens": settings.bridge_max_total_tokens,
        "response_type": "Multiple Paragraphs",
        "only_need_context": False,
        "only_need_prompt": False,
        "stream": True,
        # Filled from prior Open WebUI turns when BRIDGE_HISTORY_TURNS > 0.
        # LightRAG sends history to the LLM only; it does not affect retrieval.
        "conversation_history": history or [],
        "user_prompt": "",
        "enable_rerank": settings.bridge_enable_rerank,
        "include_references": True,
        "include_chunk_content": False,
    }


def strip_reasoning_regions(text: str) -> str:
    """Remove complete reasoning regions from aggregated stream text.

    Only removes a span delimited by a complete, recognized boundary pair
    (see REASONING_BOUNDARY_PAIRS). Orphan markers are left untouched: an
    isolated opening marker is not sufficient evidence of a reasoning
    region, and stripping from it could delete real answer text.
    """
    changed = True
    while changed:
        changed = False
        for open_marker, close_marker in REASONING_BOUNDARY_PAIRS:
            start = text.find(open_marker)
            if start == -1:
                continue
            end = text.find(close_marker, start + len(open_marker))
            if end == -1:
                continue
            text = text[:start] + text[end + len(close_marker):]
            changed = True
            break
    # only trim outer whitespace the markers themselves introduced
    return text.strip()


def extract_stream_response_text(raw_body: bytes) -> str:
    text = raw_body.decode("utf-8", errors="replace")
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            chunk = parsed.get("response")
            if isinstance(chunk, str):
                parts.append(chunk)
    text = "".join(parts).strip()
    if settings.bridge_strip_reasoning:
        text = strip_reasoning_regions(text)
    return text


def extract_stream_references(raw_body: bytes) -> list[dict[str, Any]]:
    """Pull the references list from a /query/stream NDJSON body.

    LightRAG emits references once, as the first line, when
    include_references=True. Returns [] when absent or malformed.
    """
    text = raw_body.decode("utf-8", errors="replace")
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("references"), list):
            return [r for r in parsed["references"] if isinstance(r, dict)]
    return []


def is_openwebui_task_prompt(prompt: str) -> bool:
    """True only for OpenWebUI task prompts, which begin with '### Task:'."""
    return prompt.lstrip().startswith(OPENWEBUI_TASK_PREFIX)


def extract_ollama_history(payload: dict[str, Any], turns: int) -> list[dict[str, str]]:
    """Previous user/assistant turns for LightRAG conversation_history.

    Excludes the final user message (used as the prompt), drops system
    messages and empty/non-string content, keeps the most recent turns*2
    messages, strips appended Sources blocks from assistant turns, and caps
    each message at MAX_HISTORY_MESSAGE_CHARS.
    """
    if turns <= 0:
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    # Drop the final user message that extract_ollama_prompt used as the prompt.
    trimmed = list(messages)
    for i in range(len(trimmed) - 1, -1, -1):
        msg = trimmed[i]
        if isinstance(msg, dict) and msg.get("role") == "user" and isinstance(msg.get("content"), str) and msg["content"].strip():
            del trimmed[i]
            break
    history: list[dict[str, str]] = []
    for msg in trimmed:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        content = msg.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        if SOURCES_MARKER in content:
            content = content.split(SOURCES_MARKER, 1)[0]
        content = content[:MAX_HISTORY_MESSAGE_CHARS]
        if not content.strip():
            continue
        history.append({"role": role, "content": content})
    return history[-(turns * 2):]


async def answer_ollama_bridge_direct(payload: dict[str, Any]) -> str:
    prompt = extract_ollama_prompt(payload)
    if not prompt.strip():
        return ""

    # Task short-circuit (GRAG-12): one bypass call, no retrieval pipeline.
    # A task prompt embeds the chat history OpenWebUI wants summarised, so
    # no conversation history is extracted or attached here. A failing
    # bypass call returns "" and must never fall through to retrieval.
    if settings.bridge_task_shortcircuit and is_openwebui_task_prompt(prompt):
        try:
            resp = await client.proxy(
                "POST",
                "/query/stream",
                body=json.dumps({
                    "query": prompt,
                    "mode": "bypass",
                    "stream": True,
                    "include_references": False,
                }).encode("utf-8"),
                content_type="application/json",
            )
        except Exception:
            return ""
        return extract_stream_response_text(resp.content)

    history = extract_ollama_history(payload, settings.bridge_history_turns)
    try:
        resp = await client.proxy(
            "POST",
            "/query/stream",
            body=json.dumps(build_ollama_bridge_query_payload(prompt, history)).encode("utf-8"),
            content_type="application/json",
        )
    except Exception:
        return ""
    answer = extract_stream_response_text(resp.content)
    if not answer:
        return ""
    if settings.bridge_sources_enabled:
        citations = [{"file_path": ref.get("file_path")} for ref in extract_stream_references(resp.content)]
        answer += build_sources_block(citations)
    return answer


async def answer_ollama_bridge_pipeline(prompt: str, history: list[dict[str, str]] | None = None) -> str:
    """Stage 0 bridge: the full gateway /query pipeline (kept for comparison)."""
    if not prompt.strip():
        return ""
    envelope = await query(
        QueryRequest(
            query=prompt,
            top_k=settings.bridge_top_k,
            conversation_history=history or None,
        )
    )
    data = envelope.data if isinstance(envelope.data, dict) else {}
    answer = data.get("answer") if isinstance(data, dict) else None
    answer = answer.strip() if isinstance(answer, str) else ""
    if not answer:
        return ""
    citations = data.get("citations") if isinstance(data, dict) else None
    if isinstance(citations, list):
        answer += build_sources_block(citations)
    return answer


async def answer_ollama_bridge_prompt(payload: dict[str, Any]) -> str:
    if settings.bridge_backend == "gateway_pipeline":
        prompt = extract_ollama_prompt(payload)
        history = extract_ollama_history(payload, settings.bridge_history_turns)
        return await answer_ollama_bridge_pipeline(prompt, history)
    return await answer_ollama_bridge_direct(payload)


def build_ollama_chat_response(model: str, content: str) -> dict[str, Any]:
    return {
        "model": model,
        "created_at": "2024-01-15T00:00:00Z",
        "message": {
            "role": "assistant",
            "content": content,
            "images": None,
        },
        "done_reason": "stop",
        "done": True,
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": 0,
        "eval_duration": 0,
    }


def build_ollama_generate_response(model: str, content: str) -> dict[str, Any]:
    return {
        "model": model,
        "created_at": "2024-01-15T00:00:00Z",
        "response": content,
        "done": True,
        "done_reason": "stop",
        "context": [],
        "total_duration": 0,
        "load_duration": 0,
        "prompt_eval_count": 0,
        "prompt_eval_duration": 0,
        "eval_count": 0,
        "eval_duration": 0,
    }


async def maybe_handle_ollama_bridge(raw_body: bytes, *, response_kind: str) -> Response | None:
    payload = parse_json_body(raw_body)
    if not payload:
        return None
    model = payload.get("model")
    if model != OLLAMA_BRIDGE_MODEL:
        return None

    answer = await answer_ollama_bridge_prompt(payload)
    if response_kind == "chat":
        body = build_ollama_chat_response(str(model), answer)
    else:
        body = build_ollama_generate_response(str(model), answer)

    media_type = "application/json"
    if request_wants_stream(raw_body):
        media_type = "application/x-ndjson"
        return Response(content=(json.dumps(body) + "\n").encode("utf-8"), media_type=media_type)
    return Response(content=json.dumps(body), media_type=media_type)


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


def is_graph_expansion_target(query: str) -> bool:
    lowered = query.lower()
    return is_transcript_like_query(query) or any(keyword in lowered for keyword in GRAPH_QUERY_KEYWORDS)


def is_graph_native_target(query: str) -> bool:
    return is_graph_expansion_target(query)


def classify_graph_route(query: str) -> str:
    return "graph" if is_graph_native_target(query) else "standard"


def has_weak_answer_signal(answer: str) -> bool:
    lowered = answer.lower()
    return any(marker in lowered for marker in WEAK_ANSWER_MARKERS)


def get_top_retrieval_confidence(citations: list[dict[str, Any]]) -> float:
    best = 0.0
    for citation in citations:
        for key in ("relevance_score", "score", "similarity"):
            value = citation.get(key)
            if isinstance(value, (int, float)):
                best = max(best, float(value))
                break
    return best


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
    retrieval_mode = req.mode or settings.retrieval_mode_default
    transcript_like = is_transcript_like_query(req.query)
    top_k = max(req.top_k, 24) if transcript_like else req.top_k
    chunk_top_k = req.chunk_top_k or settings.chunk_top_k

    # Parse transcript keywords for multi-query trigger
    mq_transcript_keywords = parse_keywords_csv(settings.multi_query_transcript_keywords)

    async def execute_query_pass(
        mode: str,
        query_text: str,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any], dict[str, Any], str | None]:
        """Execute a single retrieval pass and return (answer, citations, query_scope, rerank_meta, weak_reason)."""
        payload: dict[str, Any] = {
            "query": query_text,
            "mode": mode,
            "top_k": top_k,
            "chunk_top_k": chunk_top_k,
            "include_references": True,
            "include_chunk_content": True,
        }
        # History keys are added only when present so the payload stays
        # byte-for-byte identical when no history is threaded. LightRAG
        # (v1.4.15 and v1.5.7) has no history_turns field; turn capping is
        # gateway-side in extract_ollama_history.
        if req.conversation_history:
            payload["conversation_history"] = req.conversation_history
        result = await client.post_json("/query", payload)
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
            reranked_citations, rerank_meta = await rerank_citations(
                query_text,
                scoped_citations,
                settings.rerank_binding_host,
                rerank_api_key=settings.rerank_binding_api_key,
                rerank_model=settings.rerank_model,
            )
            if rerank_meta.get("rerank_applied"):
                scoped_citations = reranked_citations

        scoped_citations = truncate_citations(scoped_citations, top_k=settings.citation_top_k)
        return scoped_citations, rerank_meta

    fallback_used = False
    fallback_reason = None
    fallback_answer_used = False

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

    # ── Fallback (additive) ─────────────────────────────────────────────
    # A weak primary pass ADDS evidence; it does not replace a good answer.
    # A precise answer citing a single document is not a failure, so the
    # naive-mode answer is only used when the primary answer is empty.
    weak_reason = None
    if first_weak_reason and retrieval_mode != "bypass":
        fallback_used = True
        fallback_reason = first_weak_reason
        fallback_mode = "naive"
        primary_answer = answer

        fallback_answer, all_citations_fallback, _, _, _ = await execute_query_pass(
            fallback_mode, req.query
        )

        # Primary citations stay first; fallback evidence is appended.
        groups_fallback = [merged_citations, all_citations_fallback]
        if mq_triggered and not mq_error:
            for rw_query in (rewrites if rewrites else []):
                try:
                    _, rw_citations, _, _, _ = await execute_query_pass(fallback_mode, rw_query)
                    groups_fallback.append(rw_citations)
                except Exception:
                    mq_error = "rewrite_retrieval_failed"
                    break
        merged_citations = merge_and_dedupe_citations(groups_fallback)

        if not primary_answer.strip():
            answer = fallback_answer
            fallback_answer_used = True

    # ── GraphRAG adaptive expansion (after merge, before scope filter) ───
    graph_enabled = settings.graph_expansion_enabled
    graph_error = None
    graph_applied = False
    graph_seed_count = 0
    graph_neighbor_count = 0
    graph_hops_used = 0

    if graph_enabled:
        try:
            if not is_graph_expansion_target(req.query):
                graph_error = "graph_query_family_skipped"
            else:
            # Extract entities from top seed citations
                seed_count_cfg = settings.graph_seed_citation_count
                seed_citations = merged_citations[:seed_count_cfg]
                entities = await extract_entities_via_llm(
                    client, seed_citations, seed_count=seed_count_cfg, query=req.query
                )
                graph_seed_count = len(entities)

                if entities:
                    max_neighbors_cfg = min(settings.graph_expansion_max_neighbors, 3)
                    neighbors_1hop, err_1hop, hops_1 = await expand_graph_neighbors(
                        client, entities, max_neighbors=max_neighbors_cfg, hops=1
                    )
                    graph_error = err_1hop
                    graph_hops_used = 1 if neighbors_1hop else 0

                    if not err_1hop:
                        filtered_neighbors = filter_graph_neighbors_for_query(req.query, neighbors_1hop)
                        graph_neighbor_count = len(filtered_neighbors)

                        # no-improvement-no-merge guard
                        query_terms = {t for t in re.findall(r"[A-Za-z0-9_]+", req.query.lower()) if len(t) > 2}

                        def term_overlaps(items: list[dict[str, Any]]) -> set[str]:
                            overlaps: set[str] = set()
                            for item in items:
                                text = (str(item.get("content", "")) + " " + str(item.get("path", ""))).lower()
                                overlaps.update(term for term in query_terms if term in text)
                            return overlaps

                        base_overlaps = term_overlaps(merged_citations)
                        graph_overlaps: set[str] = set()
                        for item in filtered_neighbors:
                            text = f"{item.get('seed_entity','')} {item.get('related_entity','')} {item.get('relationship','')} {item.get('context','')}".lower()
                            graph_overlaps.update(term for term in query_terms if term in text)

                        if filtered_neighbors and graph_overlaps.issubset(base_overlaps):
                            graph_error = "graph_no_improvement"
                        elif filtered_neighbors:
                            merged_citations = merge_graph_expansion(merged_citations, filtered_neighbors)
                            graph_applied = True
                        else:
                            graph_error = graph_error or "graph_no_improvement"

        except Exception as exc:
            graph_error = str(exc)
            # Fail-open: merged_citations unchanged

    # Build graph metadata (always populated)
    graph_meta = build_graph_metadata(
        enabled=graph_enabled,
        applied=graph_applied,
        error=graph_error,
        seed_count=graph_seed_count,
        neighbor_count=graph_neighbor_count,
        hops_used=graph_hops_used,
    )

    graph_native_enabled = settings.graph_native_enabled
    graph_native_route = classify_graph_route(req.query)
    graph_native_applied = False
    graph_native_error = None
    graph_native_seed_labels: list[str] = []
    graph_native_result_count = 0
    graph_evidence: list[dict[str, Any]] = []

    if graph_native_enabled and graph_native_route == "graph":
        graph_evidence, graph_native_error, graph_native_seed_labels = await fetch_graph_native_evidence(
            client,
            req.query,
            max_seeds=settings.graph_native_max_seeds,
            max_depth=settings.graph_native_max_depth,
            max_nodes=settings.graph_native_max_nodes,
        )
        graph_native_result_count = len(graph_evidence)
        graph_native_applied = graph_native_result_count > 0 and graph_native_error is None
    elif graph_native_enabled:
        graph_native_error = "graph_native_query_family_skipped"

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
        "fallback_answer_used": fallback_answer_used,
        "explicit_mode": explicit_mode,
        "transcript_like_query": transcript_like,
        "top_k_used": top_k,
        "chunk_top_k_used": chunk_top_k,
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

    # Add graph expansion metadata
    query_scope.update(graph_meta)
    query_scope.update(
        build_graph_native_metadata(
            enabled=graph_native_enabled,
            applied=graph_native_applied,
            error=graph_native_error,
            seed_labels=graph_native_seed_labels,
            result_count=graph_native_result_count,
        )
    )
    query_scope["graph_native_route"] = graph_native_route

    combined_evidence = fuse_graph_and_vector_evidence(final_citations, graph_evidence)

    graph_synthesis_applied = False
    graph_synthesis_error = None
    if not settings.graph_synthesis_replace_answer:
        graph_synthesis_error = "graph_synthesis_replacement_disabled"
    elif graph_native_applied and combined_evidence:
        synthesized_answer, graph_synthesis_error = await synthesize_graph_aware_answer(client, req.query, combined_evidence)
        if is_graph_synthesis_answer_usable(synthesized_answer):
            answer = synthesized_answer
            graph_synthesis_applied = True
        elif synthesized_answer:
            graph_synthesis_error = "weak_graph_synthesis_answer"

    query_scope.update({
        "graph_synthesis_applied": graph_synthesis_applied,
        "graph_synthesis_error": graph_synthesis_error,
    })

    if requested_company is not None:
        query_scope["warning"] = "Company scoping is enforced on gateway-returned citations only; this packet does not claim hard isolation inside LightRAG itself"

    return ok({"answer": answer, "citations": final_citations, "graph_evidence": graph_evidence, "combined_evidence": combined_evidence, "query_scope": query_scope})


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
    bridged = await maybe_handle_ollama_bridge(body, response_kind="generate")
    if bridged is not None:
        return bridged
    resp = await client.proxy("POST", "/api/generate", body=body, content_type=request.headers.get("content-type"))
    content = resp.content
    media_type = resp.headers.get("content-type", "application/json")
    if request_wants_stream(body):
        content = normalize_ollama_stream_response(resp.content, response_key="response")
        media_type = "application/x-ndjson"
    return Response(content=content, media_type=media_type)


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.body()
    bridged = await maybe_handle_ollama_bridge(body, response_kind="chat")
    if bridged is not None:
        return bridged
    resp = await client.proxy("POST", "/api/chat", body=body, content_type=request.headers.get("content-type"))
    content = resp.content
    media_type = resp.headers.get("content-type", "application/json")
    if request_wants_stream(body):
        content = normalize_ollama_stream_response(resp.content, response_key="message")
        media_type = "application/x-ndjson"
    return Response(content=content, media_type=media_type)
