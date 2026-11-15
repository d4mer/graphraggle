#!/usr/bin/env python3
"""GL-13: validate a probe set against its source documents (counts only).

Why this exists
---------------
The go-live accuracy gate is scored on a NEW held-out probe set. A hand-authored set
fails quietly in four ways, and each one costs a run on a live stack to discover:

1. an evidence quote that is not verbatim, so an answer cannot be grounded;
2. an "unanswerable" probe whose phrase actually occurs in a source, so a refusal is
   scored as a miss;
3. a probe that reuses wording from an already-consumed set, so the set is not
   held-out any more;
4. an answer file and a question file that disagree, so the runner grades against
   something nobody reviewed.

This script checks all four and prints COUNTS ONLY - no question text, no quotes, no
source text - so the report can be pasted into a ticket without shipping corpus content.

Usage
-----
    .venv/bin/python scripts/probe_set_check.py <questions.jsonl> \
        --answer-key <answer-key.jsonl> --source-dir <dir of source documents> \
        [--forbid-overlap-with <other.jsonl> ...] [--overlap-limit 40] \
        [--max-evidence-chars 300] [--run-set <merged.jsonl>]

    GL-13b adds office documents, whose bytes are not text:

        --source-dir /home/maverick/work/solvay-src \
        --extracted-text OrderBook.docx=/home/maverick/work/solvay-probes/gl13b/extracted/OrderBook.txt

    A probe that cites `source_doc: OrderBook.docx` is checked against the text registered
    under that name. Binary sources (.pdf/.docx/.pptx/.xlsx) are never read as text: reading
    them that way yields junk in which every quote is "not verbatim" and every absent term is
    "absent" by accident, so the check would be decoration. The extracted text must come from
    the extractors the ingest worker runs in production (app/pdf_extract.py,
    app/docx_extract.py, app/pptx_extract.py), because that is the only text the system can
    ever retrieve: a sentence a PDF viewer shows but `pdftotext -layout` drops is unreachable,
    and a probe grounded in it is a trap.

Row shapes
----------
question row: id, question, source_doc, type, shape, history, absent_terms, and
    evidence/expected_answer EMPTY (answers live in the key file)
answer-key row: id, source_doc, evidence, expected_answer, key_phrases[], location
    {char_start, char_end, turn}, difficulty (easy|medium|hard), shape, type, history,
    absent_terms, and for unanswerable rows distinctive_phrase
unanswerable shapes (`unanswerable`, `near-miss`) carry an empty evidence in the key and
    a distinctive_phrase plus 3-6 absent_terms that must occur in NO source document.

Exit code 0 when every check passes, 1 with the failure list otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

# Reading these as text yields junk, so they must arrive through --extracted-text.
BINARY_SOURCE_SUFFIXES = (".pdf", ".docx", ".pptx", ".xlsx")
TYPES = ("single-fact", "multi-fact", "follow-up", "combine", "unanswerable")
SHAPES = ("fact", "synthesis", "table", "pdf", "docx", "pptx", "deep-turn",
          "unanswerable", "near-miss")
ANSWER_SHAPES = tuple(s for s in SHAPES if s not in ("unanswerable", "near-miss"))
UNANSWERABLE_SHAPES = ("unanswerable", "near-miss")
DIFFICULTIES = ("easy", "medium", "hard")
QUESTION_FIELDS = ("id", "question", "source_doc", "type", "shape")
KEY_FIELDS = ("id", "source_doc", "evidence", "expected_answer", "key_phrases",
              "location", "difficulty", "shape", "type")
ID_RE = re.compile(r"^[a-z]{2,4}\d{2,3}$")
# Gate constants. Both are overridable without editing the file, because the overlap
# ceiling and the quote budget are tuned per set:
#   GRAGGLE_PROBE_MAX_EVIDENCE_CHARS=400  GRAGGLE_PROBE_OVERLAP_LIMIT=64
MAX_EVIDENCE_CHARS = int(os.environ.get("GRAGGLE_PROBE_MAX_EVIDENCE_CHARS", "300"))
OVERLAP_LIMIT = int(os.environ.get("GRAGGLE_PROBE_OVERLAP_LIMIT", "40"))
MIN_ANSWER_KEY_PHRASES = 1


def normalise(text: str) -> str:
    """Comparison form: case-insensitive, whitespace-normalised."""
    return " ".join(text.split()).casefold()


def read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path.name} line {line_no}: invalid JSON ({exc})") from exc
        if not isinstance(parsed, dict):
            raise SystemExit(f"{path.name} line {line_no}: not an object")
        rows.append(parsed)
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n"
                            for row in rows), encoding="utf-8")


def load_sources(source_dir: Path, extracted: dict[str, str] | None = None,
                 ) -> tuple[dict[str, str], list[str]]:
    """Source documents keyed by filename and by stem, plus pre-extracted office text.

    Returns (sources, binary_files_skipped). `extracted` maps the name a probe cites
    (`OrderBook.docx`) to text extracted from it by the production extractor; those entries win
    over anything found on disk, and are registered under the name and its stem so either form
    of `source_doc` resolves.
    """
    sources: dict[str, str] = {}
    skipped: list[str] = []
    for path in sorted(source_dir.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() in BINARY_SOURCE_SUFFIXES:
            skipped.append(str(path.relative_to(source_dir)))
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.name in sources and sources[path.name] != text:
            raise SystemExit(f"duplicate source basename with different content: {path.name}")
        sources[path.name] = text
        sources.setdefault(path.stem, text)
    for name, text in (extracted or {}).items():
        sources[name] = text
        sources.setdefault(Path(name).stem, text)
    return sources, skipped


def parse_extracted_text(values: list[str]) -> dict[str, str]:
    """`NAME=PATH` repeated into {name: text}. Fails loudly on a typo or a missing file:
    a silently absent office source would turn every quote against it into a pass."""
    out: dict[str, str] = {}
    for value in values:
        name, separator, raw_path = value.partition("=")
        if not separator or not name.strip() or not raw_path.strip():
            raise SystemExit(f"--extracted-text wants NAME=PATH, got {value!r}")
        path = Path(raw_path)
        if not path.is_file():
            raise SystemExit(f"--extracted-text {name.strip()}: file not found: {path}")
        out[name.strip()] = path.read_text(encoding="utf-8")
    return out


def source_for(row: dict, sources: dict[str, str]) -> str | None:
    named = row.get("source_doc")
    if not isinstance(named, str) or not named.strip():
        return None
    if named in sources:
        return sources[named]
    basename = Path(named).name
    return sources.get(basename)


def max_shared_run(a: str, b: str, cap: int | None = None) -> int:
    """Longest run of normalised characters shared by both strings.

    Binary search on the length with a k-gram set: a shared 40-character run is what
    makes two probe sets overlap in practice, and this returns exactly that number
    without ever printing the text.
    """
    left, right = normalise(a), normalise(b)
    high = min(len(left), len(right))
    if cap is not None:
        high = min(high, cap)
    if high == 0:
        return 0
    lo, best = 1, 0
    while lo <= high:
        mid = (lo + high) // 2
        grams = {left[i:i + mid] for i in range(len(left) - mid + 1)}
        if any(right[i:i + mid] in grams for i in range(len(right) - mid + 1)):
            best = mid
            lo = mid + 1
        else:
            high = mid - 1
    return best


def check_questions(rows: list[dict], sources: dict[str, str], problems: list[str]) -> None:
    seen_ids: set[str] = set()
    for index, row in enumerate(rows, start=1):
        where = f"question line {index} id={row.get('id') or '?'}"
        for field in QUESTION_FIELDS:
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                problems.append(f"{where}: {field} missing or empty")
        ident = row.get("id")
        if isinstance(ident, str):
            if not ID_RE.match(ident):
                problems.append(f"{where}: id does not match {ID_RE.pattern}")
            if ident in seen_ids:
                problems.append(f"{where}: duplicate id")
            seen_ids.add(ident)
        if isinstance(row.get("type"), str) and row["type"] not in TYPES:
            problems.append(f"{where}: type {row['type']!r} not in {TYPES}")
        if isinstance(row.get("shape"), str) and row["shape"] not in SHAPES:
            problems.append(f"{where}: shape {row['shape']!r} not in {SHAPES}")
        if row.get("shape") and row.get("type") and row["shape"] in UNANSWERABLE_SHAPES:
            if row["type"] != "unanswerable":
                problems.append(f"{where}: {row['shape']} rows must be type unanswerable")
            terms = row.get("absent_terms")
            if not isinstance(terms, list) or not 3 <= len(terms) <= 6:
                problems.append(f"{where}: unanswerable rows need 3-6 absent_terms")
        if row.get("type") == "follow-up":
            history = row.get("history")
            if not isinstance(history, list) or not history:
                problems.append(f"{where}: follow-up rows need a non-empty history")
        elif row.get("history"):
            problems.append(f"{where}: only follow-up rows may carry history")
        for answer_field in ("evidence", "expected_answer"):
            value = row.get(answer_field, "")
            if isinstance(value, str) and value.strip():
                problems.append(f"{where}: {answer_field} must be empty in the question file")
        if source_for(row, sources) is None:
            named = row.get("source_doc")
            hint = ""
            if isinstance(named, str) and named.lower().endswith(BINARY_SOURCE_SUFFIXES):
                hint = f" - pass --extracted-text {named}=<extracted text file>"
            problems.append(f"{where}: source_doc {row.get('source_doc')!r} not found in source dir{hint}")


def check_answer_key(rows: list[dict], questions: list[dict], sources: dict[str, str],
                     source_texts: list[str], problems: list[str],
                     max_evidence_chars: int = MAX_EVIDENCE_CHARS) -> None:
    by_id = {row.get("id"): row for row in questions if isinstance(row.get("id"), str)}
    key_ids: set[str] = set()
    normalised_sources = [normalise(text) for text in source_texts]
    for index, row in enumerate(rows, start=1):
        where = f"answer-key line {index} id={row.get('id') or '?'}"
        for field in KEY_FIELDS:
            if field not in row:
                problems.append(f"{where}: {field} missing")
        ident = row.get("id")
        if ident in key_ids:
            problems.append(f"{where}: duplicate id in the answer key")
        key_ids.add(ident)
        if ident not in by_id:
            problems.append(f"{where}: no question row with this id")
        elif by_id[ident].get("source_doc") != row.get("source_doc"):
            problems.append(f"{where}: source_doc disagrees with the question row")
        if isinstance(row.get("difficulty"), str) and row["difficulty"] not in DIFFICULTIES:
            problems.append(f"{where}: difficulty {row['difficulty']!r} not in {DIFFICULTIES}")
        source = source_for(row, sources)
        if source is None:
            problems.append(f"{where}: source_doc not found in source dir")
            continue
        normalised_source = normalise(source)
        shape = row.get("shape")
        evidence = row.get("evidence")
        if shape in UNANSWERABLE_SHAPES:
            if isinstance(evidence, str) and evidence.strip():
                problems.append(f"{where}: {shape} rows must carry empty evidence")
            phrase = row.get("distinctive_phrase")
            if not isinstance(phrase, str) or not phrase.strip():
                problems.append(f"{where}: {shape} rows need a distinctive_phrase")
            elif any(normalise(phrase) in text for text in normalised_sources):
                problems.append(f"{where}: distinctive_phrase occurs in a source document")
            expected = row.get("expected_answer")
            if not isinstance(expected, str) or "not in the documents" not in expected:
                problems.append(f"{where}: {shape} expected_answer must contain 'not in the documents'")
            # An absent_term that is not absent makes the probe answerable, and the run scores a
            # correct refusal as a miss. The term is reported as a hash, not as text: this report is
            # the one thing allowed into a ticket, and an absent term is corpus wording.
            terms = row.get("absent_terms")
            if isinstance(terms, list):
                for position, term in enumerate(terms, start=1):
                    if not isinstance(term, str) or not term.strip():
                        problems.append(f"{where}: absent_term {position} is not a phrase")
                    elif any(normalise(term) in text for text in normalised_sources):
                        digest = hashlib.sha256(normalise(term).encode("utf-8")).hexdigest()[:8]
                        problems.append(f"{where}: absent_term #{position} (sha256 {digest}, "
                                        f"{len(term)} chars) occurs in a source document")
            continue
        if not isinstance(evidence, str) or not evidence.strip():
            problems.append(f"{where}: evidence missing")
            continue
        if len(evidence) > max_evidence_chars:
            problems.append(f"{where}: evidence is {len(evidence)} chars, limit {MAX_EVIDENCE_CHARS}")
        if normalise(evidence) not in normalised_source:
            problems.append(f"{where}: evidence is not a verbatim substring of the source")
        phrases = row.get("key_phrases")
        if not isinstance(phrases, list) or len(phrases) < MIN_ANSWER_KEY_PHRASES:
            problems.append(f"{where}: key_phrases must be a non-empty list")
        else:
            for phrase in phrases:
                if not isinstance(phrase, str):
                    problems.append(f"{where}: key phrase is not a string")
                elif normalise(phrase) not in normalise(evidence):
                    problems.append(f"{where}: key phrase {phrase[:40]!r} is not inside the evidence span")
        expected = row.get("expected_answer")
        if not isinstance(expected, str) or not expected.strip():
            problems.append(f"{where}: expected_answer missing")
        location = row.get("location")
        if not isinstance(location, dict):
            problems.append(f"{where}: location must be an object")
        else:
            start, end = location.get("char_start"), location.get("char_end")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start:
                problems.append(f"{where}: location needs integer char_start < char_end")
            elif end > len(source):
                problems.append(f"{where}: location is past the end of the source")
            elif normalise(source[start:end]) and normalise(evidence) != normalise(source[start:end]):
                problems.append(f"{where}: evidence does not match the located span")
    for ident in sorted(set(by_id) - key_ids):
        problems.append(f"question id {ident}: absent from the answer key")


def check_overlap(new_rows: list[dict], key_rows: list[dict], other_paths: list[Path],
                  limit: int) -> tuple[int, int, int, list[str]]:
    """Max shared normalised run between this set's questions and earlier sets'.

    Returns (max_question_run, max_evidence_run, count_of_pairs_at_or_over_limit, problems).
    """
    evidence_by_id = {row.get("id"): row.get("evidence", "") for row in key_rows}
    problems: list[str] = []
    max_question = 0
    max_evidence = 0
    over_limit = 0
    for other in other_paths:
        other_rows = read_jsonl(other)
        other_ids = {str(row.get("id")) for row in other_rows}
        collisions = sorted(str(row.get("id")) for row in new_rows if row.get("id") in other_ids)
        for ident in collisions:
            problems.append(f"id {ident} is already used in {other.name}")
        other_evidence = [row.get("evidence", "") or evidence_by_id.get(row.get("id"), "")
                          for row in other_rows]
        for row in new_rows:
            for other_row in other_rows:
                run = max_shared_run(row.get("question", ""), other_row.get("question", ""))
                max_question = max(max_question, run)
                if run >= limit:
                    over_limit += 1
                    problems.append(
                        f"question {row.get('id')} shares a {run}-char run with a question in {other.name}")
        new_evidence = [str(evidence_by_id.get(row.get("id"), "")) for row in new_rows]
        for mine in new_evidence:
            for theirs in other_evidence:
                if not mine or not theirs:
                    continue
                run = max_shared_run(mine, theirs)
                max_evidence = max(max_evidence, run)
    return max_question, max_evidence, over_limit, problems


def merge_run_set(questions: list[dict], key_rows: list[dict]) -> list[dict]:
    """Question rows with the key merged in: the shape a scoring runner expects."""
    key_by_id = {row["id"]: row for row in key_rows if isinstance(row.get("id"), str)}
    merged = []
    for row in questions:
        key = key_by_id.get(row.get("id"), {})
        out = dict(row)
        for field in ("evidence", "expected_answer", "absent_terms", "history", "type",
                      "shape", "source_doc"):
            if field in key:
                out[field] = key[field]
        merged.append(out)
    return merged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate a probe set against its sources (counts only).")
    parser.add_argument("questions", type=Path)
    parser.add_argument("--answer-key", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--forbid-overlap-with", type=Path, action="append", default=[])
    parser.add_argument("--overlap-limit", type=int, default=OVERLAP_LIMIT,
                        help="fail at this many shared normalised chars (env GRAGGLE_PROBE_OVERLAP_LIMIT)")
    parser.add_argument("--max-evidence-chars", type=int, default=MAX_EVIDENCE_CHARS,
                        help="quote budget per probe (env GRAGGLE_PROBE_MAX_EVIDENCE_CHARS)")
    parser.add_argument("--run-set", type=Path, default=None,
                        help="write the question+key merged file here (for the scoring runner)")
    parser.add_argument("--extracted-text", action="append", default=[], metavar="NAME=PATH",
                        help="pre-extracted text for an office source, named as source_doc cites it "
                             "(repeatable). Required for any .pdf/.docx/.pptx source.")
    args = parser.parse_args(argv)

    if not args.source_dir.is_dir():
        raise SystemExit(f"source dir not found: {args.source_dir}")
    questions = read_jsonl(args.questions)
    key_rows = read_jsonl(args.answer_key)
    extracted = parse_extracted_text(args.extracted_text)
    sources, skipped_binaries = load_sources(args.source_dir, extracted)
    source_texts = sorted({text for name, text in sources.items()})

    problems: list[str] = []
    check_questions(questions, sources, problems)
    check_answer_key(key_rows, questions, sources, source_texts, problems,
                     max_evidence_chars=args.max_evidence_chars)

    shape_counts = Counter(str(row.get("shape")) for row in questions)
    type_counts = Counter(str(row.get("type")) for row in questions)
    per_source = Counter(str(row.get("source_doc")) for row in questions)

    max_question_run = 0
    max_evidence_run = 0
    if args.forbid_overlap_with:
        max_question_run, max_evidence_run, _, overlap_problems = check_overlap(
            questions, key_rows, list(args.forbid_overlap_with), args.overlap_limit)
        problems.extend(overlap_problems)

    if args.run_set:
        write_jsonl(args.run_set, merge_run_set(questions, key_rows))

    print(json.dumps({
        "questions": len(questions),
        "answer_key_rows": len(key_rows),
        "source_documents": len(source_texts),
        "source_files_on_disk": len(list(args.source_dir.rglob("*"))) - sum(
            1 for p in args.source_dir.rglob("*") if p.is_dir()),
        "extracted_text_sources": sorted(extracted),
        "binary_sources_skipped": sorted(skipped_binaries),
        "by_shape": dict(sorted(shape_counts.items())),
        "by_type": dict(sorted(type_counts.items())),
        "per_source": dict(sorted(per_source.items())),
        "sha256_questions": hashlib.sha256(args.questions.read_bytes()).hexdigest(),
        "sha256_answer_key": hashlib.sha256(args.answer_key.read_bytes()).hexdigest(),
        "sha256_run_set": (hashlib.sha256(args.run_set.read_bytes()).hexdigest()
                           if args.run_set and args.run_set.exists() else None),
        "overlap": {
            "max_question_shared_normalised_chars": max_question_run,
            "max_evidence_shared_normalised_chars": max_evidence_run,
            "limit": args.overlap_limit,
            "compared_against": [path.name for path in args.forbid_overlap_with],
            "existing_question_counts": {path.name: len(read_jsonl(path))
                                        for path in args.forbid_overlap_with},
        },
        "failures": len(problems),
        "problems": problems[:40],
    }, indent=2, sort_keys=True))
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
