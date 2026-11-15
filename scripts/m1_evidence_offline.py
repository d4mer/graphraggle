"""Offline measurement harness for the M1 gate's evidence problem. Investigation only.

The 2026-11-14 stack2 run (114 identifier probes, `GROUNDING_CHECK_ASKED_IDENTIFIER` flag-only)
showed the gate firing on 96 of 102 identifier-asking rows, 86 of them unexpectedly, and
`grounding_references_without_content` = 10-11 on EVERY row. Read that as: online, the gate's
evidence argument was empty, so every asked identifier was unsupported by construction.

This script answers the two questions that can be answered without a live stack, and quantifies the
gate-design choices the run raised. It changes nothing in `app/` — it measures.

Q3  Co-location: for every planted fact, is the chunk piece that holds the planted SENTENCE also
    the piece that holds the fact CODE (verbatim, token match)? If not, no evidence-side gate over
    retrieved chunks can work without a chunking change. Reported per company and per document
    route (text-route = we submit the extracted text and LightRAG chunks what we cut; office-route =
    LightRAG extracts the bytes itself, so our pieces are an approximation).

Q4  Gate-design variants, scored against two evidence models:
      evidence_none        references carry no chunk text (what the 2026-11-14 run had)
      evidence_own_doc     every piece of the probe's own document (the kindest retrieval
                           assumption: the model was shown the document that holds the fact)
      evidence_all_docs    every piece of every indexed document of that company (corpus-side
                           support: "does this code exist at all", not "was it in the context")
    Variants (each independent; all off = the shipped rule in app/grounding.py):
      join_hyphen_split    replace adjacent identifier tokens with the single hyphen-joined
                           candidate (recovers a code opened at a hyphen; costs two-code rows)
      join_hyphen_union    keep the halves AND add the joined candidate (measured alternative)
      reconstruct_if_known   replace an adjacent token pair with its hyphen-joined form ONLY when
                           that joined form is itself an identifier that occurs in the evidence.
                           This is the only variant that can tell a code typed with a space where a
                           hyphen belongs from a question naming two codes (or one real plus one
                           invented code): token geometry alone cannot.
      fold_case            case-insensitive identifier extraction and matching
      drop_company_code    identifiers equal to a company code are not treated as asked
      stricter_all_unsupported  fire only when EVERY asked code is unsupported (the shipped rule
                           fires when ANY is)

    For each evidence model x variant set: how many ANSWERABLE rows the gate would refuse, broken
    down by probe_kind (so by-design fires and rule-brittleness fires stay separated), and how many
    unanswerable controls it catches.

Output is counts only: no question text, no answers, no reference content. `--json` for machines.

Q1 (do references carry content, and who drops it) and Q2 (which endpoint could give the gateway the
chunk text) cannot be settled from this machine; see docs/ops/runs/2026-11-15-m1-evidence-blindness.md
and scripts/m1_reference_shape_probe.py, which is the one command that settles Q1 on the stack.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from functools import lru_cache
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import grounding, structure_chunker  # noqa: E402
from app.lightrag_text import sanitize_like_lightrag  # noqa: E402

DEFAULT_CORPORA_ROOT = Path("/home/maverick/work/synthetic-co")
DEFAULT_PROBE_ROOT = Path("/home/maverick/work/synthetic-co-probes")
DEFAULT_COMPANIES = ("Northwind Chemicals", "Contoso Pharma")
OFFICE_SUFFIXES = {".pdf", ".docx", ".pptx"}

# Case-folded extraction, used ONLY when the fold_case variant is on. The shipped rule
# (app/grounding.IDENTIFIER_RE) requires an uppercase first character.
CASE_FOLD_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+\b")

VARIANTS = ("join_hyphen_split", "join_hyphen_union", "reconstruct_if_known", "fold_case",
          "drop_company_code", "stricter_all_unsupported")
EVIDENCE_MODELS = ("evidence_none", "evidence_own_doc", "evidence_all_docs")


def _load_harness():
    path = REPO_ROOT / "scripts" / "unanswerable_grounding.py"
    spec = importlib.util.spec_from_file_location("unanswerable_grounding_for_m1", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_harness = _load_harness()


@lru_cache(maxsize=None)
def pieces_of(text: str, max_piece_tokens: int | None = None) -> tuple[str, ...]:
    """Pieces as the repo chunker cuts them. ``max_piece_tokens=None`` means the configured window
    (STRUCTURE_CHUNKING_MAX_PIECE_TOKENS, 7500 by default). A smaller number is what a re-cut by
    LightRAG's own chunker would look like, and the sweep below measures how much that costs."""
    plan = structure_chunker.chunk_document(
        text, **({} if max_piece_tokens is None else {"max_piece_tokens": max_piece_tokens}))
    return tuple(piece.render() for piece in plan.pieces)


