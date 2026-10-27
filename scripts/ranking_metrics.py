#!/usr/bin/env python3
"""scripts/ranking_metrics.py — graded retrieval quality, as pure arithmetic.

Why this exists
---------------
``evidence_in_context`` in ``scripts/overnight_loop.py`` answers one binary question:
is any gold quote anywhere in what the stack returned. The recall comparisons
(production 85, stack2 91, legacy+BGE-M3 83) could therefore never say whether the
evidence sat near the TOP of the returned context, whether a multi-fact probe got
all of its facts or one of three, or whether the best evidence outranks merely
supporting evidence. This module adds those measurements and nothing else: no
I/O, no LLM, no network, no file access, no dependency on this repo.

The unit is the returned STRING, not a page
-------------------------------------------
The reference instrument this is modelled on (a RAG re-ranker comparison) grades
relevant PAGES, because its corpus has page numbers. Ours does not, so the unit
here is one string the response carries, and a gold quote is credited in the unit
that contains it.

Rank is "position in the response walk" — UNVERIFIED ORDER
---------------------------------------------------------
``context_units`` walks the JSON response depth-first and keeps every string that
is not a request echo, exactly like ``overnight_loop.context_strings``. That walk
is order-preserving with respect to the payload, but whether LightRAG lists chunks
in ranked order is NOT established in this repo: the ``only_need_context``
envelope is undocumented here. So every rank-based number (first_hit_rank, mrr,
hit@k, ndcg@k) means "position in the response walk" and is reported under the
marker ``unverified-order`` until a human confirms the order against a real
response. Recall-style numbers (evidence_recall, any_hit, all_hit) do not depend
on the order at all.

Matching is per unit, not per join
----------------------------------
``evidence_in_context`` normalises ONE joined haystack, so a gold quote that
happens to span two returned strings still counts as a hit there. Here a quote is
credited inside a single unit, so a cross-unit span is a miss. That is the
stricter, honest reading of "which returned chunk contains the answer", and it is
why ``any_hit_pct`` from this module can sit below ``evidence_in_context_pct``.
The binary success gate stays on the joined check; both numbers are reported so
the gap is visible rather than silent.

Each relevant quote counts ONCE
-------------------------------
A quote appearing in several units is credited at its FIRST appearance; later
appearances add nothing. Grades are 2 (contains the answer), 1 (partly relevant),
0 (not relevant); an ungraded probe is all 2, which is what the binary check
already assumed.

What the Hermes helpers would call
----------------------------------
``probe/run_stack2_recall.py`` and ``probe/run_prod_recall.py`` (Thinkpad, outside
this repo) already hold their own gold quotes and their own returned strings, so
they need only::

    from ranking_metrics import context_units, score_probe, mean_metric

    units = context_units(payload)                    # or their own ordered list
    row = score_probe(gold=[...], grades=[...], units=units)   # None when no gold
    arm = mean_metric([r["mrr"] for r in rows if r])   # None when nothing measured

``score_probe`` never raises on an ungraded or gold-less probe: gold-less probes
return ``None``, which every aggregation skips (na, never a miss).
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from typing import Any

__all__ = [
    "ECHO_KEYS",
    "RANK_ORDER_MARKER",
    "GRADE_ANSWER",
    "GRADE_PARTIAL",
    "GRADE_NONE",
    "MEASURED_METRICS",
    "context_units",
    "normalise_quote",
    "credit_quotes",
    "recall_fraction",
    "graded_recall_fraction",
    "first_hit_rank",
    "relevance_by_unit",
    "reciprocal_rank",
    "average_precision",
    "ndcg_at_k",
    "hit_at_k",
    "score_probe",
    "mean_metric",
]

# Grading scale borrowed from the reference instrument: 2 = contains the answer,
# 1 = partly relevant, 0 = not relevant. An ungraded quote means 2, because that
# is what "this quote would prove the answer" already asserted.
GRADE_ANSWER = 2
GRADE_PARTIAL = 1
GRADE_NONE = 0

# Reported next to every rank-based number until a human confirms that LightRAG
# lists chunks in the order it retrieved them.
RANK_ORDER_MARKER = "unverified-order"

# Keys whose values are the request echoed back, not retrieved material: matching
# them would score a hit for the question itself. Kept here so the extractor and
# the binary check cannot drift apart; overnight_loop re-exports this same object.
ECHO_KEYS = frozenset({
    "query", "question", "mode", "response_type", "model", "status", "message",
    "only_need_context", "only_need_prompt", "stream", "include_references",
    "include_chunk_content",
})

# The per-probe measurements an arm averages. Order matters only for reporting.
MEASURED_METRICS = (
    "evidence_recall", "graded_recall", "mrr", "average_precision",
    "hit@1", "hit@5", "ndcg@5", "first_hit_rank",
)


def normalise_quote(text: Any) -> str:
    """Collapse whitespace, lower-case, strip surrounding quote marks.

    This is THE normalisation the whole instrument shares: ``overnight_loop`` and
    ``chunk_embed_bench`` both score gold quotes with this exact function, so a
    "hit" means the same thing in every report. It lives here so this module can
    be imported on its own by the Thinkpad recall helpers.
    """
    normalised = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    return normalised.strip('"\u2018\u2019\u201c\u201d\'` ')


def context_units(payload: Any, *, skip: str = "", echo_keys: frozenset[str] = ECHO_KEYS) -> list[str]:
    """Every string the response carries, IN THE ORDER the payload carries them.

    Depth-first, dict insertion order first, exactly like ``context_strings``, and
    with the same request-echo filter. Each returned string is one ranked unit.

    Order-preserving is not the same as ranked: this returns what the envelope
    holds in the order the envelope holds it. Whether that is retrieval order is
    unverified (see the module docstring), which is why rank metrics carry
    ``RANK_ORDER_MARKER``.
    """
    found: list[str] = []

    def walk(node: Any, key: str | None) -> None:
        if isinstance(node, str):
            if key in echo_keys:
                return
            if node and node != skip:
                found.append(node)
        elif isinstance(node, list):
            for entry in node:
                walk(entry, key)
        elif isinstance(node, dict):
            for name, value in node.items():
                walk(value, str(name))

    walk(payload, None)
    return found


def _gold_pairs(gold: Sequence[str], grades: Sequence[int] | None) -> dict[str, int]:
    """Ordered {normalised quote: grade}. Duplicate quotes collapse to first seen."""
    pairs: dict[str, int] = {}
    for index, quote in enumerate(gold):
        needle = normalise_quote(quote)
        if not needle:
            continue
        grade = _clean_grade(grades[index] if grades is not None and index < len(grades) else None)
        pairs.setdefault(needle, grade)
    return pairs


def _clean_grade(value: Any) -> int:
    """0/1/2 as given; anything else (missing, bool, out of range) means 2.

    Silent on purpose for the probe files: an ungradeable value must not crash an
    overnight run, and 2 is what an ungraded probe already meant. A probe author
    who writes a bad grade gets the old behaviour, not a new number.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return GRADE_ANSWER
    return value if GRADE_NONE <= value <= GRADE_ANSWER else GRADE_ANSWER


