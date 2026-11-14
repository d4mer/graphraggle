"""Generate identifier-bearing ANSWERABLE probes from a generated company manifest.

Why this exists
---------------
The M1 gate (`app/grounding.py`, flag `GROUNDING_CHECK_ASKED_IDENTIFIER`) acts on an identifier
named in the QUESTION. On the GL-08 stack2 run `would_newly_refuse_answerable` was 0, but that 0
was structural: every answerable question in the generated corpora names no code at all, so the
gate reported `no_identifier` and could not have refused anything. Enforcement can only wrongly
refuse a good answer on an answerable question that DOES name an identifier. This script builds
exactly those probes, so that the guard can be measured instead of assumed.

What it writes (outside the repo, not committed)
    /home/maverick/work/synthetic-co-probes/<Company>/manifest.json        probe manifest
    /home/maverick/work/synthetic-co-probes/<Company>/edge-variants.json    expected-gate side file
    /home/maverick/work/synthetic-co-probes/RUN-NOTES.md                   how to run + what to expect

The manifest is in the shape `scripts/score_grounding_run.py` and the Hermes runner already
consume: `probes` with exactly the buckets `scoped` / `cross_company` / `unanswerable`, each row
carrying `question` plus the same expected-answer fields `make_synthetic_company.py` uses
(`expected_company`, `expected_document`, `expected_fact_id`, `expected_fact_code`), plus
`expected_answer` (the planted sentence) and `id`. The scorer ignores fields it does not know,
which is what makes the extra labelling safe to put in the rows themselves.

Rows, per company
1. One scoped probe per planted fact code, phrased over several templates (code at start, middle,
   end, quoted, with a company mention) so punctuation and position edge cases are exercised.
2. Edge variants on a deterministic ~20% subset: lowercase code, one hyphen replaced by a space,
   trailing comma. These are the interesting ones: they are answerable questions whose evidence
   holds the canonical code, and some of them make the gate fire anyway.
3. A few two-code questions: both codes present, and one present / one absent.
4. The company's existing unanswerable lookalike rows, copied unchanged, as controls - so one run
   reports the catch rate and the guard together.

Expected gate verdict is DERIVED, not hand-asserted
--------------------------------------------------
Every row gets `expected_gate`: `not_fire`, `fire`, or `no_identifier`, computed by running the
gate's own rule (`app.grounding.identifiers` + `verbatim_support`, on the question after
`sanitize_like_lightrag`) against the pieces the repo chunker cuts from the document that holds
the code. So a row labelled `fire` is a fire the gate's code actually produces, and the list of
them is a prediction the run can be scored against rather than an opinion. The assumption is
stated in the output: it presumes retrieval returns a piece of that document. `expected_gate`
therefore predicts grounding behaviour, not retrieval behaviour, and a run that disagrees is a
retrieval finding rather than a scorer bug.

Validation is loud, by design: a code that is not planted, a code that does not appear verbatim in
its document, a code the chunker loses inside a TEXT-route document, a question that contains its
own expected answer, a duplicated id, or output that the repo's own scorer refuses to load all
stop the run with a message. Office-route documents (.pdf/.docx/.pptx) are extracted here by the
same crude extractor the GL-08 harness uses, because LightRAG extracts those bytes itself; a code
that survives here but not upstream is reported as a warning, not a hard failure.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import grounding, structure_chunker          # noqa: E402
from app.lightrag_text import sanitize_like_lightrag  # noqa: E402

DEFAULT_CORPORA_ROOT = Path("/home/maverick/work/synthetic-co")
DEFAULT_OUTPUT_ROOT = Path("/home/maverick/work/synthetic-co-probes")
DEFAULT_COMPANIES = ("Northwind Chemicals", "Contoso Pharma")
ID_PREFIX = "idn"
# GL-13 probe ids look like this (scripts/probe_set_check.py ID_RE). Probe ids here must not
# collide with that shape, so the GL-07-derived set stays obviously separate.
GL13_ID_RE = re.compile(r"^[a-z]{2,4}\d{2,3}$")

# Phrasing variety, deterministic by row index. Each template names the code exactly once and
# never contains a planted sentence.
TEMPLATES: tuple[str, ...] = (
    "What outcome was recorded under {code}?",
    "{code}: what outcome was recorded for it?",
    "For entry {code}, what was the recorded outcome?",
    "Which record quotes the entry \"{code}\"?",
    "{code} - state the recorded outcome for this entry.",
    "In {company}, what does the record say about {code}?",
    "What did {company} record for code {code}?",
)
EDGE_VARIANTS: tuple[str, ...] = ("lowercase_code", "hyphen_as_space", "trailing_comma")
CONTROL_BUCKETS = ("scoped", "cross_company", "unanswerable")


class ProbeError(Exception):
    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} problem(s); refusing to write probes")
        self.problems = list(problems)


# ── the corpus side: text and pieces, the way the repo's tooling cuts them ───

def _load_harness_extractor():
    """Reuse the GL-08 harness's extractor rather than writing a second one. It handles the
    office formats the generator writes (its .docx/.pptx are zipped XML, its .pdf draws text with
    `(...) Tj`), which is the closest in-repo stand-in for LightRAG's own extraction."""
    path = REPO_ROOT / "scripts" / "unanswerable_grounding.py"
    spec = importlib.util.spec_from_file_location("unanswerable_grounding_for_probes", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_harness = _load_harness_extractor()


def document_text(path: Path) -> str:
    return _harness.plain_text_of(path)


def document_pieces(text: str) -> tuple[str, ...]:
    """Pieces as app.structure_chunker cuts them, rendered for LightRAG. The gate reads evidence
    pieces, not the raw file, so this is the level the probes must be validated at."""
    plan = structure_chunker.chunk_document(text)
    return tuple(piece.render() for piece in plan.pieces)


def expected_gate(question: str, evidence_pieces: Sequence[str]) -> tuple[str, list[str], list[str]]:
    """What M1 would decide for this question IF retrieval returned these pieces.

    Same rule as the gateway: sanitise the question the way LightRAG sanitises stored text, take
    identifier tokens, require each to appear as a standalone token in some piece.
    Returns (verdict, asked, unsupported) with verdict in {not_fire, fire, no_identifier}.
    """
    asked = grounding.identifiers(sanitize_like_lightrag(question))
    unsupported = [code for code in asked
                   if not any(grounding.verbatim_support(code, piece) for piece in evidence_pieces)]
    if not asked:
        return "no_identifier", asked, unsupported
    return ("fire" if unsupported else "not_fire"), asked, unsupported


# ── the corpus side: loading a company ───────────────────────────────────────

def load_company(company_dir: Path, problems: list[str]) -> dict[str, Any] | None:
    manifest_path = company_dir / "manifest.json"
    if not manifest_path.exists():
        problems.append(f"{manifest_path}: company manifest not found "
                        "(this script reads the generated corpora; it does not generate them)")
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for field in ("company", "company_code"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            problems.append(f"{manifest_path}: missing '{field}'")
    facts = manifest.get("facts")
    if not isinstance(facts, list) or not facts:
        problems.append(f"{manifest_path}: no 'facts' list; identifier probes need planted codes")
    probes = manifest.get("probes")
    if not isinstance(probes, dict) or not isinstance(probes.get("unanswerable"), list):
        problems.append(f"{manifest_path}: no probes.unanswerable rows to carry over as controls")
        return None
    company = manifest["company"]
    documents: dict[str, dict[str, Any]] = {}
    for fact in facts or []:
        rel = fact.get("document")
        if not isinstance(rel, str):
            problems.append(f"{manifest_path}: a fact row has no 'document'")
            continue
        if rel not in documents:
            path = company_dir / rel
            if not path.exists():
                problems.append(f"{manifest_path}: fact document '{rel}' is not in {company_dir}")
                documents[rel] = {"path": path, "text": "", "pieces": (), "office": False}
                continue
            text = document_text(path)
            documents[rel] = {"path": path, "text": text, "pieces": document_pieces(text),
                              "office": path.suffix.lower() in {".pdf", ".docx", ".pptx"}}
    return {"company": company, "company_dir": company_dir, "manifest": manifest,
            "manifest_path": manifest_path, "documents": documents}


# ── probe rows ───────────────────────────────────────────────────────────────

def _variant_code(code: str, variant: str, index: int) -> str:
    if variant == "lowercase_code":
        return code.lower()
    if variant == "trailing_comma":
        return f"{code},"
    # hyphen_as_space: which hyphen to open is deterministic and cycles, so the subset of the
    # code that survives the gate's token regex differs row by row.
    positions = [i for i, ch in enumerate(code) if ch == "-"]
    if not positions:
        return code
    target = positions[index % len(positions)]
    return code[:target] + " " + code[target + 1:]


def _row(company_record: dict[str, Any], probe_id: str, question: str, fact: dict[str, Any] | None,
         probe_kind: str, evidence: Sequence[str], shape: str, **extra: Any) -> dict[str, Any]:
    verdict, asked, unsupported = expected_gate(question, evidence)
    row: dict[str, Any] = {
        "id": probe_id,
        "question": question,
        "shape": shape,
        "probe_kind": probe_kind,
        "expected_gate": verdict,
        "expected_gate_asked": asked,
        "expected_unsupported": unsupported,
    }
    if fact is not None:
        row.update({
            "expected_company": company_record["company"],
            "expected_document": fact["document"],
            "expected_fact_id": fact["fact_id"],
            "expected_fact_code": fact["code"],
            "expected_answer": fact["text"],
        })
    row.update(extra)
    return row


def build_company_rows(company_record: dict[str, Any], *, edge_rate: float,
                       two_code_count: int, problems: list[str]) -> list[dict[str, Any]]:
    manifest = company_record["manifest"]
    company = company_record["company"]
    documents = company_record["documents"]
    company_code = manifest["company_code"]
    facts = sorted(manifest["facts"], key=lambda fact: (fact["code"], fact["fact_id"]))
    code_tag = re.sub(r"[^a-z0-9]+", "", company_code.lower())
    rows: list[dict[str, Any]] = []

    def pieces_of(fact: dict[str, Any]) -> tuple[str, ...]:
        return documents[fact["document"]]["pieces"]

    every = max(1, round(1.0 / edge_rate)) if edge_rate > 0 else 0
    edge_ids: list[str] = []
    for index, fact in enumerate(facts):
        code = fact["code"]
        probe_id = f"{ID_PREFIX}-{code_tag}-s{index + 1:03d}"
        template = TEMPLATES[index % len(TEMPLATES)]
        question = template.format(code=code, company=company)
        rows.append(_row(company_record, probe_id, question, fact, "identifier_answerable",
                         pieces_of(fact), "scoped"))
        if every and index % every == 0:
            variant = EDGE_VARIANTS[(index // every) % len(EDGE_VARIANTS)]
            variant_question = template.format(code=_variant_code(code, variant, index // every),
                                               company=company)
            edge_id = f"{ID_PREFIX}-{code_tag}-e{index + 1:03d}"
            rows.append(_row(company_record, edge_id, variant_question, fact, f"edge_{variant}",
                             pieces_of(fact), "scoped",
                             edge_variant=variant, edge_base_id=probe_id,
                             edge_expected_fire=(variant_question != question)))
            edge_ids.append(edge_id)

    # Two-code rows. Both-present pairs come from one document, which is the kindest case for
    # retrieval; one-absent pairs use a planted code and an unanswerable lookalike.
    unanswerable_codes = [entry.get("fact_code") for entry in manifest["probes"]["unanswerable"]
                          if isinstance(entry.get("fact_code"), str)]
    pairs = []
    for rel, record in sorted(documents.items()):
        same_document = [fact for fact in facts if fact["document"] == rel]
        for first, second in zip(same_document, same_document[1:]):
            pairs.append((first, second))
            if len(pairs) >= max(0, two_code_count // 2):
                break
        if len(pairs) >= max(0, two_code_count // 2):
            break
    for pair_index, (first, second) in enumerate(pairs):
        probe_id = f"{ID_PREFIX}-{code_tag}-t{pair_index + 1:03d}"
        question = (f"Both {first['code']} and {second['code']} came up in the review - what "
                    "outcome was recorded for each?")
        rows.append(_row(company_record, probe_id, question, None, "two_code_both_present",
                         tuple(pieces_of(first)) + tuple(pieces_of(second)), "scoped",
                         codes_in_question=[first["code"], second["code"]],
                         codes_planted=[first["code"], second["code"]],
                         expected_documents=[first["document"], second["document"]]))
    for pair_index in range(max(0, two_code_count - len(pairs))):
        if not (unanswerable_codes and facts):
            problems.append(f"{company}: no unanswerable lookalike codes available for a "
                            "one-present/one-absent row")
            break
        present = facts[pair_index % len(facts)]
        absent = unanswerable_codes[pair_index % len(unanswerable_codes)]
        probe_id = f"{ID_PREFIX}-{code_tag}-x{pair_index + 1:03d}"
        question = (f"{present['code']} and {absent} were both raised - what outcome was recorded "
                    "for each?")
        rows.append(_row(company_record, probe_id, question, None, "two_code_one_absent",
                         pieces_of(present), "scoped",
                         codes_in_question=[present["code"], absent],
                         codes_planted=[present["code"]],
                         expected_documents=[present["document"]]))

    # Controls: the company's existing unanswerable rows, fields unchanged, plus id and kind.
    for control_index, entry in enumerate(manifest["probes"]["unanswerable"]):
        all_pieces = tuple(piece for record in documents.values() for piece in record["pieces"])
        row = dict(entry)
        row.update(_row(company_record, f"{ID_PREFIX}-{code_tag}-u{control_index + 1:02d}",
                        entry["question"], None, "control_unanswerable", all_pieces,
                        "unanswerable"))
        row["question"] = entry["question"]      # keep the control's text byte-identical
        rows.append(row)
    return rows


# ── validation ───────────────────────────────────────────────────────────────

def validate(company_record: dict[str, Any], rows: Sequence[dict[str, Any]],
             problems: list[str], warnings: list[str]) -> None:
    manifest = company_record["manifest"]
    documents = company_record["documents"]
    planted = {fact["code"] for fact in manifest["facts"] if isinstance(fact.get("code"), str)}
    answers = {fact["text"] for fact in manifest["facts"] if isinstance(fact.get("text"), str)}

    for row in rows:
        where = f"{company_record['company']} {row['id']}"
        code = row.get("expected_fact_code")
        if code is not None and code not in planted:
            problems.append(f"{where}: expected_fact_code {code} is not a planted fact code in "
                            "the manifest")
        # Edge rows alter the code on purpose; two-code rows carry their codes in codes_in_question.
        if not row["probe_kind"].startswith(("edge_", "two_code")) and code is not None \
                and code not in row["question"]:
            problems.append(f"{where}: the canonical code does not appear verbatim in the "
                            "question, so this row would not exercise the gate")
        document = row.get("expected_document")
        if document:
            record = documents[document]
            if code is not None and code not in record["text"]:
                problems.append(f"{where}: code {code} is not verbatim in {document}")
            supported = any(grounding.verbatim_support(code, piece)
                            for piece in record["pieces"]) if code else False
            if code is not None and not supported:
                message = (f"{where}: code {code} is not a standalone token in any chunk piece of "
                           f"{document}")
                if record["office"]:
                    warnings.append(message + " - office route: LightRAG extracts these bytes "
                                    "itself, so this is the harness extractor's view, not upstream's")
                else:
                    problems.append(message)
        for answer in answers:
            if answer and answer in row["question"]:
                problems.append(f"{where}: the question contains a planted answer sentence, which "
                                "would let a probe answer itself")
        if row["id"] in GL13_ID_RE.findall(row["id"]) or not row["id"].startswith(f"{ID_PREFIX}-"):
            problems.append(f"{where}: id is not namespaced with '{ID_PREFIX}-' (it could collide "
                            "with a GL-13 probe id)")
        if row["expected_gate"] not in ("fire", "not_fire", "no_identifier"):
            problems.append(f"{where}: unexpected expected_gate {row['expected_gate']!r}")

    control_rows = [row for row in rows if row["probe_kind"] == "control_unanswerable"]
    if not control_rows:
        problems.append(f"{company_record['company']}: no unanswerable control rows carried over")
    for row in control_rows:
        if row["expected_gate"] != "fire":
            problems.append(f"{company_record['company']} {row['id']}: an unanswerable control is "
                            f"not expected to fire ({row['expected_gate']}); the corpus contains "
                            "its code, so it is not a refusal probe")
    ids = [row["id"] for row in rows]
    duplicates = sorted({probe_id for probe_id in ids if ids.count(probe_id) > 1})
    if duplicates:
        problems.append(f"{company_record['company']}: duplicate probe ids {duplicates}")


def load_scorer():
    """The scorer is the consumer of this output, so its own loader is the contract test."""
    path = REPO_ROOT / "scripts" / "score_grounding_run.py"
    spec = importlib.util.spec_from_file_location("score_grounding_run_for_generator", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def check_scorer_loads(output_paths: Sequence[Path], problems: list[str]) -> None:
    scorer = load_scorer()
    for path in output_paths:
        own: list[str] = []
        probes = scorer.load_manifests([path], own)
        for problem in own:
            problems.append(f"the scorer rejects {path}: {problem}")
        if not probes:
            problems.append(f"the scorer loaded 0 probes from {path}")


# ── writing ──────────────────────────────────────────────────────────────────

def write_company_output(company_record: dict[str, Any], rows: Sequence[dict[str, Any]],
                         out_dir: Path) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {bucket: [] for bucket in CONTROL_BUCKETS}
    for row in rows:
        buckets[row["shape"]].append(row)
    manifest = company_record["manifest"]
    document = {
        "format_version": 1,
        "generator": "scripts/make_identifier_probes.py",
        "source_manifest": str(company_record["manifest_path"]),
        "company": company_record["company"],
        "company_code": manifest["company_code"],
        "safety": manifest.get("safety", "Invented data. Safe to stage for a test index."),
        "expected_gate_assumes": (
            "retrieval returns a piece of the document named in expected_document; expected_gate "
            "predicts the grounding rule, not retrieval coverage"
        ),
        "counts": {
            "rows": len(rows),
            "by_probe_kind": {kind: sum(1 for row in rows if row["probe_kind"] == kind)
                              for kind in sorted({row["probe_kind"] for row in rows})},
            "by_expected_gate": {verdict: sum(1 for row in rows if row["expected_gate"] == verdict)
                                 for verdict in ("fire", "not_fire", "no_identifier")},
        },
        "probes": buckets,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(document, indent=2, sort_keys=True) + "\n",
                                           encoding="utf-8")
    side = {
        "company": company_record["company"],
        "expected_fire_ids": sorted(row["id"] for row in rows if row["expected_gate"] == "fire"),
        "expected_no_identifier_ids": sorted(row["id"] for row in rows
                                             if row["expected_gate"] == "no_identifier"),
        "by_probe_kind": {
            kind: {"ids": sorted(row["id"] for row in rows if row["probe_kind"] == kind),
                   "expected_gate": sorted({row["expected_gate"] for row in rows
                                            if row["probe_kind"] == kind})}
            for kind in sorted({row["probe_kind"] for row in rows})
        },
    }
    (out_dir / "edge-variants.json").write_text(json.dumps(side, indent=2, sort_keys=True) + "\n",
                                                encoding="utf-8")
    return document


RUN_NOTES = """# Identifier-bearing probe set (M1 gate guard test)

Generated by `scripts/make_identifier_probes.py`. Invented data only.

Run it against the gateway with the flag on, then score it:

```bash
# on the stack, for the window of the run:
#   GROUNDING_CHECK_ASKED_IDENTIFIER=true  QUERY_LOG_PATH=/path/query.log
python scripts/score_grounding_run.py \\
  --manifest "{out}/Northwind Chemicals/manifest.json" \\
  --manifest "{out}/Contoso Pharma/manifest.json" \\
  --results <the runner's *.results.jsonl> --log <that query-log capture>
```

The runner must record `meta.request_id` and `company` per row, or the scorer will refuse to join
repeated questions.

Every row carries `expected_gate` (`fire` / `not_fire` / `no_identifier`), derived from the gate's
own rule against the chunk pieces of the document that holds the code. `edge-variants.json` lists
the ids per expectation, so a run's disagreement can be split into:

- expected `fire` and fired: by design (the unanswerable controls, and the one-absent two-code rows)
- expected `fire` but did not fire, or expected `not_fire` and fired: the run disagrees with the
  gate's rule as measured offline - usually a retrieval finding (the piece never came back), since
  `expected_gate` presumes it did
- expected `no_identifier`: the gate cannot see the identifier at all, so it protects nothing there

Known ways the gate fires on a question that IS answerable, all of them generated here on purpose:

1. `hyphen_as_space` - one hyphen opened to a space. The question's token no longer equals the
   canonical code, and the code is not a standalone token in the evidence, so the gate fires. This
   is what a transcript line-wrap, an ASR transcript, or a human typing from memory produces.
2. Two-code questions - EVERY asked code must be supported by the returned evidence union. One
   retrieved chunk short, and a fully answerable question fires.
3. `lowercase_code` - the identifier regex requires an uppercase first character, so the question
   asks about nothing as far as the gate is concerned. No false refusal, and no protection either.

Acceptance rule for the run

A probe set row that fires but is NOT in `edge-variants.json:expected_fire_ids` is a genuine false
refusal. That is the number to hold enforcement to, not the raw
`would_newly_refuse_answerable`, which is expected to be non-zero on this set by design.

With the default settings, per company (39 planted codes): 39 identifier probes + 8 edge variants
+ 4 two-code rows + 6 unanswerable controls = 57 rows, and the offline expectation is
`fire` on 11 of them (6 controls + 2 one-present/one-absent + 3 `hyphen_as_space`),
`no_identifier` on 3 (`lowercase_code`), `not_fire` on 43. Both companies at the default seed
produce the same shape, so a run that disagrees is a retrieval or extraction finding.
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate identifier-bearing answerable probes from a generated company "
                    "manifest (invented data; writes outside the repo).")
    parser.add_argument("--corpora-root", default=str(DEFAULT_CORPORA_ROOT), metavar="PATH")
    parser.add_argument("--company", action="append", default=[], metavar="NAME",
                        help=f"repeatable; default {', '.join(DEFAULT_COMPANIES)}")
    parser.add_argument("--out", default=str(DEFAULT_OUTPUT_ROOT), metavar="PATH")
    parser.add_argument("--edge-rate", type=float, default=0.2,
                        help="fraction of fact probes that also get an edge variant (default 0.2)")
    parser.add_argument("--two-code-count", type=int, default=4,
                        help="two-code rows per company, split both-present / one-absent (default 4)")
    parser.add_argument("--dry-run", action="store_true", help="validate and count, write nothing")
    parser.add_argument("--json", action="store_true", help="machine-readable summary")
    args = parser.parse_args(argv)

    corpora_root = Path(args.corpora_root)
    # A dry run writes into a throwaway directory rather than skipping the writes, so the
    # validation and the scorer-loads-its-own-output check run on exactly the bytes a real run
    # would produce.
    effective_out = Path(tempfile.mkdtemp(prefix="grg-idprobes-")) if args.dry_run else Path(args.out)
    companies = args.company or list(DEFAULT_COMPANIES)
    problems: list[str] = []
    warnings: list[str] = []
    written: list[Path] = []
    summary: dict[str, Any] = {"output_root": str(args.out), "companies": {}}

    for name in companies:
        company_record = load_company(corpora_root / name, problems)
        if company_record is None:
            continue
        rows = build_company_rows(company_record, edge_rate=args.edge_rate,
                                  two_code_count=args.two_code_count, problems=problems)
        validate(company_record, rows, problems, warnings)
        out_dir = effective_out / company_record["company"]
        document = write_company_output(company_record, rows, out_dir)
        written.append(out_dir / "manifest.json")
        summary["companies"][company_record["company"]] = {
            "rows": document["counts"]["rows"],
            "by_probe_kind": document["counts"]["by_probe_kind"],
            "by_expected_gate": document["counts"]["by_expected_gate"],
            "expected_fire_ids": sorted(row["id"] for row in rows if row["expected_gate"] == "fire"),
            "expected_no_identifier_ids": sorted(row["id"] for row in rows
                                                 if row["expected_gate"] == "no_identifier"),
        }
        if not args.dry_run:
            notes = effective_out / "RUN-NOTES.md"
            notes.parent.mkdir(parents=True, exist_ok=True)
            notes.write_text(RUN_NOTES.format(out=args.out), encoding="utf-8")

    if written:
        check_scorer_loads(written, problems)
    if problems:
        print("CANNOT WRITE PROBES:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 2
    if warnings:
        for warning in warnings:
            print(f"warning: {warning}", file=sys.stderr)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        for company, counts in sorted(summary["companies"].items()):
            print(f"{company}: {counts['rows']} probes")
            for kind, count in sorted(counts["by_probe_kind"].items()):
                print(f"    {kind}: {count}")
            print(f"    expected fire: {counts['by_expected_gate']['fire']}, "
                  f"not_fire: {counts['by_expected_gate']['not_fire']}, "
                  f"no_identifier: {counts['by_expected_gate']['no_identifier']}")
            print(f"    expected-fire ids: {len(counts['expected_fire_ids'])}")
        print(f"written under {args.out}"
              + (" (dry run: validated in a throwaway directory, nothing written there)"
                 if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
