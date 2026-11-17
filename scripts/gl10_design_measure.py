#!/usr/bin/env python3
"""GL-10 part 3: offline numbers behind the four company-scoping designs.

Why this exists
---------------
The brief asked for the four candidate designs to be ranked on measured evidence, not on how
plausible each one sounds. Three of them turn on facts that can be measured here, on data this repo
generates: whether a chunk carries its company in its own text (design a), how many documents are
identical bytes under two companies (design d, per-company workspaces), and how much of a company's
material fits in one scoped prompt (design b/c, the retrieval-then-regenerate path).

What this is NOT
----------------
It never reads ``source_docs/``, ``lightrag_store/``, ``state/`` or any probe set, and it prints
counts only: no document text, no sentence, no company-name search results. Run it against the
output of ``scripts/make_synthetic_company.py`` (invented documents) and nothing else.

Usage
-----
    .venv/bin/python scripts/make_synthetic_company.py --company "Northwind Chemicals" \\
        --seed 7 --out /tmp/gl10-synth
    .venv/bin/python scripts/make_synthetic_company.py --company "Contoso Pharma" \\
        --seed 7 --out /tmp/gl10-synth
    .venv/bin/python scripts/gl10_design_measure.py --corpus /tmp/gl10-synth

Designs being measured (the ranking and the reasoning live in
``docs/decisions/gl10-scoped-retrieval-design.md``):

    a  company in the chunk header, so the embedding carries it (needs re-ingest)
    b  retrieve, filter references to the company through the state join, generate from that
    c  a larger retrieval pool, then post-filter, then regenerate
    d  per-company LightRAG workspaces
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.company_scope import match_key  # noqa: E402
from app.structure_chunker import chunk_document  # noqa: E402

# The default of COMPANY_SCOPED_CONTEXT_MAX_CHARS (app/config.py). Kept as a local constant so this
# script never instantiates Settings, which requires the deployment's environment.
DEFAULT_CONTEXT_CAP_CHARS = 12000

TEXT_SUFFIXES = {".txt", ".md", ".text"}
FORBIDDEN = ("source_docs", "lightrag_store", "state", "uploads")


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def discover(corpus: Path) -> dict[str, list[Path]]:
    companies: dict[str, list[Path]] = {}
    for child in sorted(corpus.iterdir()):
        if not child.is_dir() or child.name.lower() in FORBIDDEN:
            continue
        files = [path for path in sorted(child.rglob("*"))
                 if path.is_file() and path.name != "manifest.json"]
        if files:
            companies[child.name] = files
    return companies


def measure(corpus: Path, cap_chars: int = DEFAULT_CONTEXT_CAP_CHARS) -> dict:
    companies = discover(corpus)
    if len(companies) < 2:
        raise SystemExit("need at least two company folders to measure cross-company behaviour")

    by_sha: dict[str, set[str]] = defaultdict(set)
    basename_paths: dict[tuple[str, str], list[str]] = defaultdict(list)
    extension_counts: Counter[str] = Counter()
    per_company: dict[str, dict] = {}

    for company, files in companies.items():
        record = {"documents": len(files), "text_documents": 0, "pieces": 0,
                  "pieces_carrying_own_company": 0, "pieces_carrying_another_company": 0,
                  "piece_chars": []}
        for path in files:
            extension_counts[path.suffix.lower()] += 1
            by_sha[sha256_of(path)].add(company)
            basename_paths[(company, path.name)].append(str(path))
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            record["text_documents"] += 1
            plan = chunk_document(path.read_text(encoding="utf-8"), filename=path.name)
            for piece in plan.pieces:
                text = f"{piece.header or ''}\n{piece.body}"
                key = match_key(text)
                record["pieces"] += 1
                record["piece_chars"].append(len(text))
                own = match_key(company)
                if own and own in key:
                    record["pieces_carrying_own_company"] += 1
                if any(other != company and match_key(other) and match_key(other) in key
                       for other in companies):
                    record["pieces_carrying_another_company"] += 1
        per_company[company] = record

    shared_across_companies = {sha: sorted(owners) for sha, owners in by_sha.items()
                               if len(owners) > 1}
    within_company_duplicates = 0
    for company, files in companies.items():
        counts = Counter(sha256_of(path) for path in files)
        within_company_duplicates += sum(count - 1 for count in counts.values() if count > 1)

    ambiguous_within_company = sum(len(paths) - 1 for paths in basename_paths.values() if len(paths) > 1)
    basenames_by_name: dict[str, set[str]] = defaultdict(set)
    for (company, name), _ in basename_paths.items():
        basenames_by_name[name].add(company)
    ambiguous_across_companies = sum(1 for owners in basenames_by_name.values() if len(owners) > 1)

    cap = cap_chars
    pieces_fit = 0
    all_chars = sorted(char for record in per_company.values() for char in record["piece_chars"])
    used = 0
    for char in all_chars:
        if used + char + 60 > cap:     # 60: a conservative "Source:" line allowance
            break
        used += char + 60
        pieces_fit += 1

    totals = {
        "companies": len(companies),
        "documents_total": sum(record["documents"] for record in per_company.values()),
        "documents_by_extension": dict(sorted(extension_counts.items())),
        "documents_identical_bytes_in_more_than_one_company": len(shared_across_companies),
        "documents_identical_bytes_within_a_company": within_company_duplicates,
        "basenames_ambiguous_within_a_company": ambiguous_within_company,
        "basenames_shared_across_companies": ambiguous_across_companies,
        "text_pieces_total": sum(record["pieces"] for record in per_company.values()),
        "text_pieces_carrying_own_company_name": sum(
            record["pieces_carrying_own_company"] for record in per_company.values()),
        "text_pieces_carrying_another_companys_name": sum(
            record["pieces_carrying_another_company"] for record in per_company.values()),
        "median_piece_chars": all_chars[len(all_chars) // 2] if all_chars else 0,
        "pieces_fitting_scoped_context_cap": pieces_fit,
        "scoped_context_cap_chars": cap,
    }
    return {"totals": totals,
            "per_company": {company: {key: (sorted(values) if key == "piece_chars" else values)
                                      for key, values in record.items()}
                            for company, record in per_company.items()}}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", required=True,
                        help="directory holding one sub-directory per company "
                             "(scripts/make_synthetic_company.py output)")
    parser.add_argument("--context-cap-chars", type=int, default=DEFAULT_CONTEXT_CAP_CHARS,
                        help="the COMPANY_SCOPED_CONTEXT_MAX_CHARS value to size against")
    parser.add_argument("--json", help="optional path for the full counts (refuses to write in a "
                                       "corpus directory)")
    args = parser.parse_args()

    corpus = Path(args.corpus).resolve()
    if not corpus.is_dir():
        print(f"corpus directory not found: {corpus}", file=sys.stderr)
        return 2
    for part in corpus.parts:
        if part in FORBIDDEN:
            print(f"refusing to read a protected directory: {corpus}", file=sys.stderr)
            return 2

    report = measure(corpus, args.context_cap_chars)
    for name, value in report["totals"].items():
        print(f"{name}: {value}")
    for company, record in sorted(report["per_company"].items()):
        pieces = record["pieces"]
        own = record["pieces_carrying_own_company"]
        other = record["pieces_carrying_another_company"]
        print(f"company {company}: pieces={pieces} own={own} "
              f"({(own / pieces * 100 if pieces else 0):.1f}%) other={other} "
              f"({(other / pieces * 100 if pieces else 0):.1f}%)")
    if args.json:
        out = Path(args.json).resolve()
        if corpus in out.parents:
            print("refusing to write the report inside the corpus", file=sys.stderr)
            return 2
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        print(f"report: {out} (counts only)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
