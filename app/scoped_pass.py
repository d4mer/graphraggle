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
from typing import Any

from .citation_text import citation_content
from .config import settings
from .state_store import get_documents_by_paths


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


def classify_references(
    references: list[dict[str, Any]],
    rows_by_path: dict[str, list[dict[str, Any]]],
    allowed_companies: list[str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Split references into the named companies' and the rest.

    A path with rows for several companies (the shared-file case: one company ingested it, another
    has a `skipped` duplicate row) counts as allowed if any of its rows belongs to a named company,
    because that company does own the document. A path with no row, or whose rows carry no company,
    is `unattributable` and goes: the gateway cannot say it belongs to the company asked about.
    """
    allowed = set(allowed_companies)
    kept: list[dict[str, Any]] = []
    counts = {"total": len(references), "kept": 0, "other_company": 0, "unattributable": 0}
    for reference in references:
        path = reference.get("file_path") or reference.get("path") or ""
        rows = rows_by_path.get(path) or []
        companies = {row.get("company") for row in rows if row.get("company")}
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

    payload = {**payload_base, "include_references": True, "include_chunk_content": True}
    retrieval = await client.post_json("/query", payload)
    references = retrieval.get("references") or []

    paths = sorted({str(ref.get("file_path") or ref.get("path") or "") for ref in references} - {""})
    rows = await get_documents_by_paths(paths) if paths else []
    rows_by_path: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        rows_by_path.setdefault(str(row.get("path") or ""), []).append(row)

    kept, counts = classify_references(references, rows_by_path, allowed_companies)
    context, context_meta = build_scoped_context(kept, settings.company_scoped_context_max_chars)

    meta = {
        "companies": list(allowed_companies),
        "references_total": counts["total"],
        "references_kept": counts["kept"],
        "references_dropped_other_company": counts["other_company"],
        "references_unattributable": counts["unattributable"],
        "references_dropped_by_context_cap": context_meta["dropped_by_cap"],
        "references_without_content": context_meta["without_content"],
        "context_chars": context_meta["context_chars"],
        "context_max_chars": settings.company_scoped_context_max_chars,
        "second_model_call": False,
        "declined_reason": None,
    }

    if not kept:
        meta["declined_reason"] = ("no_reference_joins_a_named_company"
                                   if counts["total"] else "no_references_returned")
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=[],
                          meta=meta, used=True)
    if not context:
        # Documents for that company were retrieved but carried no chunk text, so there is nothing
        # honest to answer from. Showing the documents is still correct; guessing is not.
        meta["declined_reason"] = "kept_references_carry_no_chunk_text"
        return ScopedPass(answer=no_scoped_material_message(allowed_companies), citations=kept,
                          meta=meta, used=True)

    meta["second_model_call"] = True
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
