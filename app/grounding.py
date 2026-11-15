"""Asked-identifier grounding: does the evidence actually contain the identifier the
question asked about?

M1 in the GL-08 investigation. This module is deliberately small and side-effect free: it
classifies, it never refuses, and nothing in it changes an answer, a citation list or a
status code. The gateway calls it only when ``GROUNDING_CHECK_ASKED_IDENTIFIER`` is on, and
its output goes into ``query_scope["grounding"]`` and the query log.

Why an identifier rule rather than a similarity score
----------------------------------------------------
The GL-08 synthetic baseline hallucinated on 8 of 12 unanswerable probes: the answer
asserted an outcome for a fact code that exists in no document. Retrieval is not the
culprit - it always returns *something*, and in a store where every identifier shares a
company prefix (712 lookalikes per probe in the invented GL-07 corpus) the near misses
arrive with respectable scores. Measured on that corpus, the similarity-score distributions
of answerable and unanswerable probes OVERLAP, so no threshold separates them. The
identifier does: the asked code reached the top of retrieval in 0 of 12 unanswerable probes.

The three traps this module encodes
-----------------------------------
1. **Token match, never substring.** A company code (``NC-3617``) is a substring of every
   planted code in the store (``NC-3617-014``). ``code in evidence`` therefore reports
   support for evidence that does not contain the code.
2. **Sanitise the question, not just the evidence.** LightRAG deletes C0 control characters
   from stored text (a PDF page break leaves ``\\x0c`` behind), so the evidence is clean while
   a question pasted from a PDF is not. Comparing raw question text to clean evidence
   false-refuses an answerable question.
3. **Evidence means the primary pass's references, before company scoping and before the
   ``CITATION_TOP_K`` truncation.** The caller's citation list is a view; the model saw
   everything the retrieval pass returned.

4. **Evidence means the fields the rest of the gateway reads.** This module once looked only at a
   string-valued ``content``, while ``app/rerank.py``, ``app/graphrag.py`` and
   ``app/generation.py`` all accept ``content``/``text``/``chunk_text``/``body`` and accept a list of
   chunk strings. A guard narrower than the readers around it refuses questions the system
   demonstrably had the text to answer.

Scope, stated honestly: a question that names no identifier produces ``asked == []`` and
carries no information either way. Scoped and cross-company probes are shaped like that,
which is why this rule cannot cost them recall - but it also means it says nothing about
answers that invent an identifier the caller never asked for. That is the M2 variant, and it
is not implemented here.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

from .citation_text import CITATION_TEXT_KEYS, citation_content, content_shape_counts

# An identifier is an uppercase, hyphen-segmented token that contains a digit:
# NC-3617-014 (planted fact), NC-3617-NA-01 (the unanswerable shape), CP-2507-OB-001.
# Requiring a digit keeps ALL-CAPS prose out; requiring a hyphen keeps "PDF" out.
IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+\b")


def identifiers(text: str) -> list[str]:
    """Identifier-shaped tokens in order of first appearance, de-duplicated."""
    seen: dict[str, None] = {}
    for match in IDENTIFIER_RE.finditer(text or ""):
        token = match.group(0)
        if any(character.isdigit() for character in token):
            seen.setdefault(token, None)
    return list(seen)


def verbatim_support(code: str, evidence: str, *, match: str = "token") -> bool:
    """Is this identifier present in the evidence?

    ``match="token"`` (the default everywhere in app code) requires a standalone identifier
    token. ``match="substring"`` exists only so tests can show the hazard: it calls an
    identifier supported when it merely appears inside a longer one.
    """
    if match == "substring":
        return code in (evidence or "")
    return code in set(identifiers(evidence))


def evidence_texts(citations: Iterable[dict[str, Any]] | None,
                   keys: Sequence[str] = CITATION_TEXT_KEYS) -> tuple[str, int]:
    """Join the evidence text the answer was written from, and count references that
    carried none.

    The key set and the list-valued shape are the ones ``app/rerank.py`` and
    ``app/graphrag.py`` have always accepted, shared through ``app/citation_text.py``. Before
    that, this reader looked only at a string-valued ``content``: the same response that let the
    reranker score a chunk and the prompt cite it could leave the gate with an empty evidence
    string and a ``no_evidence`` verdict. Reading fewer fields than the callers is how a guard
    becomes a coin flip.

    The second number matters: the gateway asks LightRAG for chunk content
    (``include_chunk_content: True``), and if a real response ever omits it, a support
    verdict computed from empty text would be wrong in the loud direction. Counting it
    makes that visible in the log instead of silently becoming "unsupported".
    """
    parts: list[str] = []
    missing = 0
    for citation in citations or []:
        text = citation_content(citation, keys)
        if text:
            parts.append(text)
        else:
            missing += 1
    return "\n".join(parts), missing


def support_gate(question: str, answer: str, evidence: str, *,
                 sanitise_question: bool = True) -> dict[str, Any]:
    """Classify one (question, answer, evidence) triple.

    Returns the classification only. Nothing here raises, refuses or rewrites; the caller
    decides what to do, and today the gateway does nothing but record it.
    """
    from .lightrag_text import sanitize_like_lightrag

    question_text = sanitize_like_lightrag(question or "") if sanitise_question else (question or "")
    asked = identifiers(question_text)
    asserted = identifiers(answer or "")
    in_evidence = {code for code in set(asked) | set(asserted) if verbatim_support(code, evidence)}
    asked_unsupported = [code for code in asked if code not in in_evidence]
    asserted_unsupported = [code for code in asserted if code not in in_evidence]

    # Precedence is diagnostic. Empty evidence is its own failure. Otherwise, when the
    # QUESTION names an identifier and that identifier is nowhere in the evidence, that is
    # the unanswerable shape, and it is named even though the same code is usually also
    # asserted in the answer.
    if not (evidence or "").strip():
        verdict = "no_evidence"
    elif asked and asked_unsupported:
        verdict = "no_support_for_asked_identifier"
    elif asserted_unsupported:
        verdict = "unsupported_identifiers_in_answer"
    elif not asked:
        verdict = "no_identifier_in_question"
    else:
        verdict = "supported"

    return {
        "verdict": verdict,
        "asked": asked,
        "asserted": asserted,
        "unsupported": asked_unsupported or asserted_unsupported,
        "asked_unsupported": asked_unsupported,
        "asserted_unsupported": asserted_unsupported,
        # Tri-state on purpose: None means "there was no identifier to check", which is NOT
        # a pass. A question that names nothing carries no grounding information at all.
        "supported": (not (asked_unsupported or asserted_unsupported)
                      if (asked or asserted) else None),
    }


def asked_identifier_grounding(question: str, answer: str,
                               citations: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    """The ``query_scope["grounding"]`` payload for one query.

    Carries identifiers (which came from the caller's own question) and counts, never
    reference text, so the query log stays publishable and small.
    """
    reference_list = list(citations or [])
    evidence, missing_content = evidence_texts(reference_list)
    gate = support_gate(question, answer, evidence)
    gate["evidence_reference_count"] = len(reference_list)
    gate["evidence_references_without_content"] = missing_content
    # Shape census, counts only. A run can be read without a second probe run: if every row
    # says `absent`, the evidence was never in the response and no rule change can help; if it
    # says `list`, the old string-only reader was the whole story. Shapes are "string", "list",
    # "empty" and "absent" (see app/citation_text.py).
    gate["references_with_content"] = sum(
        1 for citation in reference_list if citation_content(citation))
    gate["references_content_shapes"] = content_shape_counts(reference_list)
    return gate