# ── Q3: is the code in the same piece as its fact sentence? ──────────────────

def co_location(corpora_root: Path, companies: Sequence[str],
                max_piece_tokens: int | None = None) -> dict[str, Any]:
    report: dict[str, Any] = {"companies": {}, "totals": Counter()}
    for company in companies:
        company_dir = corpora_root / company
        manifest_path = company_dir / "manifest.json"
        if not manifest_path.exists():
            report["companies"][company] = {"error": f"{manifest_path} not found"}
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pieces = corpus_pieces(corpora_root, company, max_piece_tokens)
        cells: Counter = Counter()
        details: list[dict[str, Any]] = []
        # The company code as a standalone token: if it is in no piece, a question that names only
        # the company code can never be "supported" under the token rule, whatever the model saw.
        company_code = manifest.get("company_code") or ""
        all_pieces = [piece for rel in pieces for piece in pieces[rel]]
        cells["pieces_total"] = len(all_pieces)
        cells["company_code_standalone_pieces"] = sum(
            1 for piece in all_pieces if grounding.verbatim_support(company_code, piece))
        corpus_text = "\n".join(all_pieces)
        own_doc_text = {rel: "\n".join(pieces[rel]) for rel in pieces}
        for fact in manifest["facts"]:
            rel = fact["document"]
            pieces.setdefault(rel, ())
            route = "office" if Path(rel).suffix.lower() in OFFICE_SUFFIXES else "text"
            code = fact["code"]
            sentence = fact["text"]
            code_pieces = [i for i, piece in enumerate(pieces[rel])
                           if grounding.verbatim_support(code, piece)]
            sentence_pieces = [i for i, piece in enumerate(pieces[rel]) if sentence in piece]
            both = sorted(set(code_pieces) & set(sentence_pieces))
            label = ("no_piece_with_sentence" if not sentence_pieces else
                     ("no_piece_with_code" if not code_pieces else
                      ("co_located" if both else "different_pieces")))
            cells[f"{route}|{label}"] += 1
            cells[f"route|{route}"] += 1
            cells["code_supported_corpus_wide"] += int(bool(grounding.verbatim_support(code, corpus_text)))
            cells["code_supported_own_doc"] += int(bool(grounding.verbatim_support(code, own_doc_text[rel])))
            # Does the planted sentence carry the code inside it? If it does, "the fact was in the
            # context but the code was not" is not possible for that fact: they are the same bytes.
            cells["fact_text_contains_code"] += int(code in sentence)
            cells["fact_text_token_supports_code"] += int(bool(grounding.verbatim_support(code, sentence)))
            details.append({"fact_id": fact["fact_id"], "route": route, "verdict": label,
                            "piece_count": len(pieces[rel]),
                            "code_pieces": len(code_pieces),
                            "sentence_pieces": len(sentence_pieces),
                            "co_located_pieces": len(both)})
        report["companies"][company] = {"facts": len(manifest["facts"]),
                                        "cells": dict(cells),
                                        "pieces_total": sum(len(v) for v in pieces.values()),
                                        "documents": len(pieces)}
        report["companies"][company]["example_details"] = details[:5]
        for key, value in cells.items():
            report["totals"][key] += value
    report["totals"] = dict(report["totals"])
    return report


# ── Q4: gate-design variants under three evidence models ─────────────────────

