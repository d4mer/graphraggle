"""GL-10 part 3: make the retrieval itself respect the company the question named.

Why this exists
---------------
Filtering the citations after LightRAG has answered does not keep another company out of the
answer; it only hides which documents the answer came from. The operator's rule - name a company
and the answer is that company's - needs the model to be shown only that company's material.

What this module does is the one shape that is already proven in this repo: retrieve with
`include_chunk_content` (which the gateway already asks for, and whose response shape was confirmed
on stack2: `references[].content` is a list of chunk strings), drop every reference whose document
row does not belong to a named company, and answer again from what is left using `mode: bypass` -
the retrieve-then-generate pattern `app/generation.py` already ships. No upstream capability is
assumed that this repo has not already exercised.

The two honest costs, stated here because they decide whether the operator turns this on:

- **A second model call.** Retrieval and generation are no longer one upstream request. On the
  bridge path the answer also stops streaming (LightRAG's `/query/stream` is replaced by this
  two-step pass), so Open WebUI shows the reply at the end instead of token by token.
- **Attribution is the gate.** A reference is kept only when the gateway can join its `file_path`
  to a `documents` row for a named company. Citations from the upload door carry a basename
  (`app/lightrag_client.py:25-31`), which can never equal the stored path, so they are
  unattributable and are dropped. That is the safe direction - an unattributable citation cannot be
  claimed as this company's - but on an upload-ingested corpus it can empty the context, which is
  why `unattributable` is counted and logged rather than swallowed.

When nothing attributable survives, the answer says so. Inventing an answer from another company's
material, or returning LightRAG's unscoped draft because the scoped one was empty, is the failure
this packet exists to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import document_identity
from .citation_text import CITATION_TEXT_KEYS, citation_content
from .config import settings

# Path keys and the shared citation→document join live in app/document_identity.py: LightRAG returns a
# bare basename, the store holds an absolute path, and every door must resolve that the same way or the
# same citation is attributed in one place and unattributable in another. `path_of` is kept as this
# module's name for the accessor.
PATH_KEYS = document_identity.PATH_KEYS
path_of = document_identity.citation_path


def error_detail(exc: BaseException) -> dict[str, Any]:
    """What a LightRAG rejection said, in a form that belongs in a log line.

    A rejected request body is a validation problem, so the response body is the diagnosis: status
    code plus the server's message. Chunk text never appears in a 4xx body, and the message is
    truncated anyway so an oversized or unexpected body cannot push document content into the log.
    """
    detail: dict[str, Any] = {"type": type(exc).__name__}
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        detail["status"] = status
    body = getattr(response, "text", None)
    if isinstance(body, str) and body.strip():
        detail["message"] = " ".join(body.split())[:300]
    return detail


def normalize_data_payload(payload: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn a `/query/data` body into reference-shaped items, plus a census of what was there.

    `/query/data` returns `data{entities, relationships, chunks, references}` with no generation, and
    `include_chunk_content` puts chunk text into the reference items. Which of the two lists carries
    the text, and under which key, has not been observed on this stack - so both are read, both are
    counted, and the caller decides whether the result is usable. Chunk items are grouped by their
    document path, because the scoping decision is per document, not per chunk. A chunk is never
    invented a path it does not carry: an item with text and no path stays pathless and is then
    counted as unattributable, exactly as an unattributable citation is.
    """
    census: dict[str, Any] = {"top_level_keys": sorted(payload) if isinstance(payload, dict) else [],
                              "data_keys": [], "references_found": 0, "chunks_found": 0,
                              "items_with_text": 0, "items_without_path": 0,
                              "chunk_only_paths": 0, "content_keys_seen": {},
                              "path_keys_seen": {}}
    if not isinstance(payload, dict):
        return [], census
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload                                    # tolerate a body with no envelope
    census["data_keys"] = sorted(data)

    def note_path(item: dict[str, Any]) -> None:
        for key in PATH_KEYS:
            if isinstance(item.get(key), str) and item[key].strip():
                census["path_keys_seen"][key] = census["path_keys_seen"].get(key, 0) + 1
                break

    items: list[dict[str, Any]] = []
    paths_with_text: set[str] = set()
    references = data.get("references")
    if isinstance(references, list):
        for reference in references:
            if not isinstance(reference, dict):
                continue
            census["references_found"] += 1
            note_path(reference)
            text = citation_content(reference)
            if text:
                census["items_with_text"] += 1
                paths_with_text.add(path_of(reference))
                for key in CITATION_TEXT_KEYS:
                    if reference.get(key) is not None:
                        census["content_keys_seen"][key] = census["content_keys_seen"].get(key, 0) + 1
            items.append(reference)

    chunks = data.get("chunks")
    if isinstance(chunks, list):
        grouped: dict[str, list[str]] = {}
        order: list[str] = []
        for chunk in chunks:
            census["chunks_found"] += 1
            note_path(chunk)
            text = citation_content(chunk)
            if not text:
                continue
            path = path_of(chunk) or ""
            if path in paths_with_text:
                continue                                   # the reference already carries this doc
            if path not in grouped:
                grouped[path] = []
                order.append(path)
            grouped[path].append(text)
        for path in order:
            census["chunk_only_paths"] += 1
            census["items_with_text"] += 1
            if not path:
                census["items_without_path"] += 1
            items.append({"file_path": path, "content": grouped[path]})
    census["content_key_counts"] = census.pop("content_keys_seen")
    return items, census