def credit_quotes(
    units: Sequence[str],
    gold: Sequence[str],
    grades: Sequence[int] | None = None,
) -> tuple[list[int], list[int]]:
    """``(grade_per_unit, grades_actually_credited)``, both in probe terms.

    A quote is credited in the FIRST unit that contains it and never counted again,
    so a quote repeated across three units raises one unit, not three. A unit that
    happens to carry two gold quotes is ONE relevant position graded by its best
    quote — the unit is the ranked thing here — while the second quote still counts
    in the recall denominators, which is why the two lists are returned separately.
    """
    pending = _gold_pairs(gold, grades)
    relevance: list[int] = []
    credited: list[int] = []
    for unit in units:
        text = normalise_quote(unit)
        hits = [quote for quote in tuple(pending) if quote in text]
        if hits:
            awarded = [pending.pop(quote) for quote in hits]
            credited.extend(awarded)
            relevance.append(max(awarded))
        else:
            relevance.append(GRADE_NONE)
    return relevance, credited


def relevance_by_unit(
    units: Sequence[str],
    gold: Sequence[str],
    grades: Sequence[int] | None = None,
) -> list[int]:
    """Grade of each unit in order: 0 when it contains no still-uncounted gold quote."""
    return credit_quotes(units, gold, grades)[0]


def first_hit_rank(
    units: Sequence[str],
    gold: Sequence[str],
    grades: Sequence[int] | None = None,
) -> int | None:
    """1-based position of the first unit carrying any gold quote, None if absent."""
    for position, grade in enumerate(relevance_by_unit(units, gold, grades), start=1):
        if grade > GRADE_NONE:
            return position
    return None


def reciprocal_rank(first_rank: int | None) -> float:
    """1/rank, or 0.0 when the evidence never appeared."""
    return 1.0 / first_rank if first_rank else 0.0


def average_precision(relevance: Sequence[int]) -> float:
    """Mean of precision@k taken at each relevant position. 0.0 when nothing is relevant."""
    relevant = sum(1 for grade in relevance if grade > GRADE_NONE)
    if not relevant:
        return 0.0
    total = 0.0
    hits = 0
    for position, grade in enumerate(relevance, start=1):
        if grade > GRADE_NONE:
            hits += 1
            total += hits / position
    return total / relevant