def _tokens_in_order(text: str, variants: set[str]) -> list[str]:
    """Identifier-shaped tokens in the order they appear in the question."""
    cleaned = sanitize_like_lightrag(text)
    pattern = CASE_FOLD_RE if "fold_case" in variants else grounding.IDENTIFIER_RE
    found: list[str] = []
    for match in pattern.finditer(cleaned):
        token = match.group(0)
        if any(char.isdigit() for char in token):
            found.append(token)
    return found


def evidence_identifiers(evidence: str) -> set[str]:
    """Identifier-shaped tokens that occur in the evidence: what the corpus says exists."""
    return set(grounding.IDENTIFIER_RE.findall(evidence))


def asked_codes(question: str, company_code: str | None, variants: set[str],
                known: set[str] | None = None) -> list[str]:
    """The codes the gate would treat as asked, under a given variant set.

    ``join_hyphen_split`` pairs ADJACENT identifier tokens and replaces the pair with the single
    hyphen-joined candidate, because a half of a code opened at a hyphen is never in the evidence
    either. It can only repair a split whose both halves are still identifier-shaped ("NC-3617 T03"
    cannot be reassembled: "T03" survives, "NC-3617" and "T03" join to "NC-3617-T03", which is not
    the planted code).
    """
    tokens = _tokens_in_order(question, variants)
    if "reconstruct_if_known" in variants:
        known = known or set()
        rebuilt: list[str] = []
        index = 0
        while index < len(tokens):
            if index + 1 < len(tokens) and f"{tokens[index]}-{tokens[index + 1]}" in known:
                rebuilt.append(f"{tokens[index]}-{tokens[index + 1]}")
                index += 2
            else:
                rebuilt.append(tokens[index])
                index += 1
        tokens = rebuilt
    elif "join_hyphen_union" in variants:
        # The safe form of the same repair: keep the halves AND offer the joined candidate, so a
        # question naming two real codes still asks both.
        paired = []
        index = 0
        while index + 1 < len(tokens):
            paired.append(f"{tokens[index]}-{tokens[index + 1]}")
            index += 2
        while index < len(tokens):
            paired.append(tokens[index])
            index += 1
        tokens = tokens + paired
    elif "join_hyphen_split" in variants:
        joined: list[str] = []
        index = 0
        while index + 1 < len(tokens):
            joined.append(f"{tokens[index]}-{tokens[index + 1]}")
            index += 2
        while index < len(tokens):
            joined.append(tokens[index])
            index += 1
        tokens = joined
    if "drop_company_code" in variants and company_code:
        tokens = [token for token in tokens if token.upper() != company_code.upper()]
    return list(dict.fromkeys(tokens))


def supported_by(code: str, evidence: str, variants: set[str]) -> bool:
    if not evidence.strip():
        return False
    if "fold_case" not in variants:
        return grounding.verbatim_support(code, evidence)
    folded = re.sub(r"[^0-9A-Za-z-]+", " ", evidence.upper())
    return grounding.verbatim_support(code.upper(), folded)


def fires(codes: Sequence[str], evidence: str, variants: set[str]) -> bool:
    if not codes:
        return False
    verdicts = [supported_by(code, evidence, variants) for code in codes]
    if "stricter_all_unsupported" in variants:
        return not any(verdicts)     # stricter: fire only when EVERY asked code is unsupported
    return not all(verdicts)         # shipped rule: fire when ANY asked code is unsupported


def variant_sets() -> list[tuple[str, set[str]]]:
    """Baseline (the shipped rule), each variant alone, and the combination."""
    sets: list[tuple[str, set[str]]] = [("shipped_rule", set())]
    for name in VARIANTS:
        sets.append((f"+{name}", {name}))
    sets.append(("+all_variants", set(VARIANTS)))
    return sets


