"""Topic-shift segmentation: cut flat text where the topic moves, not at a token count.

Why this module exists
----------------------
A transcript or a page of notes has no headings, no tables and no turns the chunker
can trust, so "cut at the natural boundary" has nothing to bite on. Cutting it every
N tokens produces pieces that straddle two subjects, which is what the retrieval
evaluation keeps hitting. TextTiling answers the question differently: embed adjacent
blocks of the text, look for the places where similarity between neighbours DROPS, and
cut there. Pieces come out unequal, one per topic, of whatever length the topics have.

Embedding is injected, never performed here
-------------------------------------------
This module makes no network calls and reads no settings. The caller hands in an
``embed(texts) -> vectors`` callable (the worker uses the served BGE-M3 through oMLX)
or ``None``. When it is missing, or raises, or the document is too short to have a
topic structure at all, the module falls back to a deterministic cut at unit
boundaries near the window, and SAYS SO in the report: a large piece produced by a
fallback is a different fact from a large piece produced by a topic, and the ingest
metadata has to be able to tell them apart.

Determinism
-----------
Same units + same embedder answers -> same groups. Nothing here uses a clock, a
random number, or a dictionary ordering that could drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

DEFAULT_UNITS_PER_WINDOW = 6
DEFAULT_DEPTH_THRESHOLD = 0.30
DEFAULT_GAP_SPAN = 2
DEFAULT_MAX_WINDOWS = 400
MIN_BLOCKS_FOR_TILING = 3


@dataclass(frozen=True)
class TopicReport:
    """What the segmentation actually did - counts and a method name, never text."""
    method: str
    blocks: int = 0
    units_per_block: int = 0
    boundaries: int = 0
    embedded: int = 0
    coarsened: bool = False
    splits_over_window: int = 0
    merges_under_floor: int = 0
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"method": self.method, "blocks": self.blocks,
                "units_per_block": self.units_per_block, "boundaries": self.boundaries,
                "embedded": self.embedded, "coarsened": self.coarsened,
                "splits_over_window": self.splits_over_window,
                "merges_under_floor": self.merges_under_floor,
                "notes": list(self.notes)}


def _cosine(left: Sequence[float], right: Sequence[float]) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    norm = math.sqrt(sum(a * a for a in left)) * math.sqrt(sum(b * b for b in right))
    if norm <= 0.0:
        return 0.0
    return dot / norm


def _depth_scores(sims: Sequence[float], span: int = DEFAULT_GAP_SPAN) -> list[float]:
    """TextTiling depth: how deep the valley is on each side of every gap.

    A gap between two similar neighbours with similar neighbours of its own is a
    shallow dip inside one topic. A gap that sits between two plateaus is deep, and
    that is where the topic changed.
    """
    scores: list[float] = []
    for gap, value in enumerate(sims):
        previous = sims[gap - 1] if gap > 0 else 1.0
        following = sims[gap + 1] if gap + 1 < len(sims) else 1.0
        if value > previous or value > following:
            # Not a valley: the drop is at another gap, and scoring this one too would
            # cut the same topic change three times.
            scores.append(0.0)
            continue
        left = sims[max(0, gap - span):gap]
        right = sims[gap + 1:min(len(sims), gap + span + 1)]
        peak_left = max(left) if left else previous
        peak_right = max(right) if right else following
        scores.append((peak_left - value) + (peak_right - value))
    return scores


def _greedy_groups(units: Sequence[str], tokens: Callable[[str], int], window: int,
                   _unused: Sequence[int] = ()) -> list[list[int]]:
    """Pack whole units into groups that fit ``window``. Units are never split."""
    groups: list[list[int]] = []
    current: list[int] = []
    cost = 0
    for unit in units:
        unit_cost = tokens(unit)
        if current and cost + unit_cost > window:
            groups.append(current)
            current, cost = [], 0
        current.append(unit)
        cost += unit_cost
    if current:
        groups.append(current)
    return groups


def group_units(
    units: Sequence[str],
    *,
    tokens: Callable[[str], int],
    window: int,
    floor: int = 0,
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]] | None = None,
    threshold: float = DEFAULT_DEPTH_THRESHOLD,
    units_per_block: int = DEFAULT_UNITS_PER_WINDOW,
    max_windows: int = DEFAULT_MAX_WINDOWS,
) -> tuple[list[list[str]], TopicReport]:
    """Split ``units`` into topic-coherent groups; each group is a list of units.

    ``window`` is the ceiling a group may not exceed (the embedder window), ``floor``
    the size below which a group is welded back onto its neighbour. The answer is a
    list of groups, never indices, so the caller cannot lose a unit on the way out.
    """
    notes: list[str] = []
    if not units:
        return [], TopicReport(method="no-units")
    if len(units) == 1:
        return [list(units)], TopicReport(method="single-unit", blocks=1)

    per_block = max(1, int(units_per_block))
    # Bounded work: more blocks than the cap coarsens the blocks, it never drops text.
    coarsened = False
    while -(-len(units) // per_block) > max_windows:
        per_block += 1
        coarsened = True
    blocks: list[list[int]] = []
    for start in range(0, len(units), per_block):
        blocks.append(list(range(start, min(len(units), start + per_block))))

    if embed is None:
        groups = _greedy_groups(units, tokens, window, ())
        return groups, TopicReport(method="fallback(no-embedder)", blocks=len(blocks),
                                   units_per_block=per_block, boundaries=max(0, len(groups) - 1),
                                   coarsened=coarsened,
                                   notes=("cut at unit boundaries near the window",))

    texts = ["\n".join(units[index] for index in block) for block in blocks]
    try:
        vectors = list(embed(texts))
    except Exception as exc:  # an unreachable model server changes HOW we cut, not WHETHER
        groups = _greedy_groups(units, tokens, window, ())
        return groups, TopicReport(method="fallback(embedder-unreachable)",
                                   blocks=len(blocks), units_per_block=per_block,
                                   boundaries=max(0, len(groups) - 1), coarsened=coarsened,
                                   notes=(f"{type(exc).__name__}: {str(exc)[:120]}",))
    if len(vectors) != len(texts) or any(len(vector) != len(vectors[0])
                                         for vector in vectors) or not vectors[0]:
        groups = _greedy_groups(units, tokens, window, ())
        return groups, TopicReport(method="fallback(bad-embedder-answer)",
                                   blocks=len(blocks), units_per_block=per_block,
                                   boundaries=max(0, len(groups) - 1), coarsened=coarsened,
                                   notes=(f"{len(vectors)} vectors for {len(texts)} blocks",))

    if len(blocks) < MIN_BLOCKS_FOR_TILING:
        groups = _greedy_groups(units, tokens, window, ())
        return groups, TopicReport(method="fallback(too-few-blocks)", blocks=len(blocks),
                                   units_per_block=per_block,
                                   boundaries=max(0, len(groups) - 1), coarsened=coarsened)

    sims = [_cosine(vectors[index], vectors[index + 1]) for index in range(len(vectors) - 1)]
    depths = _depth_scores(sims)
    cut_blocks: set[int] = set()
    order = sorted(range(len(sims)), key=lambda gap: (-depths[gap], gap))
    for gap in order:
        if depths[gap] < float(threshold):
            break
        # One valley can span two adjacent gaps (the block either side of the change both
        # straddle it). Two cuts there would cut the same change twice.
        if any(abs(gap - chosen) < 2 for chosen in cut_blocks):
            continue
        if not (0 < gap + 1 < len(blocks)):
            continue
        cut_blocks.add(gap)
    if not cut_blocks:
        groups = _greedy_groups(units, tokens, window, ())
        return groups, TopicReport(method="embedder(no-shift-found)", blocks=len(blocks),
                                   units_per_block=per_block, embedded=len(texts),
                                   coarsened=coarsened,
                                   notes=(f"deepest drop {max(depths):.3f} under threshold "
                                          f"{float(threshold):.2f}",))

    # Block edges are unit edges: a boundary never lands inside a unit.
    boundaries = sorted(block_start for block_start in
                        (blocks[gap + 1][0] for gap in cut_blocks))
    segments: list[list[int]] = []
    start = 0
    for boundary in boundaries:
        segments.append(list(range(start, boundary)))
        start = boundary
    segments.append(list(range(start, len(units))))

    # A segment bigger than the window is split at unit boundaries, not at a topic.
    splits = 0
    final: list[list[int]] = []
    for segment in segments:
        if sum(tokens(units[index]) for index in segment) <= window:
            final.append(segment)
            continue
        chunk: list[int] = []
        cost = 0
        for index in segment:
            unit_cost = tokens(units[index])
            if chunk and cost + unit_cost > window:
                final.append(chunk)
                chunk, cost = [], 0
                splits += 1
            chunk.append(index)
            cost += unit_cost
        if chunk:
            final.append(chunk)

    # A segment under the floor goes back to whichever neighbour it was closest to.
    merges = 0
    merged: list[list[int]] = []
    for segment in final:
        cost = sum(tokens(units[index]) for index in segment)
        if merged and cost < floor and cost < window:
            previous = sum(tokens(units[index]) for index in merged[-1])
            if previous + cost <= window:
                merged[-1] = merged[-1] + segment
                merges += 1
                continue
        merged.append(segment)
    # A leading fragment (the first topic was tiny) joins forward.
    while len(merged) > 1 and sum(tokens(units[index]) for index in merged[0]) < floor:
        if sum(tokens(units[index]) for index in merged[0]) + sum(
                tokens(units[index]) for index in merged[1]) <= window:
            merged[1] = merged[0] + merged[1]
            merged.pop(0)
            merges += 1
        else:
            break

    groups = [[units[index] for index in segment] for segment in merged]
    return groups, TopicReport(
        method="embedder", blocks=len(blocks), units_per_block=per_block,
        boundaries=len(boundaries), embedded=len(texts), coarsened=coarsened,
        splits_over_window=splits, merges_under_floor=merges,
        notes=(f"threshold {float(threshold):.2f}",) + tuple(notes))


def segment_bodies(
    units: Sequence[str],
    tokens: Callable[[str], int],
    window: int,
    floor: int = 0,
    join: str = "\n",
    **kwargs: Any,
) -> tuple[list[str], TopicReport]:
    """Convenience wrapper: units in, joined piece bodies out."""
    groups, report = group_units(units, tokens=tokens, window=window, floor=floor, **kwargs)
    return [join.join(group) for group in groups], report
