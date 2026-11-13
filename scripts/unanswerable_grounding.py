#!/usr/bin/env python3
"""scripts/unanswerable_grounding.py — investigation harness for unanswerable-probe
hallucination. TEST-ONLY: it measures, it does not change gateway behaviour, and it
is not wired into the worker, the gateway, or any deploy script.

Why this exists
---------------
The GL-08 synthetic baseline refused 4 of 12 unanswerable probes and hallucinated 8:
the answer asserted an outcome for a fact code that exists in no document. The probes
are adversarial by construction -- the generator writes unanswerable codes as
``<CODE>-NA-<NN>`` next to real planted codes like ``<CODE>-3617``, so the retrieval
pool is full of lookalikes.

This harness answers three things offline, with no model, no server and no real
corpus:

1. Where could a support check live in the gateway, and what text would it see?
   ``support_gate`` is the candidate rule, as a pure function, so it can be scored on
   any (question, answer, evidence) triple -- including a live run on stack2.
2. Is the rule keying on something that survives our own pipeline? A gate is worthless
   if the identifier it demands is mangled by the sanitiser or split by a chunk cut.
   ``identifier_survival`` measures exactly that against ``app.lightrag_text`` and
   ``app.structure_chunker``.
3. What would the rule cost in recall? For every planted fact code, is it present
   verbatim in a chunk of the document that holds it? Anything missing there is a
   false refusal waiting to happen, independent of retrieval quality.

Corpus scope: only INVENTED corpora produced by scripts/make_synthetic_company.py.
Text documents are read directly; DOCX/PPTX text is recovered by unzipping and
stripping tags, PDF text by reading the content streams. Those extractions are crude
on purpose -- they are used only to ask "does this identifier appear verbatim", never
to quote content, and they are reported as the method used.

Usage:
    python3 scripts/unanswerable_grounding.py <corpus-dir> [<corpus-dir> ...]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import zipfile
from collections import Counter
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Sequence

repo_root = Path(__file__).resolve().parents[1]
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

from app import grounding, lightrag_text, structure_chunker  # noqa: E402

# ── identifiers ────────────────────────────────────────────────────────────
# One implementation, in app/grounding.py, because the gateway now applies the same
# rule the harness measures. A second copy here would let the harness certify a rule
# the gateway no longer runs.
identifiers = grounding.identifiers
verbatim_support = grounding.verbatim_support


def near_misses(code: str, pool: Iterable[str], *, company_code: str | None = None) -> dict[str, list[str]]:
    """Lookalikes in the retrieval pool, graded by how similar they are.

    This is the bridging fuel. The generator writes unanswerable codes as
    ``<company_code>-NA-<NN>`` while planted facts are ``<company_code>-<NNN>``, so
    every planted code in the store is a near miss for every unanswerable probe.

    ``same_company_code`` is the dangerous tier: it shares the whole company prefix.
    ``shares_a_subtoken`` is the hazard for a sloppy gate: the asked identifier occurs
    inside a longer identifier, so a substring test would call it supported.
    """
    pool = list(pool)
    same_first_segment = sorted(c for c in pool if c != code and c.split("-", 1)[0] == code.split("-", 1)[0])
    same_company_code = sorted(
        c for c in pool if c != code and company_code is not None and c.startswith(f"{company_code}-")
    )
    substring_hazard = sorted(c for c in pool if c != code and code in c)
    return {
        "same_first_segment": same_first_segment,
        "same_company_code": same_company_code,
        "shares_a_subtoken": substring_hazard,
    }


# ── the mitigation the gateway now runs, driven with a list of chunks ───────

def support_gate(question: str, answer: str, evidence: Sequence[str]) -> dict[str, Any]:
    """``app.grounding.support_gate`` over a list of evidence chunks instead of one string.

    Same rule, same numbers. On the gateway the evidence is the primary pass's
    ``references`` with ``include_chunk_content: True`` - before company scoping and before
    the ``CITATION_TOP_K`` truncation, because a chunk dropped from the returned list was
    still in front of the model.
    """
    joined = "\n".join(evidence)
    result = grounding.support_gate(question, answer, joined)
    result["evidence_identifier_count"] = len(identifiers(joined))
    return result


# ── invented-corpus text recovery (deliberately crude; see module docstring) ──

def plain_text_of(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".html", ".htm", ".tex", ".rst", ".json"}:
        return path.read_text(encoding="utf-8", errors="replace")
    if suffix in {".docx", ".pptx"}:
        parts: list[str] = []
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.endswith(".xml"):
                    xml = archive.read(name).decode("utf-8", errors="replace")
                    parts.append(re.sub(r"<[^>]+>", " ", xml))
        return "\n".join(parts)
    if suffix == ".pdf":
        raw = path.read_bytes()
        chunks = [block.decode("latin-1", errors="replace")
                  for block in re.findall(rb"\((.*?)\) Tj", raw, flags=re.S)]
        return "\n".join(chunks)
    return ""


def corpus_documents(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "manifest.json":
            continue
        out[str(path.relative_to(root))] = plain_text_of(path)
    return out


# ── chunk-level survival of identifiers ─────────────────────────────────────

@lru_cache(maxsize=None)
def chunk_texts(text: str, *, max_piece_tokens: int | None = None,
                min_piece_tokens: int | None = None) -> tuple[str, ...]:
    """Pieces exactly as app.structure_chunker would cut them, rendered for LightRAG.

    Cached: the same documents are re-cut across windows and probes many times in one
    report, and chunking dominates the cost of this harness.
    """
    plan = structure_chunker.chunk_document(
        text,
        max_piece_tokens=max_piece_tokens,
        min_piece_tokens=min_piece_tokens,
    )
    return tuple(piece.render() for piece in plan.pieces)


def identifier_survival(document: str, identifiers_of_interest: Sequence[str], *,
                        max_piece_tokens: int | None = None,
                        min_piece_tokens: int | None = None) -> dict[str, int]:
    """How many identifiers are still findable verbatim after each stage of OUR pipeline.

    Stages: raw text -> after the LightRAG-imitating sanitiser -> after chunking
    (present in at least one piece). An identifier lost at any stage is one a
    verbatim support gate would demand and never find.
    """
    sanitised = lightrag_text.sanitize_like_lightrag(document)
    pieces = chunk_texts(document, max_piece_tokens=max_piece_tokens,
                         min_piece_tokens=min_piece_tokens)
    joined_pieces = "\n".join(pieces)
    out = {"total": 0, "missing_in_raw": 0, "lost_to_sanitiser": 0, "lost_to_chunking": 0}
    for code in identifiers_of_interest:
        out["total"] += 1
        in_raw = code in document
        in_sanitised = code in sanitised
        in_pieces = code in joined_pieces
        if not in_raw:
            out["missing_in_raw"] += 1
        elif not in_sanitised:
            out["lost_to_sanitiser"] += 1
        elif not in_pieces:
            out["lost_to_chunking"] += 1
    return out


# ── per-company analysis ────────────────────────────────────────────────────

# ── retrieval stand-in: what the answerer is actually shown ─────────────────
# A deterministic lexical stand-in, NOT an embedding model. It cannot say what
# LightRAG would retrieve. It is here for one narrow question that does not need
# semantics: when a probe asks about an identifier that exists nowhere, does the
# top of the retrieval list contain lookalikes anyway, and is that visible in a
# similarity score at all?

_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall((text or "").lower()))


def lexical_scores(query: str, chunks: Sequence[str]) -> list[float]:
    """Jaccard token overlap per chunk. Set-based so chunk length does not dominate."""
    return _score_against(query, [_tokens(c) for c in chunks])


def _score_against(query: str, chunk_sets: Sequence[set[str]]) -> list[float]:
    """Same arithmetic, with token sets already built (tokenising a chunk is the cost)."""
    q = _tokens(query)
    return [len(q & c) / len(q | c) if (q or c) else 0.0 for c in chunk_sets]


def probe_retrieval_separation(manifest: dict[str, Any], documents: dict[str, str], *,
                               top_k: int = 16) -> dict[str, Any]:
    """Compare an identifier-support gate against a similarity-score threshold.

    For every unanswerable probe: does the asked code appear in the top-k chunks, and
    how high is the best similarity score there? For every scoped probe: does the code
    a CORRECT answer would cite appear in the top-k? A score threshold is only usable
    if the two score distributions separate; the identifier rule is usable if the first
    column is empty and the second is not.
    """
    chunks = [piece for text in documents.values() for piece in chunk_texts(text)]
    chunk_sets = [_tokens(piece) for piece in chunks]
    out: dict[str, Any] = {}
    for shape, rows, code_key in (("unanswerable", manifest["probes"]["unanswerable"], "fact_code"),
                                  ("scoped", manifest["probes"]["scoped"], "expected_fact_code")):
        scores: list[float] = []
        codes_present = 0
        lookalike_counts: list[int] = []
        code_chunk_ranks: list[int] = []
        planted_codes = _all_codes(manifest)
        for row in rows:
            code = row[code_key]
            scored = list(zip(_score_against(row["question"], chunk_sets), chunks))
            ranked = sorted(scored, key=lambda pair: -pair[0])[:top_k]
            scores.append(max(score for score, _ in ranked) if ranked else 0.0)
            top_text = "\n".join(text for _, text in ranked)
            if verbatim_support(code, top_text):
                codes_present += 1
            # how many OTHER company codes were still handed to the answerer
            lookalike_counts.append(sum(
                1 for other in planted_codes
                if other != code and verbatim_support(other, top_text)))
            # Where does a chunk that actually contains the asked code rank? If it is
            # beyond chunk_top_k, the code never reached the answerer; if it is inside,
            # an "unanswerable" probe is answerable in the index.
            ranked_all = sorted(scored, key=lambda pair: -pair[0])
            ranks = [position for position, (_score, text) in enumerate(ranked_all, start=1)
                     if verbatim_support(code, text)]
            if ranks:
                code_chunk_ranks.append(min(ranks))
        ordered = sorted(scores)
        out[shape] = {
            "probes": len(rows),
            "asked_or_expected_code_in_top_k": codes_present,
            "lookalike_planted_codes_in_top_k_min": min(lookalike_counts, default=0),
            "lookalike_planted_codes_in_top_k_max": max(lookalike_counts, default=0),
            "best_score_min": round(ordered[0], 4) if ordered else 0.0,
            "best_score_median": round(ordered[len(ordered) // 2], 4) if ordered else 0.0,
            "best_score_max": round(ordered[-1], 4) if ordered else 0.0,
            # null = the code is in NO chunk of this pool (the honest unanswerable case)
            "best_code_chunk_rank_min": min(code_chunk_ranks) if code_chunk_ranks else None,
            "best_code_chunk_rank_max": max(code_chunk_ranks) if code_chunk_ranks else None,
            "probes_with_a_code_chunk_at_all": len(code_chunk_ranks),
        }
    if out["unanswerable"]["probes"] and out["scoped"]["probes"]:
        out["score_threshold_separates"] = (
            out["unanswerable"]["best_score_max"] < out["scoped"]["best_score_min"])
    out["chunks_indexed_for_stand_in"] = len(chunks)
    return out


def _all_codes(manifest: dict[str, Any]) -> list[str]:
    return [entry["code"] for entry in manifest["facts"]]


def analyse_company(corpus_dir: Path) -> dict[str, Any]:
    manifest = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    documents = corpus_documents(corpus_dir)
    corpus_identifiers: Counter[str] = Counter()
    for text in documents.values():
        corpus_identifiers.update(identifiers(text))

    planted = [entry["code"] for entry in manifest["facts"]]
    unanswerable = [entry["code"] for entry in manifest["unanswerable_facts"]]
    home_of = {entry["code"]: entry["document"] for entry in manifest["facts"]}
    fact_text_of = {entry["code"]: entry["text"] for entry in manifest["facts"]}

    # 1. Unanswerable probes: is the code anywhere, and what lookalikes are near it?
    #    Checked over document text only, and separately over manifest.json, because
    #    manifest.json sat INSIDE the company folder and was ingested as a document
    #    until the skip rule landed (see the report): if it reached LightRAG, the
    #    "code that exists in no document" existed in the index, with its answer key.
    manifest_raw = (corpus_dir / "manifest.json").read_text(encoding="utf-8")
    # Chunking is the expensive part of this harness, so each document is cut once per
    # window and reused by every check below.
    budgets = (None, 7500, 200, 40, 10)     # None = the shipped defaults
    def pieces_for(text: str, window: int | None) -> tuple[str, ...]:
        return (chunk_texts(text) if window is None else
                chunk_texts(text, max_piece_tokens=window, min_piece_tokens=0))

    all_chunks = [piece for text in documents.values() for piece in pieces_for(text, None)]
    manifest_chunks = chunk_texts(manifest_raw)
    unanswerable_rows = []
    for code in unanswerable:
        misses = near_misses(code, corpus_identifiers, company_code=manifest["company_code"])
        unanswerable_rows.append({
            "code": code,
            "verbatim_hits_in_documents": corpus_identifiers.get(code, 0),
            "token_support_in_document_chunks": any(
                verbatim_support(code, chunk) for chunk in all_chunks),
            "token_support_in_manifest_chunks": any(
                verbatim_support(code, chunk) for chunk in manifest_chunks),
            "substring_would_claim_support_in_documents": any(
                verbatim_support(code, chunk, match="substring") for chunk in all_chunks),
            "near_miss_same_company_code": len(misses["same_company_code"]),
            "near_miss_same_first_segment": len(misses["same_first_segment"]),
            "substring_hazard_identifiers": len(misses["shares_a_subtoken"]),
        })

    # 2. Answerable probes: would a verbatim gate find the code it needs?
    answerable_rows = []
    for code in planted:
        home = documents.get(home_of[code], "")
        at_home = verbatim_support(code, home)
        # Does any cut our chunker can make split the identifier so that NO piece
        # contains it verbatim? Swept down to a pathological window, because a gate that
        # demands the identifier is only fair if our own pipeline never destroys it.
        # (_split_oversize documents "never mid-word": the sweep is the proof, not the
        # docstring.)
        curve = {budget: 0 if any(verbatim_support(code, piece)
                                  for piece in pieces_for(home, budget)) else 1
                 for budget in budgets if budget is not None}
        # Can the sanitiser break it? It only deletes C0 controls and surrogates, so an
        # identifier that is intact in the raw text stays intact; measured, not assumed.
        survives_sanitiser = (not at_home) or verbatim_support(
            code, lightrag_text.sanitize_like_lightrag(home))
        # Is the SENTENCE verbatim in the rendered piece? Measured on plain-text homes
        # only, where extraction is exact. It matters because a grounding rule that
        # demands sentence-level support would false-fire: the chunker re-wraps lines and
        # rewrites speaker prefixes ("E. Tancredi:" -> "Tancredi:"), so the rendered piece
        # is not byte-identical source text even though every identifier survives.
        sentence_ok = None
        if Path(home_of[code]).suffix.lower() in {".txt", ".md", ".csv", ".html", ".htm", ".tex", ".rst", ".json"}:
            sentence_ok = any(verbatim_support(code, piece) and fact_text_of[code][:40] in piece
                              for piece in pieces_for(home, None))
        answerable_rows.append({
            "code": code,
            "code_in_home_document": at_home,
            "survives_sanitiser": survives_sanitiser,
            "lost_to_chunking_by_budget": curve,
            "fact_sentence_verbatim_in_rendered_piece": sentence_ok,
        })

    return {
        "company": manifest["company"],
        "company_code": manifest["company_code"],
        "seed": manifest["seed"],
        "documents_read": len(documents),
        "distinct_identifiers_in_corpus": len(corpus_identifiers),
        "planted_fact_codes": len(planted),
        "unanswerable_codes": len(unanswerable),
        "unanswerable_absent_from_document_chunks": sum(
            1 for r in unanswerable_rows if not r["token_support_in_document_chunks"]),
        "unanswerable_present_in_manifest_chunks": sum(
            1 for r in unanswerable_rows if r["token_support_in_manifest_chunks"]),
        "unanswerable_substring_false_support_in_documents": sum(
            1 for r in unanswerable_rows if r["substring_would_claim_support_in_documents"]),
        "unanswerable_near_miss_same_company_code_min": min(
            (r["near_miss_same_company_code"] for r in unanswerable_rows), default=0),
        "unanswerable_near_miss_same_first_segment_max": max(
            (r["near_miss_same_first_segment"] for r in unanswerable_rows), default=0),
        "substring_hazard_identifiers_total": sum(
            r["substring_hazard_identifiers"] for r in unanswerable_rows),
        "answerable_codes_verbatim_in_home_document": sum(
            1 for r in answerable_rows if r["code_in_home_document"]),
        "answerable_codes_lost_to_sanitiser": sum(
            1 for r in answerable_rows if not r["survives_sanitiser"]),
        "answerable_codes_lost_to_chunking_by_budget": {
            budget: sum(r["lost_to_chunking_by_budget"][budget] for r in answerable_rows)
            for budget in (7500, 200, 40, 10)},
        "answerable_codes_count": len(answerable_rows),
        "plaintext_facts": sum(1 for r in answerable_rows
                               if r["fact_sentence_verbatim_in_rendered_piece"] is not None),
        "plaintext_facts_sentence_verbatim_in_piece": sum(
            1 for r in answerable_rows if r["fact_sentence_verbatim_in_rendered_piece"]),
        "retrieval_stand_in": probe_retrieval_separation(manifest, documents),
        # The pre-fix state: manifest.json sat in the company folder and was ingested
        # as a document until the skip rule landed, so the same probes are re-run with
        # the manifest inside the pool. If the code reaches the top-k here, the probe is
        # no longer unanswerable in the index, and the manifest's own sentence about the
        # code ("No record exists for X: ... never measured in N of M reviews") reads
        # like a recorded outcome to an answerer that must say something.
        "retrieval_stand_in_with_manifest": probe_retrieval_separation(
            manifest, {**documents, "manifest.json": manifest_raw}),
        "unanswerable_detail": unanswerable_rows,
        "answerable_detail": answerable_rows,
    }


def store_report(roots: Sequence[Path]) -> dict[str, Any]:
    """The probe sets of every company scored against the WHOLE store.

    This is the shape Hermes ran: both companies indexed together, 28 scoped + 15
    cross-company + 12 unanswerable = 55 probes. Retrieval is store-wide, so a probe
    about one company sees the other company's lookalikes too.
    """
    pool: dict[str, str] = {}
    manifests = []
    for root in roots:
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        manifests.append((root, manifest))
        for rel, text in corpus_documents(root).items():
            pool[f"{manifest['company']}/{rel}"] = text
    pool_with_manifests = dict(pool)
    for root, manifest in manifests:
        pool_with_manifests[f"{manifest['company']}/manifest.json"] = (
            root / "manifest.json").read_text(encoding="utf-8")

    out: dict[str, Any] = {"companies": [m["company"] for _, m in manifests],
                           "pool_documents": len(pool),
                           "probe_counts": {}}
    for label, documents in (("documents_only", pool), ("documents_plus_manifest_json", pool_with_manifests)):
        totals = {"scoped": [0, 0, []], "unanswerable": [0, 0, []]}  # probes, in-top-k, ranks
        for _, manifest in manifests:
            separation = probe_retrieval_separation(manifest, documents)
            for shape in ("scoped", "unanswerable"):
                totals[shape][0] += separation[shape]["probes"]
                totals[shape][1] += separation[shape]["asked_or_expected_code_in_top_k"]
                if separation[shape]["probes_with_a_code_chunk_at_all"]:
                    totals[shape][2].append(
                        (separation[shape]["best_code_chunk_rank_min"],
                         separation[shape]["best_code_chunk_rank_max"]))
        out[label] = {}
        for shape, (probes, hits, ranks) in totals.items():
            out[label][shape] = {
                "probes": probes,
                "code_in_top_k": hits,
                "code_chunk_rank_range": [min(rank_min for rank_min, _rank_max in ranks),
                                          max(rank_max for _rank_min, rank_max in ranks)]
                                         if ranks else None,
            }
    # How the 55-probe run is counted: probe ROWS for scoped (28) and unanswerable (12),
    # and one question per shared topic for cross-company (15), because that question is
    # the same text in both manifests. Note that 14 scoped rows per company cover only 7
    # distinct questions -- the generator writes one row per fact and two facts can share
    # a topic, so a 55-probe run has 48 distinct questions. Worth knowing before anyone
    # treats per-probe accuracy as 55 independent samples.
    questions: dict[str, set[str]] = {}
    rows: dict[str, int] = {}
    for _, manifest in manifests:
        for shape, probe_rows in manifest["probes"].items():
            rows[shape] = rows.get(shape, 0) + len(probe_rows)
            questions.setdefault(shape, set()).update(row["question"] for row in probe_rows)
    out["probe_counts"] = {
        shape: (len(questions[shape]) if shape == "cross_company" else rows[shape])
        for shape in rows
    }
    out["distinct_questions"] = {shape: len(unique) for shape, unique in questions.items()}
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", nargs="+", help="a generated company corpus directory")
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    args = parser.parse_args(argv)

    reports = []
    for raw in args.corpus:
        root = Path(raw)
        if not (root / "manifest.json").is_file():
            print(f"!! {root}: no manifest.json -- not a generated corpus", file=sys.stderr)
            return 2
        reports.append(analyse_company(root))

    for report in reports:
        print(f"\n== {report['company']} (seed {report['seed']}, code {report['company_code']}) ==")
        for key in ("documents_read", "distinct_identifiers_in_corpus", "planted_fact_codes",
                    "unanswerable_codes", "unanswerable_absent_from_document_chunks",
                    "unanswerable_present_in_manifest_chunks",
                    "unanswerable_substring_false_support_in_documents",
                    "unanswerable_near_miss_same_company_code_min",
                    "unanswerable_near_miss_same_first_segment_max",
                    "substring_hazard_identifiers_total",
                    "answerable_codes_verbatim_in_home_document",
                    "answerable_codes_lost_to_sanitiser",
                    "answerable_codes_lost_to_chunking_by_budget",
                    "answerable_codes_count", "plaintext_facts",
                    "plaintext_facts_sentence_verbatim_in_piece"):
            print(f"  {key}: {report[key]}")
        stand_in = report["retrieval_stand_in"]
        print(f"  retrieval_stand_in (lexical, top_k=16, {stand_in['chunks_indexed_for_stand_in']} chunks):")
        for shape in ("scoped", "unanswerable"):
            row = stand_in[shape]
            print(f"    {shape}: probes={row['probes']} code_in_top_k={row['asked_or_expected_code_in_top_k']} "
                  f"lookalike_planted_codes_in_top_k={row['lookalike_planted_codes_in_top_k_min']}"
                  f"..{row['lookalike_planted_codes_in_top_k_max']} "
                  f"best_score min/med/max={row['best_score_min']}/{row['best_score_median']}/{row['best_score_max']} "
                  f"code_chunk_rank={row['best_code_chunk_rank_min']}..{row['best_code_chunk_rank_max']}")
        print(f"    score_threshold_separates: {stand_in.get('score_threshold_separates')}")
        with_manifest = report["retrieval_stand_in_with_manifest"]
        print(f"  same probes, manifest.json INSIDE the pool (the pre-skip-rule state, "
              f"{with_manifest['chunks_indexed_for_stand_in']} chunks):")
        for shape in ("scoped", "unanswerable"):
            row = with_manifest[shape]
            print(f"    {shape}: code_in_top_k={row['asked_or_expected_code_in_top_k']}/{row['probes']} "
                  f"code_chunk_rank={row['best_code_chunk_rank_min']}..{row['best_code_chunk_rank_max']} "
                  f"(of {with_manifest['chunks_indexed_for_stand_in']} chunks)")
    if len(roots := [Path(raw) for raw in args.corpus]) > 1:
        store = store_report(roots)
        print(f"\n== store-level: {' + '.join(store['companies'])} indexed together ==")
        print(f"  probe counts as run: {store['probe_counts']} "
              f"(distinct questions: {store['distinct_questions']})")
        for label in ("documents_only", "documents_plus_manifest_json"):
            print(f"  {label}: " + " | ".join(
                f"{shape}: {row['code_in_top_k']}/{row['probes']} code in top-16, "
                f"best code-chunk rank {row['code_chunk_rank_range']}"
                for shape, row in store[label].items()))
        reports.append({"store": store})
    if args.json:
        print(json.dumps({"companies": reports}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