def _discount(position: int) -> float:
    """The log2 discount at a 1-based position: position 1 costs nothing."""
    return math.log2(position + 1)


def _gain(grade: int) -> float:
    """(2^grade - 1): grade 2 is worth three of grade 0/1, grade 1 worth one."""
    return float(2 ** max(0, int(grade)) - 1)


def ndcg_at_k(relevance: Sequence[int], k: int = 5) -> float:
    """Normalised discounted cumulative gain over the first k units.

    ``gain = (2^grade - 1)`` divided by ``log2(position + 1)``, over the ideal
    ordering of the SAME grades. So 1.0 means the good evidence is as high in the
    response as it could have been, and a probe with no relevant unit scores 0.0.
    """
    gains = [_gain(grade) for grade in relevance]
    if not gains or k <= 0:
        return 0.0
    window = min(int(k), len(gains))
    dcg = sum(gains[index] / _discount(index + 1) for index in range(window))
    ideal = sorted(gains, reverse=True)
    idcg = sum(ideal[index] / _discount(index + 1) for index in range(window))
    return dcg / idcg if idcg else 0.0


def hit_at_k(relevance: Sequence[int], k: int = 5) -> int:
    """1 when any relevant unit sits in the first k, else 0."""
    return 1 if any(grade > GRADE_NONE for grade in relevance[:max(0, int(k))]) else 0


def recall_fraction(gold_hits: Sequence[Any]) -> float | None:
    """Fraction of a probe's gold quotes that were found. None when it has no gold (na).

    ``gold_hits`` is one truthy/falsy value per gold quote, in the probe's own order.
    """
    total = len(gold_hits)
    if not total:
        return None
    return round(sum(1 for found in gold_hits if found) / total, 4)


def graded_recall_fraction(graded_hits: Sequence[tuple[int, Any]]) -> float | None:
    """Weighted version: found grade mass over total grade mass. None when unmeasurable.

    A quote graded 0 declares itself irrelevant, so it is in neither the numerator
    nor the denominator; a probe whose quotes are all graded 0 has no graded
    denominator and returns None rather than a flattering 0.0.
    """
    total = sum(_clean_grade(grade) for grade, _found in graded_hits)
    if not total:
        return None
    found = sum(_clean_grade(grade) for grade, was_found in graded_hits if was_found)
    return round(found / total, 4)


def score_probe(
    gold: Sequence[str],
    units: Sequence[str],
    grades: Sequence[int] | None = None,
    *,
    k: int = 5,
) -> dict[str, Any] | None:
    """Every measurement for one probe, or None when the probe carries no gold quote.

    ``gold`` is the probe's gold quotes (probe order matters: ``grades`` aligns with
    it), ``units`` is the returned context as ordered strings — see ``context_units``.
    The gold-less ``None`` is what keeps an unanswerable probe out of every mean:
    a probe nobody can answer is na, never a miss.
    """
    pairs = _gold_pairs(gold, grades)
    if not pairs:
        return None
    relevance, credited = credit_quotes(units, list(pairs), list(pairs.values()))
    gold_n = len(pairs)
    found_n = len(credited)
    total_mass = sum(pairs.values())
    rank = first_hit_rank(units, list(pairs), list(pairs.values()))
    return {
        "gold_n": gold_n,
        "found_n": found_n,
        "units": len(units),
        "any_hit": found_n > 0,
        "all_hit": found_n == gold_n,
        # Counted per QUOTE, not per unit: one unit can prove two facts.
        "evidence_recall": round(found_n / gold_n, 4),
        "graded_recall": round(sum(credited) / total_mass, 4) if total_mass else None,
        "first_hit_rank": rank,
        "mrr": reciprocal_rank(rank),
        "hit@1": hit_at_k(relevance, 1),
        "hit@5": hit_at_k(relevance, k),
        "ndcg@k": round(ndcg_at_k(relevance, k), 4),
        "average_precision": round(average_precision(relevance), 4),
        "k": int(k),
        "rank_order": RANK_ORDER_MARKER,
    }


def mean_metric(values: Sequence[Any], *, digits: int = 4) -> float | None:
    """Mean over the values that exist. None when none do — never a 0.0 for "not measured".

    A zero would be read as a measured failure; a probe whose stack never answered,
    or whose summary predates these columns, must not become one.
    """
    numbers = [float(value) for value in values if isinstance(value, (int, float)) and not isinstance(value, bool)]
    if not numbers:
        return None
    return round(sum(numbers) / len(numbers), digits)
