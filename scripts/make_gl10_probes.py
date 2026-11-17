#!/usr/bin/env python3
"""GL-10: the probe set for the "name the company in the question" measurement (counts only).

Why this exists
---------------
`docs/ops/gl10-company-scoping-probe-plan.md` says what stack2 must measure. Hermes should not have
to invent probe rows on the stack, and a scorer is only as good as the expectations it is given. This
script turns the two ingested GL-07 corpora into the ten probe shapes of that plan, with every
expectation written down: which companies a question is expected to scope to, which planted code the
answer should contain, which document the citation should be, and which rows must be refused.

Determinism contract
--------------------
Every row is derived from the corpus `manifest.json` (company names, codes, topics, files, facts) plus
fixed template lists, rotating by row index. No wall-clock time, no global `random` state, no reading
of documents: run it twice on the same corpus and the files are byte-identical. The corpora used on
stack2 are `/home/maverick/work/synthetic-co/{Northwind Chemicals,Contoso Pharma}` at seed 20261101.

Refusals, not guesses
---------------------
`validate()` runs BEFORE anything is written and refuses the whole set if any of these fail:

1. every expected planted code exists in that company's manifest (and an unanswerable row's code is
   in `unanswerable_facts` and in no fact of any company);
2. the question scopes to exactly the expected companies — checked with the shipped detector
   (`app.company_scope.detect_companies`) against the real company list, and for the near-miss shape
   checked to scope to NOTHING;
3. no question contains a run of four or more consecutive words from any fact sentence in either
   corpus, so a probe cannot be answerable by copying the question;
4. expected documents are real files in the manifest, ids are unique, `gl10-`-prefixed and cannot
   collide with the GL-13 id shape, and every row carries a shape in the plan;
5. alias rows name a company only through the alias, and the alias map they need is written into the
   manifest so the runbook sets exactly that environment.

Nothing here is committed: the output lives outside the repo and every file is written mode 600. The
script refuses to write inside the repo. Usage:

    .venv/bin/python scripts/make_gl10_probes.py            # default corpora and output
    .venv/bin/python scripts/make_gl10_probes.py --corpora-root DIR --out DIR --companies A B
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.company_scope import detect_companies  # noqa: E402

DEFAULT_CORPORA_ROOT = Path("/home/maverick/work/synthetic-co")
DEFAULT_OUTPUT_ROOT = Path("/home/maverick/work/synthetic-co-probes/gl10")
DEFAULT_COMPANIES = ("Northwind Chemicals", "Contoso Pharma")
ID_PREFIX = "gl10"
# scripts/probe_set_check.py uses this shape for the real-corpus GL-13 ids; a GL-10 id must never
# look like one, or a stack run would silently feed the wrong scorer.
GL13_ID_RE = re.compile(r"^[a-z]{2,4}\d{2,3}$")
CODE_RE = re.compile(r"\b[A-Z]{2,4}-\d{3,6}(?:-[A-Z0-9]+)*\b")
SHARED_FILE_HINT = "shared-between-companies"
MIN_ANSWER_TOKEN_RUN = 4

# Counts per shape. The plan's acceptance numbers are per-shape rates, so each shape needs enough
# rows to say something without pretending precision.
SHAPE_COUNTS: dict[str, int] = {
    "scoped": 12, "cross_company": 9, "unscoped": 6, "unanswerable": 6, "shared_file": 2,
    "alias": 3, "near_miss_word": 3, "unknown_org": 2, "punctuation_case": 3, "upload_door": 3,
}
SHAPE_KEYS = tuple(SHAPE_COUNTS)

# Question phrasings. Each names the company (or nothing) and a topic label; none contains a
# sentence from a document. Rotation by index keeps the set varied and the build deterministic.
SCOPED_TEMPLATES = (
    "In {company}, what is recorded about {topic}?",
    "What does {company} report about {topic}?",
    "For {company}, summarise the record on {topic}.",
    "{company}: what was recorded about {topic}?",
    "Within {company}, how was {topic} handled?",
)
CROSS_TEMPLATES = (
    "Compare {company_a} and {company_b} on {topic}.",
    "How do {company_a} and {company_b} differ on {topic}?",
    "Between {company_b} and {company_a}, what does each record about {topic}?",
    "What do {company_a} and {company_b} each report for {topic}?",
)
UNSCOPED_TEMPLATES = (
    "What is recorded about {topic}?",
    "Summarise the record on {topic}.",
    "What was reported for {topic}?",
    "Which documents cover {topic}?",
)
SHARED_TEMPLATES = (
    "In {company}, what does the shared checklist for {topic} require?",
    "In {company}, what does the record say about {topic} in the shared checklist?",
)
UNANSWERABLE_TEMPLATES = (
    "In {company}, what outcome was recorded under {code}?",
    "For {company}, what does the record say about {code}?",
    "In {company}, what was decided under reference {code}?",
)
ALIAS_TEMPLATES = (
    "What does {alias} report about {topic}?",
    "In {alias}, how was {topic} handled?",
    "{alias}: what is recorded about {topic}?",
)
NEAR_MISS_TEMPLATES = (
    "What did the {word} report about {topic}?",
    "Did the {word} record anything about {topic}?",
    "Is there anything in the {word} records about {topic}?",
)
UNKNOWN_ORG_TEMPLATES = (
    "What does {other_org} record about {topic}?",
    "In {other_org}, how was {topic} handled?",
)
PUNCT_TEMPLATES = (
    "What did {upper} report about {topic}?",
    "What does the {dashed} record about {topic}?",
    "What is {possessive} position on {topic}?",
)
# Ordinary words a company might be named after. These rows expect NO scope when written lowercase.
NEAR_MISS_WORDS = ("target", "pilot", "market", "record")
UNKNOWN_ORGS = ("Zenith Logistics", "Harbour Analytics", "Pinnacle Freight")


class ProbeError(Exception):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} problem(s); refusing to write probes")
        self.problems = list(problems)


def _rot(items: Sequence[str], index: int) -> str:
    return items[index % len(items)]


def _cycle(items: Sequence[dict[str, Any]], index: int) -> dict[str, Any]:
    """Same rotation for record lists; empty in, empty out (a degenerate corpus must not crash)."""
    return items[index % len(items)] if items else {}


def _slug(company: str) -> str:
    return re.sub(r"[^a-z0-9]", "", company.lower())[:6]


def _alias_for(company: str) -> str:
    """The short form the operator would type: the first word, lower-cased."""
    return company.split()[0].lower()


def load_corpus(root: Path, company: str) -> dict[str, Any]:
    manifest = root / company / "manifest.json"
    if not manifest.is_file():
        raise ProbeError([f"corpus manifest not found: {manifest}"])
    return json.loads(manifest.read_text(encoding="utf-8"))


def fact_texts(corpus: dict[str, Any]) -> list[str]:
    return [str(fact.get("text", "")) for fact in corpus.get("facts", [])]


def shared_document(corpus: dict[str, Any]) -> str | None:
    for entry in corpus.get("files", []):
        if SHARED_FILE_HINT in str(entry.get("path", "")):
            return str(entry["path"])
    return None


def topic_labels(corpus: dict[str, Any], shared: bool) -> list[str]:
    """Topic labels, shared ones first when a shared document is being asked about."""
    labels = [str(topic.get("label", "")) for topic in corpus.get("topics", [])]
    labels = [label for label in labels if label]
    if shared:
        # A shared document can only be asked about through a topic the corpora share, otherwise the
        # two companies' copies would support different answers.
        labels = [label for label in labels if label in shared_topic_labels(corpus)]
    return labels


def shared_topic_labels(corpus: dict[str, Any]) -> set[str]:
    return {str(topic.get("label", "")) for topic in corpus.get("topics", [])
            if str(topic.get("scope", "")) == "shared"}


def _code_map(corpora: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {company: str(manifest.get("company_code", "")) for company, manifest in corpora.items()}


def build_rows(company: str, corpus: dict[str, Any], corpora: dict[str, dict[str, Any]],
               aliases: dict[str, list[str]]) -> list[dict[str, Any]]:
    code = str(corpus.get("company_code", ""))
    slug = _slug(company)
    others = [name for name in corpora if name != company]
    shared_doc = shared_document(corpus)
    topics = topic_labels(corpus, shared=False)
    shared_topics = [label for label in topic_labels(corpus, shared=True)][:12]
    rows: list[dict[str, Any]] = []

    def add(shape: str, index: int, question: str, expected: list[str], **extra: Any) -> None:
        row = {
            "id": f"{ID_PREFIX}-{slug}-{shape[:4]}{index:02d}",
            "shape": shape,
            "question": question,
            "expected_companies": sorted(expected),
            "expected_mode": ("unscoped" if not expected
                              else "single" if len(expected) == 1 else "multi"),
            "run_group": "upload_door" if shape == "upload_door" else "main",
        }
        row.update(extra)
        rows.append(row)

    scoped = corpus.get("probes", {}).get("scoped", [])
    for index in range(SHAPE_COUNTS["scoped"]):
        source = _cycle(scoped, index)
        topic = _topic_of(corpus, str(source.get("expected_fact_code", ""))) or _rot(topics, index)
        add("scoped", index + 1,
            _rot(SCOPED_TEMPLATES, index).format(company=company, topic=topic), [company],
            expected_fact_code=source.get("expected_fact_code"),
            expected_document=source.get("expected_document"), expected_outcome="answer")

    for index in range(SHAPE_COUNTS["cross_company"]):
        topic = _rot(shared_topics or topics, index)
        pair = others[0] if others else company
        add("cross_company", index + 1,
            _rot(CROSS_TEMPLATES, index).format(company_a=company, company_b=pair, topic=topic),
            sorted([company, pair]), expected_outcome="answer")

    for index in range(SHAPE_COUNTS["unscoped"]):
        topic = _rot(topics, index + 3)
        add("unscoped", index + 1, _rot(UNSCOPED_TEMPLATES, index).format(topic=topic), [])

    unanswerable = corpus.get("probes", {}).get("unanswerable", [])
    for index in range(SHAPE_COUNTS["unanswerable"]):
        source = _cycle(unanswerable, index)
        add("unanswerable", index + 1,
            _rot(UNANSWERABLE_TEMPLATES, index).format(company=company,
                                                       code=source.get("fact_code", "")),
            [company], expected_fact_code=source.get("fact_code"), expected_outcome="refusal")

    for index in range(SHAPE_COUNTS["shared_file"]):
        topic = _rot(shared_topics or topics, index)
        add("shared_file", index + 1,
            _rot(SHARED_TEMPLATES, index).format(company=company, topic=topic), [company],
            expected_document=shared_doc, expected_shared_document=shared_doc,
            expected_outcome="answer")

    for index in range(SHAPE_COUNTS["alias"]):
        alias = _alias_for(company)
        topic = _rot(topics, index + 7)
        add("alias", index + 1, _rot(ALIAS_TEMPLATES, index).format(alias=alias, topic=topic),
            [company], expected_alias=alias, expected_outcome="answer")

    for index in range(SHAPE_COUNTS["near_miss_word"]):
        word = NEAR_MISS_WORDS[index % len(NEAR_MISS_WORDS)]
        topic = _rot(topics, index + 11)
        add("near_miss_word", index + 1,
            _rot(NEAR_MISS_TEMPLATES, index).format(word=word, topic=topic), [],
            expected_outcome="answer_unscoped")

    for index in range(SHAPE_COUNTS["unknown_org"]):
        other_org = UNKNOWN_ORGS[index % len(UNKNOWN_ORGS)]
        topic = _rot(topics, index + 5)
        add("unknown_org", index + 1,
            _rot(UNKNOWN_ORG_TEMPLATES, index).format(other_org=other_org, topic=topic), [],
            expect_candidate=other_org, expected_outcome="answer_unscoped")

    transforms = {
        "upper": company.upper(),
        "dashed": company.lower().replace(" ", "-"),
        "possessive": f"{company}'s",
    }
    for index in range(SHAPE_COUNTS["punctuation_case"]):
        topic = _rot(topics, index + 9)
        template = _rot(PUNCT_TEMPLATES, index)
        add("punctuation_case", index + 1, template.format(topic=topic, **transforms), [company],
            expected_outcome="answer")

    for index in range(SHAPE_COUNTS["upload_door"]):
        source = _cycle(scoped, index + 4)
        topic = _topic_of(corpus, str(source.get("expected_fact_code", ""))) or _rot(topics, index + 2)
        add("upload_door", index + 1,
            _rot(SCOPED_TEMPLATES, index + 1).format(company=company, topic=topic), [company],
            expected_fact_code=source.get("expected_fact_code"), expected_outcome="answer_or_refusal")

    return rows


def _topic_of(corpus: dict[str, Any], fact_code: str) -> str | None:
    """The topic label of the topic a planted code belongs to (labels, never sentences)."""
    for fact in corpus.get("facts", []):
        if str(fact.get("code", "")) == fact_code:
            topic_id = str(fact.get("topic", ""))
            for topic in corpus.get("topics", []):
                if str(topic.get("id", topic_id)) == topic_id:
                    return str(topic.get("label", "")) or None
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--corpora-root", default=str(DEFAULT_CORPORA_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--companies", nargs="*", default=list(DEFAULT_COMPANIES))
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    corpora_root = Path(args.corpora_root).resolve()
    out = Path(args.out).resolve()
    if REPO_ROOT in out.parents or out == REPO_ROOT:
        print(f"refusing to write probes inside the repo: {out}", file=sys.stderr)
        return 2
    try:
        corpora = {company: load_corpus(corpora_root, company) for company in args.companies}
    except ProbeError as exc:
        for problem in exc.problems:
            print(f"probe problem: {problem}", file=sys.stderr)
        print(f"refusing to write probes: {len(exc.problems)} problem(s)", file=sys.stderr)
        return 2
    codes = _code_map(corpora)
    aliases = {company: [_alias_for(company)] for company in args.companies}
    rows = {company: build_rows(company, corpora[company], corpora, aliases)
            for company in args.companies}

    problems = validate(rows, corpora, codes, aliases, args.companies)
    if problems:
        for problem in problems[:40]:
            print(f"probe problem: {problem}", file=sys.stderr)
        print(f"refusing to write: {len(problems)} problem(s)", file=sys.stderr)
        return 2

    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for company, company_rows in rows.items():
        buckets: dict[str, list[dict[str, Any]]] = {key: [] for key in SHAPE_KEYS}
        for row in company_rows:
            buckets[row["shape"]].append(row)
        manifest = {
            "format_version": 1,
            "generator": "scripts/make_gl10_probes.py",
            "company": company,
            "company_code": codes[company],
            "company_codes": codes,
            "seed": corpora[company].get("seed"),
            "source_manifest": str(corpora_root / company / "manifest.json"),
            "aliases": aliases,
            "known_companies": sorted(args.companies),
            "probes": buckets,
            "counts": {
                "rows": len(company_rows),
                "by_shape": {key: len(value) for key, value in sorted(buckets.items())},
                "by_run_group": _run_groups(company_rows),
                "by_expected_mode": _modes(company_rows),
            },
            "safety": "Invented data from scripts/make_synthetic_company.py. Not derived from any "
                      "real document. Safe to stage for a test index.",
        }
        path = out / company / "manifest.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        written.append(path)

    if not args.quiet:
        for path in written:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            print(f"{path} sha256={digest}")
        totals: dict[str, int] = {}
        for company_rows in rows.values():
            for row in company_rows:
                totals[row["shape"]] = totals.get(row["shape"], 0) + 1
        print("rows total:", sum(len(value) for value in rows.values()))
        for shape, count in sorted(totals.items()):
            print(f"  {shape}: {count}")
    return 0


def _run_groups(company_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in company_rows:
        counts[row["run_group"]] = counts.get(row["run_group"], 0) + 1
    return dict(sorted(counts.items()))


def _modes(company_rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in company_rows:
        counts[row["expected_mode"]] = counts.get(row["expected_mode"], 0) + 1
    return dict(sorted(counts.items()))


def validate(rows: dict[str, list[dict[str, Any]]], corpora: dict[str, dict[str, Any]],
             codes: dict[str, str], aliases: dict[str, list[str]],
             companies: Sequence[str]) -> list[str]:
    """Every check runs before a byte is written. Problems are counts and ids, never question text."""
    problems: list[str] = []
    seen_ids: set[str] = set()
    answer_word_runs = _forbidden_word_runs(corpora)
    known = list(companies)

    for company, company_rows in rows.items():
        fact_codes = {str(fact.get("code", "")) for fact in corpora[company].get("facts", [])}
        na_codes = {str(fact.get("code", ""))
                    for fact in corpora[company].get("unanswerable_facts", [])}
        other_codes = {code for name, manifest in corpora.items() if name != company
                       for code in {str(fact.get("code", ""))
                                    for fact in manifest.get("facts", [])}}
        documents = {str(entry.get("path", "")) for entry in corpora[company].get("files", [])}

        for row in company_rows:
            row_id = row["id"]
            if row_id in seen_ids:
                problems.append(f"{row_id}: duplicate id")
            seen_ids.add(row_id)
            if GL13_ID_RE.match(row_id):
                problems.append(f"{row_id}: id collides with the GL-13 id shape")
            if row["shape"] not in SHAPE_KEYS:
                problems.append(f"{row_id}: unknown shape {row['shape']}")

            expected = row["expected_companies"]
            detected = detect_companies(row["question"], known, aliases=aliases).companies
            if sorted(detected) != sorted(expected):
                problems.append(f"{row_id}: question scopes to {sorted(detected)}, "
                                f"expected {sorted(expected)}")

            code = row.get("expected_fact_code")
            if code:
                if row["shape"] == "unanswerable":
                    if code not in na_codes:
                        problems.append(f"{row_id}: unanswerable code {code} is not an "
                                        f"unanswerable fact")
                    if code in fact_codes or code in other_codes:
                        problems.append(f"{row_id}: unanswerable code {code} exists in a document")
                elif code not in fact_codes:
                    problems.append(f"{row_id}: expected code {code} is not a fact of {company}")

            for key in ("expected_document", "expected_shared_document"):
                document = row.get(key)
                if document and document not in documents:
                    problems.append(f"{row_id}: {key} {document} is not a file in the corpus")

            if row["shape"] == "alias":
                alias = str(row.get("expected_alias", ""))
                question = row["question"].lower()
                named_full = [name for name in known if name.lower() in question]
                if not alias or alias not in question or named_full:
                    problems.append(f"{row_id}: an alias row must reach the company only through "
                                    f"the alias, not its full name")

            if row["shape"] in ("near_miss_word", "unknown_org") and expected:
                problems.append(f"{row_id}: {row['shape']} rows must expect no company")

            tokens = [token for token in re.split(r"\W+", row["question"].lower()) if token]
            for window in _windows(tokens, MIN_ANSWER_TOKEN_RUN):
                if window in answer_word_runs:
                    problems.append(f"{row_id}: question carries {MIN_ANSWER_TOKEN_RUN} consecutive "
                                    f"words from a document sentence")
                    break
    return problems


def _windows(tokens: Sequence[str], size: int) -> list[str]:
    return [" ".join(tokens[start:start + size]) for start in range(0, max(0, len(tokens) - size + 1))]


def _forbidden_word_runs(corpora: dict[str, dict[str, Any]]) -> set[str]:
    """Runs of MIN_ANSWER_TOKEN_RUN consecutive words out of any fact sentence, minus the topic
    vocabulary.

    A probe has to name the topic it asks about, and a topic label is a name, not an answer: the runs
    that come from a label are subtracted. What stays is a run that only makes sense inside a written
    sentence, which is what a question must not contain.
    """
    forbidden: set[str] = set()
    for manifest in corpora.values():
        for text in fact_texts(manifest):
            tokens = [token for token in re.split(r"\W+", str(text).lower()) if token]
            forbidden.update(_windows(tokens, MIN_ANSWER_TOKEN_RUN))
    allowed: set[str] = set()
    for manifest in corpora.values():
        for topic in manifest.get("topics", []):
            tokens = [token for token in re.split(r"\W+", str(topic.get("label", "")).lower())
                      if token]
            allowed.update(_windows(tokens, MIN_ANSWER_TOKEN_RUN))
    return forbidden - allowed


if __name__ == "__main__":
    raise SystemExit(main())