def apply_keyword_hint(payload: dict[str, Any], companies: Sequence[str]) -> bool:
    """Add the named companies to the retrieval keywords. Returns whether anything was added.

    The bridge already supplies `hl_keywords`/`ll_keywords`, so the keys are known, and a payload may
    already carry question keywords - those are kept and the company names appended, never replaced.
    This cannot exclude anything: the state-join filter is what excludes. It is a separate opt-in whose
    only claim is to pull the right company's chunks into a bounded retrieval pool.
    """
    names = [name for name in companies if name]
    if not names:
        return False
    added = False
    for key in ("hl_keywords", "ll_keywords"):
        existing = payload.get(key)
        merged = [item for item in (existing if isinstance(existing, list) else [])
                  if isinstance(item, str)]
        before = len(merged)
        merged += [name for name in names if name not in merged]
        payload[key] = merged
        added = added or len(merged) > before
    return added


async def retrieve_context(question: str, payload_base: dict[str, Any], client: Any,
                           companies: Sequence[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Get the chunk text and document paths WITHOUT paying for an answer that will be thrown away.

    `/query/data` is the retrieval-only endpoint; a scoped question then costs one retrieval and one
    generation, the same as an unscoped one. It is tried first and abandoned on evidence, not on
    assumption: if the call fails, or the body carries no usable chunk text, the pass falls back to
    the `/query` retrieval it used before (which does cost an extra generation) and records why.
    """
    # Counted separately from the answer the pass later writes, so a run that declines before
    # generating does not report a generation it never made.
    meta: dict[str, Any] = {"retrieval_route": "query/data", "retrieval_fallback_reason": None,
                            "keyword_hint_applied": False, "retrieval_model_calls": 0,
                            "retrieval_errors": {}}
    # `query` is set here rather than trusted from payload_base: the gateway door passes
    # mode/top_k/chunk_top_k and the bridge door strips `query` deliberately, so a retrieval built
    # from either base without the question is rejected by LightRAG (400 on /query/data, 422 on
    # /query). The question is what is being retrieved; it belongs to the pass, not to the caller.
    payload = {**payload_base, "query": question, "include_references": True,
               "include_chunk_content": True}
    if settings.company_keyword_hint:
        meta["keyword_hint_applied"] = apply_keyword_hint(payload, companies)
    try:
        body = await client.post_json("/query/data", payload)
    except Exception as exc:
        meta["retrieval_route"] = "query"
        meta["retrieval_errors"]["/query/data"] = error_detail(exc)
        status = meta["retrieval_errors"]["/query/data"].get("status")
        meta["retrieval_fallback_reason"] = (f"query_data_http_{status}" if status
                                             else f"query_data_error:{type(exc).__name__}")
        return await _retrieve_via_query(question, payload_base, client, meta, companies)

    items, census = normalize_data_payload(body)
    meta["retrieval_shape"] = census
    if not any(citation_content(item) for item in items):
        meta["retrieval_route"] = "query"
        meta["retrieval_fallback_reason"] = ("query_data_no_chunks" if items
                                             else "query_data_no_items")
        return await _retrieve_via_query(question, payload_base, client, meta, companies)
    return items, meta


async def _retrieve_via_query(question: str, payload_base: dict[str, Any], client: Any,
                              meta: dict[str, Any],
                              companies: Sequence[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The route this code used before: retrieve through the answered endpoint, and pay for it."""
    payload = {**payload_base, "query": question, "include_references": True,
               "include_chunk_content": True}
    if settings.company_keyword_hint:
        meta["keyword_hint_applied"] = apply_keyword_hint(payload, companies)
    try:
        body = await client.post_json("/query", payload)
    except Exception as exc:
        # Both retrieval routes failed. Raising here turns a LightRAG rejection into an HTTP 500 for
        # the user, which is what the first stack2 R2 run saw. The caller degrades to the
        # citations-only behaviour it already ships, with the status codes in the log.
        meta["retrieval_errors"]["/query"] = error_detail(exc)
        status = meta["retrieval_errors"]["/query"].get("status")
        meta["retrieval_failed"] = True
        meta["retrieval_fallback_reason"] = (f"query_http_{status}" if status
                                             else f"query_error:{type(exc).__name__}")
        return [], meta
    references = body.get("references") if isinstance(body, dict) else None
    items = [item for item in (references or []) if isinstance(item, dict)]
    meta["retrieval_shape"] = {"references_found": len(items),
                               "items_with_text": sum(1 for item in items if citation_content(item))}
    # The answered endpoint cannot retrieve without generating, so this route always costs one model
    # call whose text is thrown away.
    meta["retrieval_model_calls"] = 1
    return items, meta


def no_scoped_material_message(companies: list[str]) -> str:
    """Said out loud when the named companies' documents hold nothing for the question."""
    if len(companies) == 1:
        return (f"I could not find an answer to that in the {companies[0]} documents I was given. "
                "I have not answered from any other company's material.")
    joined = ", ".join(companies[:-1]) + " and " + companies[-1]
    return (f"I could not find an answer to that in the documents of {joined}. "
            "I have not answered from any other company's material.")


def scoped_generation_prompt(question: str, companies: list[str], context: str) -> str:
    """The instruction that makes the second call answer from the scoped material only."""
    joined = ", ".join(companies)
    return (
        f"Answer the question using ONLY the material below. That material belongs to: {joined}. "
        "Do not use knowledge about any other company, and do not name a document that is not "
        "listed. If the material does not answer the question, say that you could not find it in "
        f"these documents.\n\nQuestion: {question}\n\nMaterial:\n{context}\n\n"
        "Answer:"
    )


@dataclass
class ScopedPass:
    """Result of one company-scoped retrieve -> filter -> generate pass."""

    answer: str
    citations: list[dict[str, Any]] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    used: bool = False          # False = the pass declined and the caller's normal path stands


async def companies_for_paths(paths: list[str]) -> dict[str, set[str]]:
    """Compatibility wrapper: the companies that own each citation name.

    Kept for callers that only want the company set. It cannot express ambiguity, which is why the
    scoped pass uses `document_identity.ownership_for_paths` directly.
    """
    ownership = await document_identity.ownership_for_paths(paths)
    return {path: set(entry.companies) for path, entry in ownership.items()}


def classify_references(
    references: list[dict[str, Any]],
    owners_by_path: dict[str, set[str]],
    allowed_companies: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split references into the named companies' and the rest.

    A reference is kept when the gateway can show that one of the named companies owns that path. A
    path with no row, or whose rows carry no company, is `unattributable` and goes: the gateway
    cannot claim it belongs to the company asked about, and a citation shown under the wrong company
    is worse than a citation withheld.
    """
    allowed = set(allowed_companies)
    kept: list[dict[str, Any]] = []
    counts = {"total": len(references), "kept": 0, "other_company": 0, "unattributable": 0,
              "ambiguous": 0}
    for reference in references:
        path = path_of(reference)
        entry = owners_by_path.get(path)
        companies = set(entry.companies) if entry is not None else set()
        if entry is not None and entry.ambiguous:
            # The same name, different bytes, different companies: no honest answer exists, so none is
            # given. This is deliberately its own count - "we cannot tell" is not "this has no owner".
            counts["ambiguous"] += 1
            continue
        if not companies:
            counts["unattributable"] += 1
            continue
        if companies & allowed:
            kept.append(reference)
            counts["kept"] += 1
        else:
            counts["other_company"] += 1
    return kept, counts


def build_scoped_context(references: list[dict[str, Any]], max_chars: int) -> tuple[str, dict[str, int]]:
    """Concatenate the kept references' chunk text, whole references only, under the cap.

    Dropping whole references rather than truncating one keeps a citation in the prompt complete or
    absent, so a sentence cannot be cut off and then quoted as if it were evidence.
    """
    parts: list[str] = []
    used_chars = 0
    dropped_by_cap = 0
    without_content = 0
    for reference in references:
        content = citation_content(reference)
        if not content:
            without_content += 1
            continue
        block = f"Source: {reference.get('file_path') or reference.get('path') or 'unknown'}\n{content}"
        if used_chars + len(block) > max_chars:
            dropped_by_cap += 1
            continue
        parts.append(block)
        used_chars += len(block)
    return "\n\n".join(parts), {"context_chars": used_chars, "dropped_by_cap": dropped_by_cap,
                                "without_content": without_content}


async def company_scoped_pass(
    question: str,
    allowed_companies: list[str],
    *,
    payload_base: dict[str, Any],
    client: Any,
) -> ScopedPass:
    """Retrieve, restrict to the named companies' documents, then answer from what remains."""
    if not allowed_companies:
        return ScopedPass(answer="", used=False)

    references, retrieval_meta = await retrieve_context(question, payload_base, client,
                                                         allowed_companies)

    if retrieval_meta.get("retrieval_failed"):
        # Neither retrieval route worked. Answer as `citations` enforcement does - the caller's
        # unscoped pass plus the company filter on the citations - instead of failing the request.
        # used=False is what tells the caller that; the meta says why.
        statuses = ";".join(
            f"{path}={detail.get('status') or detail['type']}"
            for path, detail in sorted(retrieval_meta["retrieval_errors"].items()))
        return ScopedPass(answer="", citations=[], used=False, meta={
            "companies": list(allowed_companies),
            "retrieval_route": retrieval_meta["retrieval_route"],
            "retrieval_fallback_reason": retrieval_meta["retrieval_fallback_reason"],
            "retrieval_errors": retrieval_meta["retrieval_errors"],
            "model_calls": retrieval_meta.get("retrieval_model_calls", 0),
            "keyword_hint_applied": retrieval_meta["keyword_hint_applied"],
            "second_model_call": False,
            "declined_reason": f"retrieval_routes_failed:{statuses}" if statuses
                               else "retrieval_routes_failed",
        })

    paths = sorted({path_of(reference) for reference in references} - {""})
    owners_by_path = await document_identity.ownership_for_paths(paths)
    kept, counts = classify_references(references, owners_by_path, allowed_companies)
    context, context_meta = build_scoped_context(kept, settings.company_scoped_context_max_chars)

    meta = {
        "companies": list(allowed_companies),
        "references_total": counts["total"],
        "references_kept": counts["kept"],
        "references_dropped_other_company": counts["other_company"],
        "references_unattributable": counts["unattributable"],
        "references_ambiguous": counts["ambiguous"],
        "references_dropped_by_context_cap": context_meta["dropped_by_cap"],
        "references_without_content": context_meta["without_content"],
        "context_chars": context_meta["context_chars"],
        "context_max_chars": settings.company_scoped_context_max_chars,
        # `second_model_call` is kept as the log/scorer key it already is. Under `/query/data` it is
        # the only model call the pass makes - `model_calls` says which - and it is genuinely the
        # second one only on the `/query` fallback route.
        "second_model_call": False,
        "model_calls": retrieval_meta.get("retrieval_model_calls", 0),
        "retrieval_route": retrieval_meta["retrieval_route"],
        "retrieval_fallback_reason": retrieval_meta["retrieval_fallback_reason"],
        "keyword_hint_applied": retrieval_meta["keyword_hint_applied"],
        "retrieval_shape": retrieval_meta.get("retrieval_shape", {}),
        # A rejected body is diagnosable only if the status and the server's message survive; this is
        # the case where /query/data failed and the /query fallback carried the run.
        "retrieval_errors": retrieval_meta.get("retrieval_errors", {}),
        "declined_reason": None,
    }

    if not kept:
        if not counts["total"]:
            meta["declined_reason"] = "no_references_returned"
        elif counts["ambiguous"] and not counts["unattributable"]:
            # Everything retrieved was a name shared by different companies' files. Refusing is the
            # only honest move, and the reason says so rather than blaming a missing join.
            meta["declined_reason"] = "every_reference_is_ambiguous_across_companies"
        else:
            meta["declined_reason"] = "no_reference_joins_a_named_company"
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=[],
                          meta=meta, used=True)
    if not context:
        # Documents for that company were retrieved but carried no chunk text, so there is nothing
        # honest to answer from. Showing the documents is still correct; guessing is not.
        meta["declined_reason"] = "kept_references_carry_no_chunk_text"
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=kept,
                          meta=meta, used=True)

    meta["second_model_call"] = True
    meta["model_calls"] = retrieval_meta.get("retrieval_model_calls", 0) + 1
    generation = await client.post_json(
        "/query",
        {"query": scoped_generation_prompt(question, allowed_companies, context),
         "mode": "bypass",
         "include_references": False},
    )
    answer = str(generation.get("response", "")).strip()
    if not answer:
        # A blank regeneration must not become a silent pass-through of the unscoped draft: the
        # caller sees used=True with the honest message and the citations that do belong here.
        meta["declined_reason"] = "regeneration_returned_no_text"
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=kept,
                          meta=meta, used=True)
    return ScopedPass(answer=answer, citations=kept, meta=meta, used=True)