def co_location_sweep(corpora_root: Path, companies: Sequence[str],
                      budgets: Sequence[int | None] = (None, 1200, 400)) -> dict[str, Any]:
    """Co-location under different piece windows.

    The configured window (7500 tokens) is far larger than these synthetic documents, so at that
    setting one piece is one document and a code cannot be split from its sentence. A smaller window
    is what LightRAG's own chunker does when the structured submission path is off or fell back -
    then the answer stops being obvious, and the split counts below are what the gate would face.
    """
    out: dict[str, Any] = {}
    for budget in budgets:
        label = "configured" if budget is None else f"t{budget}"
        totals = co_location(corpora_root, companies, budget)["totals"]
        out[label] = {key: value for key, value in totals.items()
                      if "co_located" in key or "different_pieces" in key
                      or "no_piece_with_sentence" in key or key == "pieces_total"}
    return out


def load_probes(probe_root: Path, company: str) -> list[dict[str, Any]]:
    """Flatten one company's generated probe manifest (the scorer's format).

    The file is an object whose "probes" value holds the buckets the scorer consumes (scoped /
    cross_company / unanswerable). Row ids live under "id"; "expected_gate" is the string
    fire / not_fire / no_identifier, with expected_gate_asked and expected_unsupported beside it.
    """
    path = probe_root / company / "manifest.json"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found — run scripts/make_identifier_probes.py")
    document = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for shape, bucket in document["probes"].items():
        for row in bucket:
            rows.append({**row, "shape": shape, "company": company,
                         "company_code": document.get("company_code")})
    return rows


def corpus_pieces(corpora_root: Path, company: str,
                  max_piece_tokens: int | None = None) -> dict[str, tuple[str, ...]]:
    """Piece set for EVERY file the company manifest lists, as app.structure_chunker cuts the
    text we would submit for it. For office routes LightRAG extracts and chunks the bytes itself,
    so these pieces are our approximation of what its chunks contain, not a transcript of them."""
    manifest = json.loads((corpora_root / company / "manifest.json").read_text(encoding="utf-8"))
    company_dir = corpora_root / company
    pieces: dict[str, tuple[str, ...]] = {}
    for entry in manifest["files"]:
        rel = entry["path"]
        pieces.setdefault(rel, pieces_of(_harness.plain_text_of(company_dir / rel), max_piece_tokens))
    return pieces


def gate_simulation(probe_root: Path, companies: Sequence[str],
                    corpora_root: Path = DEFAULT_CORPORA_ROOT) -> dict[str, Any]:
    pieces_by_company = {company: corpus_pieces(corpora_root, company) for company in companies}

    rows: list[dict[str, Any]] = []
    for company in companies:
        all_pieces = [piece for pieces in pieces_by_company[company].values() for piece in pieces]
        for probe in load_probes(probe_root, company):
            doc = probe.get("expected_document")
            rows.append({
                "probe_id": probe["id"], "company": company, "company_code": probe["company_code"],
                "kind": probe["probe_kind"], "question": probe["question"],
                "expected_gate": probe["expected_gate"],
                "expected_fire": probe["expected_gate"] == "fire",
                "own_doc_pieces": pieces_by_company[company].get(doc, ()) if doc else (),
                "all_docs_pieces": all_pieces,
            })

    out: dict[str, Any] = {"rows": len(rows), "by_evidence": {}, "rows_detail": []}
    for model in EVIDENCE_MODELS:
        out["by_evidence"][model] = {}
        for label, variants in variant_sets():
            fired_answerable: Counter = Counter()
            fired_control: Counter = Counter()
            answerable = control = 0
            for row in rows:
                evidence = {"evidence_none": "",
                            "evidence_own_doc": "\n".join(row["own_doc_pieces"]),
                            "evidence_all_docs": "\n".join(row["all_docs_pieces"])}[model]
                codes = asked_codes(row["question"], row["company_code"], variants,
                                    evidence_identifiers(evidence))
                fired = fires(codes, evidence, variants)
                if row["kind"] == "control_unanswerable":
                    control += 1
                    if fired:
                        fired_control["refused"] += 1
                else:
                    answerable += 1
                    if fired:
                        fired_answerable[row["kind"]] += 1
            out["by_evidence"][model][label] = {
                "answerable_rows": answerable,
                "answerable_refused": sum(fired_answerable.values()),
                "answerable_refused_by_kind": dict(fired_answerable),
                "control_rows": control,
                "control_refused": sum(fired_control.values()),
            }
    return out


