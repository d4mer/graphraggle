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
                              "chunk_only_paths": 0, "chunk_only_used": 0, "chunk_only_skipped": 0,
                              "content_keys_seen": {},
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
        # The chunk list is used only when the document-shaped list carries no text at all. It is never
        # appended below references that DO carry text: `references[]` and `chunks[]` are two separate
        # rankings, and merging them into one context ordering would silently rank one system's output
        # above the other's - and a chunk that landed at the end could be the one holding the answer. A
        # chunk-shaped body is read in chunk order, which is retrieval order there. Either way the count
        # is reported, so a shape that needs the fallback is visible in the log before it costs a run.
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
        census["chunk_only_paths"] = len(order)
        if order and not paths_with_text:
            census["chunk_only_used"] = len(order)
            for path in order:
                census["items_with_text"] += 1
                if not path:
                    census["items_without_path"] += 1
                items.append({"file_path": path, "content": grouped[path]})
        elif order:
            census["chunk_only_skipped"] = len(order)
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


def _scaled_sizes(payload_base: dict[str, Any], multiplier: int) -> tuple[dict[str, int], dict[str, Any]]:
    """Ask for more candidates than we can keep, because the company filter throws most of them away.

    Ranked retrieval over the whole index at the ordinary top_k/chunk_top_k, then a filter that removes
    ~40% of the survivors (other company, unattributable, ambiguous), leaves a named company with a thin -
    sometimes answer-less - context. Stack2 cutover-9 measured the consequence: an honest model refused 21
    answerable questions it used to guess at. The lever is recall BEFORE filtering, not another prompt
    line.

    Scaling only touches keys the door already sets, and stops at `COMPANY_SCOPED_RETRIEVAL_CEILING` so a
    multiplier cannot silently ask upstream for an unbounded pool. With multiplier 1 (the shipped default)
    the payload is not touched at all: the request stays byte-for-byte what it was.
    """
    bookkeeping: dict[str, Any] = {
        "multiplier": multiplier,
        "ceiling": settings.company_scoped_retrieval_ceiling,
        "base_top_k": payload_base.get("top_k"),
        "base_chunk_top_k": payload_base.get("chunk_top_k"),
        "top_k_requested": payload_base.get("top_k"),
        "chunk_top_k_requested": payload_base.get("chunk_top_k"),
        "scaled": False,
        "not_scaled_reason": None,
    }
    if multiplier <= 1:
        bookkeeping["not_scaled_reason"] = "multiplier_1"
        return {}, bookkeeping
    ceiling = settings.company_scoped_retrieval_ceiling
    sizes: dict[str, int] = {}
    for key in ("top_k", "chunk_top_k"):
        base = payload_base.get(key)
        if not isinstance(base, int) or base <= 0:
            continue
        sizes[key] = min(base * multiplier, ceiling)
    if not sizes:
        bookkeeping["not_scaled_reason"] = "no_sizes_in_payload_base"
        return {}, bookkeeping
    bookkeeping["scaled"] = any(sizes[key] != payload_base.get(key) for key in sizes)
    bookkeeping["top_k_requested"] = sizes.get("top_k", bookkeeping["top_k_requested"])
    bookkeeping["chunk_top_k_requested"] = sizes.get("chunk_top_k", bookkeeping["chunk_top_k_requested"])
    return sizes, bookkeeping


