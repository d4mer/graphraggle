#!/usr/bin/env python3
"""How many document names collide across companies, and whether the bytes agree.

This is the number that decides whether stack2's R1-R3 can re-run on the existing index with the
basename join alone, or whether the corpus needs re-ingesting under company-qualified names
(`COMPANY_QUALIFIED_FILE_SOURCE`). Three cases, counted apart because they need different answers:

- **unique** - one company holds that name: the basename join attributes it, no ambiguity.
- **shared identical bytes** - several companies own the same file: the join attributes it to all of
  them, which is correct (that is what `COMPANY_AWARE_DEDUPE` implies upstream).
- **ambiguous** - one name, different bytes, different companies: never attributed; the citation is
  refused. Every row in this bucket is a re-ingest candidate.

Corpora are read from disk; a state database is read read-only. Only names, counts and digests are
printed - never file contents, and names are hashed unless `--show-names` is passed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

def company_slug(company: str) -> str:
    """Mirrors app.document_identity.company_slug.

    Duplicated on purpose: this script runs on a stack host where importing the app package needs the
    whole settings surface, and an audit tool that cannot start without a configured gateway is useless
    in an incident. tests/test_gl10_basename_audit.py pins that the two rules stay identical.
    """
    slug = "".join(character.lower() if character.isalnum() else "-" for character in company)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _classify(entries: dict[str, list[tuple[str, str]]]) -> dict[str, dict]:
    """entries: name -> [(company, content_digest)]. Returns the buckets with counts."""
    buckets = {"unique": [], "shared_identical": [], "ambiguous": [],
               "unattributed_rows": [], "multi_row_same_company": []}
    for name, pairs in sorted(entries.items()):
        companies = {company for company, _ in pairs if company}
        digests = {digest for _, digest in pairs if digest}
        if not companies:
            buckets["unattributed_rows"].append(name)
        elif len(companies) == 1:
            buckets["unique" if len(digests) <= len(companies) else "multi_row_same_company"].append(name)
        elif len(digests) == 1:
            buckets["shared_identical"].append(name)
        else:
            buckets["ambiguous"].append(name)
    return buckets


def _name_reports(names: list[str], show: bool) -> list[dict]:
    return [{"name": name} if show else {"name_sha256": hashlib.sha256(name.encode()).hexdigest()[:16]}
            for name in names]


args_show_names = False


def scan_corpora(roots: list[Path]) -> dict:
    entries: dict[str, list[tuple[str, str]]] = defaultdict(list)
    files = 0
    for root in roots:
        company = root.name
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            files += 1
            entries[path.name].append((company, _digest(path)))
    buckets = _classify(entries)
    qualified: dict[str, list[str]] = defaultdict(list)
    for name, pairs in entries.items():
        for company, _ in pairs:
            qualified[f"{company_slug(company)}--{name}"].append(company)
    qualified_collisions = sorted(name for name, owners in qualified.items() if len(set(owners)) > 1)
    return {
        "source": "corpus",
        "companies": sorted({company for pairs in entries.values() for company, _ in pairs}),
        "files": files,
        "distinct_basenames": len(entries),
        "counts": {key: len(value) for key, value in buckets.items()},
        "ambiguous_names": _name_reports(buckets["ambiguous"], args_show_names),
        "shared_identical_names": _name_reports(buckets["shared_identical"], args_show_names),
        "qualified_name_collisions": qualified_collisions,
    }


def scan_state_db(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    columns = {row[1] for row in conn.execute("PRAGMA table_info(documents)")}
    if "submitted_source" not in columns:
        print("note: this database has no `submitted_source` column - it predates the join fix",
              file=sys.stderr)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(documents)")]
    wanted = [column for column in ("path", "filename", "original_filename", "sha256", "company",
                                    "submitted_source", "status") if column in columns]
    rows = list(conn.execute(f"SELECT {', '.join(wanted)} FROM documents"))
    conn.close()
    entries: dict[str, list[tuple[str, str]]] = defaultdict(list)
    recorded = 0
    for row in rows:
        name = row["filename"] or Path(str(row["path"])).name
        entries[name].append((row["company"] or "", row["sha256"] or ""))
        if row["submitted_source"] if "submitted_source" in row.keys() else None:
            recorded += 1
    buckets = _classify(entries)
    return {
        "source": "state_db",
        "rows": len(rows),
        "rows_with_submitted_source": recorded,
        "distinct_basenames": len(entries),
        "counts": {key: len(value) for key, value in buckets.items()},
        "ambiguous_names": _name_reports(buckets["ambiguous"], args_show_names),
        "shared_identical_names": _name_reports(buckets["shared_identical"], args_show_names),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpus", action="append", default=[],
                        help="a company's corpus root (its directory name is the company)")
    parser.add_argument("--state-db", help="read a gateway state database read-only")
    parser.add_argument("--show-names", action="store_true",
                        help="print colliding basenames verbatim (synthetic corpora only)")
    parser.add_argument("--json", help="write the report here as well as stdout")
    args = parser.parse_args(argv)
    global args_show_names
    args_show_names = args.show_names

    reports = []
    if args.corpus:
        reports.append(scan_corpora([Path(path) for path in args.corpus]))
    if args.state_db:
        reports.append(scan_state_db(Path(args.state_db)))
    if not reports:
        parser.error("pass --corpus and/or --state-db")

    document = {"reports": reports}
    print(json.dumps(document, indent=2, sort_keys=True))
    if args.json:
        Path(args.json).write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    for report in reports:
        counts = report["counts"]
        print(f"\n{report['source']}: "
              f"unique={counts['unique']} shared_identical={counts['shared_identical']} "
              f"ambiguous={counts['ambiguous']} same_company_multi_row={counts['multi_row_same_company']} "
              f"unattributed={counts['unattributed_rows']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