def run_reconciliation(probe_root: Path, companies: Sequence[str]) -> dict[str, Any]:
    """Predict what the shipped rule does when the evidence argument is empty.

    The 2026-11-14 run reported 102 identifier-asking rows, 96 fired, 86 of them unexpected. This
    recomputes those three numbers from the probe set alone, under ``evidence = ""``: if they match,
    empty evidence explains the whole run and no rule change is needed to explain it.
    """
    asked = fired = by_design = unexpected = total = answerable = 0
    disagreements: list[str] = []
    for company in companies:
        for probe in load_probes(probe_root, company):
            total += 1
            codes = grounding.identifiers(probe["question"])
            # Check the manifest's expectation against an independent re-extraction.
            if bool(codes) != (probe["expected_gate"] != "no_identifier"):
                disagreements.append(probe["id"])
            if probe["probe_kind"] == "control_unanswerable":
                continue
            answerable += 1
            if codes:
                asked += 1
                fired += 1                 # empty evidence -> every asked code unsupported
                if probe["expected_gate"] == "fire":
                    by_design += 1
                else:
                    unexpected += 1
    return {"probe_rows": total, "answerable_rows": answerable,
            "identifier_asking_rows": asked, "would_fire": fired,
            "by_design_fires": by_design, "unexpected_fires": unexpected,
            "answerable_rows_without_identifier": answerable - asked,
            "control_rows": total - answerable,
            "manifest_vs_reextraction_disagreements": disagreements}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 evidence/co-location measurement (offline)")
    parser.add_argument("--corpora-root", type=Path, default=DEFAULT_CORPORA_ROOT)
    parser.add_argument("--probe-root", type=Path, default=DEFAULT_PROBE_ROOT)
    parser.add_argument("--companies", default=",".join(DEFAULT_COMPANIES))
    parser.add_argument("--skip-co-location", action="store_true",
                        help="skip Q3 (it re-cuts every document)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    companies = [item.strip() for item in args.companies.split(",") if item.strip()]

    result: dict[str, Any] = {}
    try:
        result["run_reconciliation"] = run_reconciliation(args.probe_root, companies)
        result["gate_simulation"] = gate_simulation(args.probe_root, companies, args.corpora_root)
        if not args.skip_co_location:
            result["co_location"] = co_location(args.corpora_root, companies)
            result["co_location_sweep"] = co_location_sweep(args.corpora_root, companies)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    recon = result["run_reconciliation"]
    print("== run reconciliation (evidence = empty string, shipped rule) ==")
    for key in ("probe_rows", "answerable_rows", "control_rows", "identifier_asking_rows",
                "answerable_rows_without_identifier", "would_fire", "by_design_fires",
                "unexpected_fires"):
        print(f"  {key:26} {recon[key]}")
    print(f"  {'manifest disagreements':26} {len(recon['manifest_vs_reextraction_disagreements'])}")
    print("\n== gate simulation ==")
    for model, table in result["gate_simulation"]["by_evidence"].items():
        print(f"  {model}")
        for label, cell in table.items():
            kinds = ", ".join(f"{k}={v}" for k, v in sorted(cell["answerable_refused_by_kind"].items()))
            print(f"    {label:26} refused {cell['answerable_refused']:3}/{cell['answerable_rows']:3}"
                  f" answerable | controls refused {cell['control_refused']:2}/{cell['control_rows']:2}"
                  f" | {kinds}")
    if "co_location" in result:
        print("\n== Q3 co-location (code piece == fact-sentence piece) ==")
        for key, value in sorted(result["co_location"]["totals"].items()):
            print(f"  {key:40} {value}")
        print("\n== Q3 sensitivity: co-location under smaller piece windows ==")
        for label, cells in result["co_location_sweep"].items():
            line = "  " + f"{label:12}" + "  ".join(
                f"{key}={value}" for key, value in sorted(cells.items()))
            print(line[:200])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