async def retrieve_context(question: str, payload_base: dict[str, Any], client: Any,
                           companies: Sequence[str], *,
                           multiplier: int | None = None,
                           allow_query_fallback: bool = True) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Get the chunk text and document paths WITHOUT paying for an answer that will be thrown away.

    `/query/data` is the retrieval-only endpoint; a scoped question then costs one retrieval and one
    generation, the same as an unscoped one. It is tried first and abandoned on evidence, not on
    assumption: if the call fails, or the body carries no usable chunk text, the pass falls back to
    the `/query` retrieval it used before (which does cost an extra generation) and records why.

    `multiplier` overrides COMPANY_SCOPED_RETRIEVAL_MULTIPLIER for this round (the widening round asks
    twice as wide). `allow_query_fallback=False` keeps a round retrieval-only: the wider round must not
    turn a row that cost nothing into one that costs a discarded generation, so if `/query/data` fails
    on that round the round simply returns empty and the first round's evidence stands.
    """
    # Counted separately from the answer the pass later writes, so a run that declines before
    # generating does not report a generation it never made.
    meta: dict[str, Any] = {"retrieval_route": "query/data", "retrieval_fallback_reason": None,
                            "keyword_hint_applied": False, "retrieval_model_calls": 0,
                            "retrieval_errors": {}, "retrieval_calls": 0}
    sizes, sizes_log = _scaled_sizes(payload_base, multiplier if multiplier is not None
                                     else max(1, settings.company_scoped_retrieval_multiplier))
    meta["retrieval_sizes"] = sizes_log
    # `query` is set here rather than trusted from payload_base: the gateway door passes
    # mode/top_k/chunk_top_k and the bridge door strips `query` deliberately, so a retrieval built
    # from either base without the question is rejected by LightRAG (400 on /query/data, 422 on
    # /query). The question is what is being retrieved; it belongs to the pass, not to the caller.
    payload = {**payload_base, **sizes, "query": question, "include_references": True,
               "include_chunk_content": True}
    if settings.company_keyword_hint:
        meta["keyword_hint_applied"] = apply_keyword_hint(payload, companies)
    meta["retrieval_calls"] += 1
    try:
        body = await client.post_json("/query/data", payload)
    except Exception as exc:
        meta["retrieval_errors"]["/query/data"] = error_detail(exc)
        status = meta["retrieval_errors"]["/query/data"].get("status")
        reason = f"query_data_http_{status}" if status else f"query_data_error:{type(exc).__name__}"
        if not allow_query_fallback:
            meta["retrieval_fallback_reason"] = f"wider_round_no_query_fallback:{reason}"
            meta["retrieval_shape"] = {"references_found": 0, "chunks_found": 0, "items_with_text": 0}
            return [], meta
        meta["retrieval_route"] = "query"
        meta["retrieval_fallback_reason"] = reason
        return await _retrieve_via_query(question, payload_base, client, meta, companies, sizes)

    items, census = normalize_data_payload(body)
    meta["retrieval_shape"] = census
    if not any(citation_content(item) for item in items):
        reason = "query_data_no_chunks" if items else "query_data_no_items"
        if not allow_query_fallback:
            meta["retrieval_fallback_reason"] = f"wider_round_no_query_fallback:{reason}"
            return items, meta
        meta["retrieval_route"] = "query"
        meta["retrieval_fallback_reason"] = reason
        return await _retrieve_via_query(question, payload_base, client, meta, companies, sizes)
    return items, meta


async def _retrieve_via_query(question: str, payload_base: dict[str, Any], client: Any,
                              meta: dict[str, Any],
                              companies: Sequence[str],
                              sizes: dict[str, int] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """The route this code used before: retrieve through the answered endpoint, and pay for it."""
    payload = {**payload_base, **(sizes or {}), "query": question, "include_references": True,
               "include_chunk_content": True}
    if settings.company_keyword_hint:
        meta["keyword_hint_applied"] = apply_keyword_hint(payload, companies)
    meta["retrieval_calls"] += 1
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


# The refusal the gateway gives is one wording family, and it deliberately contains the phrase
# app/api.py:WEAK_ANSWER_MARKERS already looks for ("do not have enough information"). A refusal in
# invented words is a refusal the gateway, the operator's tooling and the run scorer cannot see: the
# scoped pass answers 0/12 unanswerable questions on stack2 partly because the only refusal instruction
# in the prompt asked for words nothing downstream recognises.
REFUSAL_SENTENCE = "I do not have enough information in these documents to answer that."


def no_scoped_material_message(companies: list[str]) -> str:
    """Said out loud when the named companies' documents hold nothing for the question.

    Carries the weak-answer marker phrase so this refusal is recognisable by the same detector the
    normal path's refusals are recognisable by, and names the companies that were searched so the
    reader knows what "these documents" means.
    """
    if len(companies) == 1:
        where = f"the {companies[0]} documents I was given"
    else:
        joined = ", ".join(companies[:-1]) + " and " + companies[-1]
        where = f"the documents of {joined}"
    return (f"I could not find an answer to that in {where}: I do not have enough information in them "
            "to answer it. I have not answered from any other company's material.")


HISTORY_BLOCK_MAX_CHARS = 2000
HISTORY_BLOCK_MESSAGE_CHARS = 400


def history_block(payload_base: dict[str, Any]) -> tuple[str, int]:
    """Earlier turns, as prompt context for WHAT the question is asking - never as evidence.

    A follow-up like "and what about their second contract?" only resolves against the previous turn, and
    the scoped retrieval already gets that history - so without it here the pass retrieves the right
    material and then asks the model a question it cannot see the referent of.

    Only USER turns are embedded, for a safety reason: an assistant turn in the history may be an answer
    this gateway produced before any company boundary existed, and pasting it into a scoped generation
    would put another company's text back in front of the model - the exact thing the pass exists to stop.
    Earlier questions carry the referent without carrying any company's facts. The full history is still
    sent upstream in the payload, as the normal path does; this block is what makes the instruction
    self-sufficient whatever upstream does with that field under `mode: bypass`.
    """
    history = payload_base.get("conversation_history")
    if not isinstance(history, list):
        return "", 0
    lines: list[str] = []
    used = 0
    for entry in history:
        if not isinstance(entry, dict) or entry.get("role") != "user":
            continue
        content = entry.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        line = " ".join(content.split())[:HISTORY_BLOCK_MESSAGE_CHARS]
        cost = len(line) + 2
        if used + cost > HISTORY_BLOCK_MAX_CHARS:
            break
        lines.append(f"- {line}")
        used += cost
    if not lines:
        return "", 0
    return "\n".join(lines), len(lines)


def scoped_generation_prompt(question: str, companies: list[str], context: str,
                             history: str = "") -> str:
    """The instruction that makes the generation call answer from the scoped material, or refuse.

    Why it is this long. `mode: bypass` sends the LLM the text we hand it and nothing else: LightRAG's
    own retrieval prompt - the one that carries its "if you do not have enough information, say so"
    instruction and its reference discipline - never runs. So the scoped pass has to supply that
    discipline itself, in the same terms, or the model treats the material as a starting point and
    finishes the answer from what it already believes it knows about the company. That is exactly the
    stack2 result: no leak, no extra latency, and a hallucination count that went UP (R1 37 -> R2 42).

    Three things the prompt must do, in this order: bound the evidence (markers, no outside knowledge),
    ask for the coverage check BEFORE the answer, and name the exact refusal sentence, so a refusal is
    a fixed string the gateway can detect rather than a phrasing the model invents.
    """
    joined = ", ".join(companies)
    return (
        "You are answering from a bounded set of documents, and from nothing else.\n"
        f"The material below is the complete evidence available for this question. It belongs to: "
        f"{joined}.\n"
        "\n"
        "Rules:\n"
        "1. Use only the text between MATERIAL START and MATERIAL END. Do not use what you know about "
        "these companies, their products, their industry, or any other company.\n"
        "2. First decide whether the material states the specific fact being asked about. If it does "
        "not, reply with exactly this sentence and nothing else:\n"
        f"   {REFUSAL_SENTENCE}\n"
        "3. Do not guess, estimate, or supply a name, number, date, code or document title that does "
        "not appear in the material. A plausible answer that is not in the material is a wrong answer, "
        "and refusing costs less than inventing.\n"
        "4. Attribute every fact you state to one of the Source lines below, naming that file. Never "
        "cite a document that is not listed.\n"
        "5. If the material answers only part of the question, answer that part and then say which "
        "part is not in these documents.\n"
        "\n"
        + (f"Earlier questions in this conversation. They say what the question refers to; they are NOT "
           f"evidence, and you must not answer from them:\n{history}\n" if history else "")
        + f"Question: {question}\n"
        "\n"
        "MATERIAL START\n"
        f"{context}\n"
        "MATERIAL END\n"
        "\n"
        "Answer:"
    )


def hard_refusal_verdict(question: str, context: str) -> str | None:
    """`no_support_for_asked_identifier` when the question names an identifier the context lacks.

    This is the one unanswerable case the gateway can decide for itself instead of asking the model to
    be disciplined about it: the question names a code (NC-3617) and that code appears nowhere in the
    material the model was given. The matcher is the shipped one - `app/grounding.py`'s identifier and
    verbatim-token rules, the same vocabulary the M1 gate reports in - not a new one invented here.

    A question with no identifier in it returns None: absence of a code proves nothing, and this check
    must not become a hunch. Returns None rather than a verdict when everything asked is supported.
    """
    from .grounding import identifiers, verbatim_support

    asked = identifiers(question or "")
    if not asked:
        return None
    if all(verbatim_support(code, context) for code in asked):
        return None
    return "no_support_for_asked_identifier"


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


CONTEXT_CUT_MARKER = "[... material cut here by the gateway context cap ...]"


def _truncate_block(block: str, limit: int) -> str:
    """Cut a block to `limit` at a line boundary, with the cut marked.

    The marker is not decoration: an unmarked cut block can be quoted as if it were complete evidence.
    """
    room = max(0, limit - len(CONTEXT_CUT_MARKER) - 1)
    cut = block[:room]
    header_end = block.find("\n")
    boundary = cut.rfind("\n")
    if boundary > header_end:
        # Trim to a line boundary - but never past the "Source:" line, which would cut the block down
        # to a label and no material at all.
        cut = cut[:boundary]
    return (cut.rstrip() + "\n" + CONTEXT_CUT_MARKER)[:limit]


def build_scoped_context(references: list[dict[str, Any]],
                         max_chars: int) -> tuple[str, dict[str, int]]:
    """Concatenate the kept references' chunk text, in retrieval order, under the cap.

    The two rules, because "apply a cap" hides a decision about which evidence the model is allowed to
    see:

    - **The kept set is a prefix of the retrieval order.** The old loop skipped a reference that did not
      fit and kept the smaller ones below it, so a lower-ranked chunk could be in the prompt while a
      higher-ranked one from the same retrieval was not - which makes an answer unreproducible and makes
      "the answer chunk was dropped" undetectable from the counts. Now the first reference that does not
      fit ends the fill, and everything below it is counted as dropped (`first_dropped_index` says where).
    - **A block is included whole, except at the top.** Whole-or-absent keeps a citation in the prompt
      complete or missing, so a sentence cannot be cut off and quoted as if it were evidence. The one
      exception is the highest-ranked block: if it alone exceeds the cap, dropping it and answering from
      nothing would be the worse failure, so it is cut to the budget with `CONTEXT_CUT_MARKER` in it.

    Counted: `blocks_kept`, `dropped_by_cap`, `truncated_blocks`, `unused_chars` (budget left over when
    the fill stopped) and `first_dropped_index` (-1 when nothing was dropped).
    """
    parts: list[str] = []
    used_chars = 0
    dropped_by_cap = 0
    truncated_blocks = 0
    without_content = 0
    first_dropped_index = -1
    fill_closed = False
    for index, reference in enumerate(references):
        content = citation_content(reference)
        if not content:
            without_content += 1
            continue
        block = f"Source: {reference.get('file_path') or reference.get('path') or 'unknown'}\n{content}"
        if fill_closed or used_chars + len(block) > max_chars:
            if not parts:
                # Highest-ranked block, bigger than the whole budget: keep it cut, rather than answer
                # from nothing while claiming the company's documents were searched.
                block = _truncate_block(block, max_chars)
                truncated_blocks += 1
                parts.append(block)
                used_chars += len(block)
                fill_closed = True
                continue
            dropped_by_cap += 1
            if first_dropped_index < 0:
                first_dropped_index = index
            fill_closed = True
            continue
        parts.append(block)
        used_chars += len(block)
    return "\n\n".join(parts), {"context_chars": used_chars, "dropped_by_cap": dropped_by_cap,
                                "without_content": without_content, "blocks_kept": len(parts),
                                "truncated_blocks": truncated_blocks,
                                "unused_chars": max(0, max_chars - used_chars),
                                "first_dropped_index": first_dropped_index}


def _round_record(round_number: int, retrieval_meta: dict[str, Any], counts: dict[str, int],
                  context_meta: dict[str, int]) -> dict[str, Any]:
    """One retrieval round as the log must show it: asked / returned / kept.

    Without the ASKED numbers a thin context is undiagnosable: "this company has no such document"
    and "we asked for 20 chunks out of a five-company index and 11 of them belonged to other
    companies" look identical in the counts, and they want opposite fixes. Counted per round rather
    than summed, because the only way to see whether the multiplier pays for itself is to compare
    round 1 against round 2 in the same row.
    """
    sizes = retrieval_meta.get("retrieval_sizes") or {}
    shape = retrieval_meta.get("retrieval_shape") or {}
    return {
        "round": round_number,
        "multiplier": sizes.get("multiplier"),
        "top_k_requested": sizes.get("top_k_requested"),
        "chunk_top_k_requested": sizes.get("chunk_top_k_requested"),
        "scaled": sizes.get("scaled"),
        "not_scaled_reason": sizes.get("not_scaled_reason"),
        "route": retrieval_meta.get("retrieval_route"),
        "retrieval_calls": retrieval_meta.get("retrieval_calls", 0),
        "fallback_reason": retrieval_meta.get("retrieval_fallback_reason"),
        "errors": retrieval_meta.get("retrieval_errors") or {},
        "returned_references": shape.get("references_found"),
        "returned_chunks": shape.get("chunks_found"),
        "items_with_text": shape.get("items_with_text"),
        "kept": counts["kept"],
        "dropped_other_company": counts["other_company"],
        "unattributable": counts["unattributable"],
        "ambiguous": counts["ambiguous"],
        "blocks_kept": context_meta["blocks_kept"],
        "dropped_by_cap": context_meta["dropped_by_cap"],
        "context_chars": context_meta["context_chars"],
    }


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
            # Same sizing keys as the carried path, so a run's retrieval-sizing census covers the
            # failures too instead of silently dropping them from the totals.
            "retrieval_rounds": 1,
            "retrieval_calls": retrieval_meta.get("retrieval_calls", 0),
            "retrieval_floor": settings.company_scoped_retrieval_floor,
            "retrieval_ceiling": settings.company_scoped_retrieval_ceiling,
            "retrieval_multiplier": retrieval_meta["retrieval_sizes"]["multiplier"],
            "retrieval_top_k_requested": retrieval_meta["retrieval_sizes"]["top_k_requested"],
            "retrieval_chunk_top_k_requested": retrieval_meta["retrieval_sizes"]["chunk_top_k_requested"],
            "retrieval_not_scaled_reason": retrieval_meta["retrieval_sizes"]["not_scaled_reason"],
            "retrieval_widened": False,
            "retrieval_widening_kept": False,
            "retrieval_widen_skipped_reason": "retrieval_routes_failed",
            "references_returned": 0,
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

    # ── one wider round when the company filter left too little to answer from ──
    # The over-fetch multiplier is a static guess about how much the filter eats; this is the reactive
    # half, and it is what saves a row whose company simply sits low in the ranking. Bounded hard:
    # one extra round, only when the first round cost no generation (so a widened row is still at most
    # ONE generation), and only when a wider request would actually differ. The context cap is not
    # touched by any of this - a wider pool can add blocks to the prompt, never exceed the budget.
    floor = settings.company_scoped_retrieval_floor
    base_multiplier = max(1, settings.company_scoped_retrieval_multiplier)
    rounds = [_round_record(1, retrieval_meta, counts, context_meta)]
    widened, widening_kept, widen_skipped = False, False, None
    if floor > 0 and context_meta["blocks_kept"] < floor:
        if base_multiplier < 2:
            # Widening belongs to the over-fetch lever, not to every scoped question. With
            # COMPANY_SCOPED_RETRIEVAL_MULTIPLIER=1 (the shipped default) the pass must behave exactly
            # as it did before this row existed, one retrieval and one generation per row; a second
            # retrieval round on every thin row would change that whether or not it helps.
            widen_skipped = "over_fetch_multiplier_1"
        elif retrieval_meta.get("retrieval_model_calls"):
            # The first round already paid for a generation on the `/query` fallback route. A second
            # round can pay for another, and a row costing two generations is not the row the
            # operator agreed to buy. Better a thin context than an unbudgeted second answer.
            widen_skipped = "first_round_paid_a_generation"
        else:
            wide_sizes = _scaled_sizes(payload_base, base_multiplier * 2)[0]
            if not wide_sizes:
                # The door sent no top_k/chunk_top_k to scale. Nothing is invented here: the pass
                # over-fetches what the caller asked for, it does not re-decide the caller's sizing.
                widen_skipped = "no_sizes_in_payload_base"
            elif wide_sizes == _scaled_sizes(payload_base, base_multiplier)[0]:
                widen_skipped = "at_retrieval_ceiling"
            else:
                widened = True
                wide_refs, wide_meta = await retrieve_context(
                    question, payload_base, client, allowed_companies,
                    multiplier=base_multiplier * 2, allow_query_fallback=False)
                wide_owners = await document_identity.ownership_for_paths(
                    sorted({path_of(reference) for reference in wide_refs} - {""}))
                wide_kept, wide_counts = classify_references(wide_refs, wide_owners,
                                                             allowed_companies)
                wide_context, wide_context_meta = build_scoped_context(
                    wide_kept, settings.company_scoped_context_max_chars)
                rounds.append(_round_record(2, wide_meta, wide_counts, wide_context_meta))
                # Take the wider round only if it is strictly better on what the model actually gets
                # to read, then on attributable references. On a tie the first round stands: a second
                # retrieval that changed nothing must not silently change which evidence answers.
                if ((wide_context_meta["blocks_kept"], wide_counts["kept"])
                        > (context_meta["blocks_kept"], counts["kept"])):
                    references, kept, counts = wide_refs, wide_kept, wide_counts
                    context, context_meta = wide_context, wide_context_meta
                    retrieval_meta = wide_meta
                    widening_kept = True
    carried = rounds[-1] if widening_kept else rounds[0]

    meta = {
        # Retrieval sizing, per round. `retrieval_widened` says a second round ran (whether or not it
        # won); `retrieval_widen_skipped_reason` says why a thin kept set did NOT get one. A run that
        # cannot tell these apart cannot tell an over-fetch that worked from one that was refused.
        "retrieval_rounds": len(rounds),
        "retrieval_calls": sum(round_["retrieval_calls"] for round_ in rounds),
        "retrieval_floor": floor,
        "retrieval_ceiling": settings.company_scoped_retrieval_ceiling,
        "retrieval_multiplier": carried["multiplier"],
        "retrieval_top_k_requested": carried["top_k_requested"],
        "retrieval_chunk_top_k_requested": carried["chunk_top_k_requested"],
        "retrieval_not_scaled_reason": carried["not_scaled_reason"],
        "retrieval_widened": widened,
        "retrieval_widening_kept": widening_kept,
        "retrieval_widen_skipped_reason": widen_skipped,
        "retrieval_rounds_detail": rounds,
        "companies": list(allowed_companies),
        "references_total": counts["total"],
        "references_kept": counts["kept"],
        "references_dropped_other_company": counts["other_company"],
        "references_unattributable": counts["unattributable"],
        "references_ambiguous": counts["ambiguous"],
        "references_dropped_by_context_cap": context_meta["dropped_by_cap"],
        "references_returned": carried["returned_references"],
        "references_without_content": context_meta["without_content"],
        "context_chars": context_meta["context_chars"],
        "context_max_chars": settings.company_scoped_context_max_chars,
        # The cap decision is auditable now: how many blocks the model actually saw, whether one was
        # cut, how much budget was left unused, and the index at which the ranked list stopped being
        # kept. Without `first_dropped_index` a binding cap looks like a retrieval problem.
        "context_blocks_kept": context_meta["blocks_kept"],
        "context_truncated_blocks": context_meta["truncated_blocks"],
        "context_unused_chars": context_meta["unused_chars"],
        "context_first_dropped_index": context_meta["first_dropped_index"],
        # `second_model_call` now means what its name says: a SECOND generation ran, which happens only
        # on the `/query` fallback route, where retrieval itself cost an answer that is thrown away. The
        # scoped generation is reported by `generated` and counted by `model_calls`; on the `/query/data`
        # route it is the pass's ONLY generation, and saying `second_model_call: true` there made every
        # scoped row look like it had paid twice (Hermes: true on 70/70 rows while model_calls was 1).
        "second_model_call": bool(retrieval_meta.get("retrieval_model_calls")),
        "generated": False,
        "hard_refusal_verdict": None,
        # A rejected or failed GENERATION call (status and class only): the difference between "the scoped
        # pass decided to decline" and "the scoped pass could not run".
        "generation_error": None,
        "fallback_error": None,
        "history_messages": 0,
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

    if settings.company_scoped_hard_refusal:
        # The deterministic half of the refusal fix: when the question names an identifier and that
        # identifier is nowhere in the material we chose to send, the gateway refuses instead of asking
        # the model to behave. Off by default - the M1 measurement of how often this classifier is wrong
        # was never run over a dev set, so this is a knob for Hermes to measure, not a shipped promise.
        verdict = hard_refusal_verdict(question, context)
        meta["hard_refusal_verdict"] = verdict
        if verdict:
            meta["declined_reason"] = ("asked_identifier_absent_from_scoped_context_after_context_cap"
                                       if context_meta["dropped_by_cap"] else
                                       "asked_identifier_absent_from_scoped_context")
            return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=kept,
                              meta=meta, used=True)

    meta["generated"] = False
    if retrieval_meta.get("retrieval_model_calls"):
        meta["second_model_call"] = True
    meta["model_calls"] = retrieval_meta.get("retrieval_model_calls", 0) + 1
    history_text, history_messages = history_block(payload_base)
    meta["history_messages"] = history_messages
    generation_payload: dict[str, Any] = {
        "query": scoped_generation_prompt(question, allowed_companies, context, history_text),
        "mode": "bypass",
        "include_references": False,
    }
    # The same history the normal /query path would send, sent the same way (it was bounded at the door:
    # extract_ollama_history for the bridge, the caller's own list for /query). Sending it costs nothing
    # and is what upstream would honour; the prompt block above is what makes the instruction work even if
    # bypass ignores the field.
    if isinstance(payload_base.get("conversation_history"), list) and payload_base["conversation_history"]:
        generation_payload["conversation_history"] = payload_base["conversation_history"]
    try:
        generation = await client.post_json("/query", generation_payload)
    except Exception as exc:
        # The scoped pass failing to generate must not become the user's 500. Degrade the way the
        # retrieval legs do: used=False sends the caller back to its normal path with citation filtering,
        # and the log carries the class and status. No other-company text comes from THIS pass; the caller
        # decides what its own path produced, and it is already the shipped `citations` behaviour.
        detail = error_detail(exc)
        meta["generation_error"] = detail
        status = detail.get("status")
        meta["declined_reason"] = (f"generation_failed_{status}" if status
                                   else f"generation_failed:{detail['type']}")
        meta["model_calls"] = retrieval_meta.get("retrieval_model_calls", 0)
        return ScopedPass(answer="", citations=[], used=False, meta=meta)
    meta["generated"] = True
    answer = str(generation.get("response", "")).strip()
    if not answer:
        # A blank regeneration must not become a silent pass-through of the unscoped draft: the
        # caller sees used=True with the honest message and the citations that do belong here.
        meta["declined_reason"] = "regeneration_returned_no_text"
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=kept,
                          meta=meta, used=True)
    return ScopedPass(answer=answer, citations=kept, meta=meta, used=True)
