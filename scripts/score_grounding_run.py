"""Score a labelled probe run against the M1 asked-identifier gate — COUNTS ONLY.

M1 (`app/grounding.py`, `GROUNDING_CHECK_ASKED_IDENTIFIER`) is flag-only. Whether to
ENFORCE it is a numbers decision, and the numbers this script produces are the decision:

    would_newly_refuse_answerable   <- the guard. Target 0. Non-zero means the gate costs
                                       real answers, and no amount of caught hallucinations
                                       makes that a free win.
    hallucinations_caught / _missed <- what enforcement would buy.

Input (a) one or more generated-company probe manifests — the `manifest.json` written by
`scripts/make_synthetic_company.py`, whose `probes` block is {scoped, cross_company,
unanswerable}, and (b) a run export `*.results.jsonl` with one labelled row per probe
attempt, plus optionally (c) the gateway's query-log lines (`QUERY_LOG_PATH`, one JSON
object per line as `app/observability._emit` writes them) carrying the `grounding_*` fields.

Output is counts only. No question text, no answers, no reference text, no file paths, and
not even the identifiers the questions named — those come from the corpus and this report is
meant to be pasteable into a ticket. Row and problem references are indices and counts.

Every assumption about the runner's field NAMES is declared in FIELD_ALIASES below and never
inferred from content: if a row cannot be read under the declared names, the script fails
loudly with the row index and the key names it saw (names, not values). Field names can be
aliased per run with `--map <theirs>=<ours>` without editing code, so a real export
that uses other names is a one-line fix rather than a rewrite.

The join key is chosen up front and reported:
    request_id            preferred. The gateway puts the same id in the response
                          (`meta.request_id`) and in the query log, so a runner that records
                          it gets an unambiguous join even with repeats.
    question+company      fallback. Requires the export to carry a company per row, and
                          fails loudly if a key matches more than one log line (the 55-row
                          GL-08 run has repeats, so repeats without request ids are exactly
                          the case that must not be guessed).

Exit codes: 0 scored, 2 could not score (bad join, unreadable row, unknown label, flag was
off in that run). A failed join prints no headline counts, so a half-joined run can never
produce a go/no-go number.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Sequence

# ── the declared contract ────────────────────────────────────────────────────

# Manifest side: fixed by scripts/make_synthetic_company.py (in-repo, so not a guess).
MANIFEST_PROBE_BUCKETS = {"scoped": "scoped", "cross_company": "cross-company",
                          "unanswerable": "unanswerable"}
ANSWERABLE_SHAPES = ("scoped", "cross-company")
UNANSWERABLE_SHAPE = "unanswerable"

# Run-export side: ASSUMED, because the runner (Hermes scratch/gl08/probe_baseline.py) is not
# in this repo and no real export was on disk when this was written. Each entry is the set of
# names tried, in order. Nothing is inferred from values.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "question": ("question", "query", "prompt"),
    "label": ("label", "verdict", "grade", "judgement", "judgment"),
    "company": ("company", "company_scope", "scope"),
    "request_id": ("request_id", "requestid", "trace_id", "correlation_id"),
    "draw": ("draw", "attempt", "repeat", "iteration"),
}

# Label vocabulary. Known values come from the GL-08 run record (correct / partial / timeout /
# refused / hallucinated) and from scripts/overnight_loop.py VERDICT_VALUES. Anything else is
# reported as an unknown label rather than absorbed into a bucket.
LABEL_ROLES: dict[str, str] = {
    "correct": "accepted",
    "partial": "accepted",
    "wrong": "accepted",
    "refused": "refused",
    "refused_ok": "refused",
    "refused_wrong": "refused",
    "hallucinated": "hallucinated",
    "timeout": "not_an_answer",
    "error": "not_an_answer",
}
HALLUCINATION_LABELS = tuple(l for l, r in LABEL_ROLES.items() if r == "hallucinated")
REFUSAL_LABELS = tuple(l for l, r in LABEL_ROLES.items() if r == "refused")

# Gateway log fields, exactly as app/observability.emit_query_log projects them.
LOG_GROUNDING_FIELDS = ("grounding_checked", "grounding_verdict", "grounding_asked",
                        "grounding_unsupported", "grounding_evidence_pass",
                        "grounding_references_without_content", "grounding_supported",
                        "grounding_asked_unsupported")


class ScoringError(Exception):
    """Raised with every problem found, so one run reports all of them."""

    def __init__(self, problems: Sequence[str]) -> None:
        super().__init__(f"{len(problems)} problem(s); cannot score this run")
        self.problems = list(problems)


# ── loading ──────────────────────────────────────────────────────────────────

def read_jsonl(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    """Strict JSONL: one JSON object per line. Blank lines are skipped; anything else is a
    problem naming the line number and the parse error, never the content."""
    rows: list[dict[str, Any]] = []
    if not path.exists():
        problems.append(f"{path}: file not found")
        return rows
    for number, raw in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            problems.append(f"{path} line {number}: not JSON ({exc.msg} at column {exc.colno}); "
                            "a docker-log or prefixed capture must be unwrapped first")
            continue
        if not isinstance(value, dict):
            problems.append(f"{path} line {number}: JSON {type(value).__name__}, expected object")
            continue
        rows.append(value)
    return rows


def resolve(row: dict[str, Any], field: str) -> Any:
    """Return the value for a logical field under its declared aliases, or None if absent.

    Absence is not itself a problem: the caller decides whether it is fatal and reports the
    aliases it tried (see resolve_required).
    """
    for name in FIELD_ALIASES[field]:
        if name in row:
            return row[name]
    return None


def resolve_required(row: dict[str, Any], field: str, problems: list[str], where: str) -> Any:
    value = resolve(row, field)
    if value is None:
        problems.append(
            f"{where}: no '{field}' field. Declared aliases tried: "
            f"{', '.join(FIELD_ALIASES[field])}. Keys present in this row: "
            f"{', '.join(sorted(str(k) for k in row))}")
    return value


def load_manifests(paths: Sequence[Path], problems: list[str]) -> dict[tuple[str, str], dict[str, Any]]:
    """Probe identity = (company, question text). Generated manifests carry no probe id, so
    the question text is the only identity they offer — which is why a run export that also
    carries the company per row is strongly preferred."""
    probes: dict[tuple[str, str], dict[str, Any]] = {}
    for path in paths:
        if not path.exists():
            problems.append(f"{path}: manifest not found")
            continue
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(f"{path}: manifest is not valid JSON ({exc.msg})")
            continue
        company = manifest.get("company")
        if not isinstance(company, str) or not company:
            problems.append(f"{path}: manifest has no 'company' string")
            continue
        block = manifest.get("probes")
        if not isinstance(block, dict):
            problems.append(f"{path}: manifest has no 'probes' object "
                            "(expected keys: scoped, cross_company, unanswerable)")
            continue
        unknown = sorted(set(block) - set(MANIFEST_PROBE_BUCKETS))
        if unknown:
            problems.append(f"{path}: unrecognised probe shape key(s) {unknown}; "
                            f"recognised: {sorted(MANIFEST_PROBE_BUCKETS)}")
        for bucket, canonical in MANIFEST_PROBE_BUCKETS.items():
            rows = block.get(bucket, [])
            if not isinstance(rows, list):
                problems.append(f"{path}: probes.{bucket} is not a list")
                continue
            for index, row in enumerate(rows):
                where = f"{path} probes.{bucket}[{index}]"
                if not isinstance(row, dict):
                    problems.append(f"{where}: not an object")
                    continue
                question = row.get("question")
                if not isinstance(question, str) or not question.strip():
                    problems.append(f"{where}: no 'question' string")
                    continue
                declared = row.get("shape")
                if declared != canonical:
                    problems.append(f"{where}: shape {declared!r} does not match its "
                                    f"'{bucket}' bucket (expected {canonical!r})")
                    continue
                key = (company, question)
                existing = probes.get(key)
                if existing is not None:
                    # The generated manifests legitimately repeat question text: scoped probes are
                    # built per FACT, and several facts share one topic label, so 14 scoped probes
                    # can be 7 distinct questions. That is the whole reason the 55-row GL-08 run is
                    # 41 distinct questions, so repeating text is counted, not rejected. A repeat
                    # that crosses shapes is a contradiction and fails.
                    if existing["shape"] != canonical:
                        problems.append(f"{path} probes.{bucket}[{index}]: the same question text "
                                        f"also appears as shape {existing['shape']!r} in company "
                                        f"{existing['company']!r}; one text cannot be two shapes")
                        continue
                    existing["probe_entries"] += 1
                    continue
                probes[key] = {"company": company, "question": question, "shape": canonical,
                               "probe_entries": 1}
    total_entries = sum(probe["probe_entries"] for probe in probes.values())
    for probe in probes.values():
        probe["total_entries"] = total_entries
    return probes


def load_export(path: Path, problems: list[str]) -> list[dict[str, Any]]:
    rows = []
    for index, row in enumerate(read_jsonl(path, problems), 1):
        where = f"{path} row {index}"
        question = resolve_required(row, "question", problems, where)
        label = resolve_required(row, "label", problems, where)
        if isinstance(label, str):
            label = label.strip().lower()
            if label not in LABEL_ROLES:
                problems.append(f"{where}: unrecognised label {label!r}. Known labels: "
                                f"{sorted(LABEL_ROLES)}. Fix the mapping or extend LABEL_ROLES; "
                                "do not guess a bucket.")
        elif label is not None:
            problems.append(f"{where}: label is {type(label).__name__}, expected a string")
        request_id = resolve(row, "request_id")
        company = resolve(row, "company")
        draw = resolve(row, "draw")
        rows.append({"where": where, "index": index,
                     "question": question if isinstance(question, str) else None,
                     "label": label if isinstance(label, str) else None,
                     "company": company if isinstance(company, str) else None,
                     "request_id": request_id if isinstance(request_id, str) else None,
                     "draw": draw})
    return rows


def load_logs(paths: Sequence[Path], problems: list[str]) -> list[dict[str, Any]]:
    lines: list[dict[str, Any]] = []
    for path in paths:
        for index, row in enumerate(read_jsonl(path, problems), 1):
            where = f"{path} line {index}"
            if "grounding" in row and not any(f in row for f in LOG_GROUNDING_FIELDS):
                problems.append(f"{where}: carries a 'grounding' object, i.e. the query_scope "
                                "form, not the query-log projection. Feed the QUERY_LOG_PATH "
                                "lines (app/observability.emit_query_log).")
                continue
            request_id = row.get("request_id")
            if not isinstance(request_id, str) or not request_id:
                problems.append(f"{where}: query-log line without 'request_id'")
                continue
            if "grounding_checked" not in row:
                problems.append(f"{where}: no 'grounding_checked' field, so this line predates "
                                "the M1 fields or was written by an older gateway")
            lines.append({"where": where, "request_id": request_id, "log": row})
    return lines


# ── the gate decision, from the log projection ───────────────────────────────

GATE_STATES = ("fired", "not_fired", "no_identifier", "not_checked")


def gate_state(log_row: dict[str, Any] | None, basis: Counter) -> str:
    """Would enforcement have refused this row? Plus how confidently we know.

    `fired` means the identifier the QUESTION named is absent from the evidence the answer was
    written from, which is the only thing M1 enforcement is allowed to act on.
    """
    if log_row is None:
        basis["no_log_line"] += 1
        return "not_checked"
    log = log_row["log"]
    if log.get("grounding_checked") is not True:
        basis["flag_off_in_this_run"] += 1
        return "not_checked"

    asked_unsupported = log.get("grounding_asked_unsupported")
    if isinstance(asked_unsupported, list):
        basis["exact_asked_unsupported"] += 1
        if asked_unsupported:
            return "fired"
        return "not_fired" if log.get("grounding_asked") else "no_identifier"

    supported = log.get("grounding_supported")
    if supported is False:
        basis["log_grounding_supported"] += 1
        return "fired"
    if supported is True:
        basis["log_grounding_supported"] += 1
        return "not_fired"
    if supported is None and "grounding_supported" in log:
        basis["log_grounding_supported"] += 1
        return "no_identifier"

    # Older gateway builds logged only grounding_asked / grounding_unsupported. `unsupported`
    # there is asked-or-asserted, so this is CONSERVATIVE: a row whose answer merely asserted an
    # extra code is counted as fired. That can only overstate the guard, never hide it.
    asked = log.get("grounding_asked") or []
    unsupported = log.get("grounding_unsupported") or []
    if not isinstance(asked, list) or not isinstance(unsupported, list):
        basis["malformed_grounding_fields"] += 1
        return "not_checked"
    basis["derived_conservative"] += 1
    if not asked:
        return "no_identifier"
    return "fired" if unsupported else "not_fired"


# ── joining ──────────────────────────────────────────────────────────────────

def choose_log_join(export_rows: Sequence[dict[str, Any]], log_lines: Sequence[dict[str, Any]],
                    problems: list[str]) -> str | None:
    if not log_lines:
        return None
    if all(row["request_id"] for row in export_rows):
        seen: dict[str, int] = Counter()
        for line in log_lines:
            seen[line["request_id"]] += 1
        duplicated = sum(1 for count in seen.values() if count > 1)
        if duplicated:
            problems.append(f"query log: {duplicated} request_id value(s) appear on more than "
                            "one line; one retrieval pass should log one line")
        return "request_id"
    if all(row["question"] is not None for row in export_rows):
        # The fallback is only usable when the log can be keyed the same way, and repeats make
        # it unusable. Say so rather than picking one at random.
        keyed = Counter((line["log"].get("query"), line["log"].get("company"))
                        for line in log_lines)
        repeats = sum(1 for count in keyed.values() if count > 1)
        if repeats:
            problems.append(f"cannot join on question+company: {repeats} query/company key(s) "
                            "match more than one log line. This run has repeats; record "
                            "meta.request_id in the export and join on that.")
            return None
        return "question+company"
    problems.append("cannot choose a join key: export rows carry no request_id and no question. "
                    "Record meta.request_id from the gateway response.")
    return None


def join_to_log(export_rows: Sequence[dict[str, Any]], log_lines: Sequence[dict[str, Any]],
                key: str | None, problems: list[str]) -> list[dict[str, Any] | None]:
    if key is None:
        return [None] * len(export_rows)
    by_key: dict[Any, list[dict[str, Any]]] = defaultdict(list)
    for line in log_lines:
        if key == "request_id":
            by_key[line["request_id"]].append(line)
        else:
            by_key[(line["log"].get("query"), line["log"].get("company"))].append(line)
    joined: list[dict[str, Any] | None] = []
    for row in export_rows:
        lookup = row["request_id"] if key == "request_id" else (row["question"], row["company"])
        matches = by_key.get(lookup, [])
        if len(matches) > 1:
            problems.append(f"{row['where']}: {len(matches)} log lines match this row")
            joined.append(None)
        elif not matches:
            problems.append(f"{row['where']}: no query-log line matches. The gateway logs one "
                            "line per /query, so this probe either bypassed the gateway or its "
                            "request_id was not recorded")
            joined.append(None)
        else:
            joined.append(matches[0])
    return joined


def join_to_manifest(export_rows: Sequence[dict[str, Any]],
                     probes: dict[tuple[str, str], dict[str, Any]],
                     problems: list[str],
                     matched: set[tuple[str, str]] | None = None
                     ) -> list[dict[str, Any] | None]:
    by_question: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for probe in probes.values():
        by_question[probe["question"]].append(probe)
    out: list[dict[str, Any] | None] = []
    for row in export_rows:
        if row["question"] is None:
            out.append(None)
            continue
        if row["company"] is not None:
            probe = probes.get((row["company"], row["question"]))
            if probe is not None and matched is not None:
                matched.add((row["company"], row["question"]))
            if probe is None:
                problems.append(f"{row['where']}: company+question matches no probe in the "
                                "manifests (unknown company, or the question text drifted from "
                                "the manifest it was run from)")
            out.append(probe)
            continue
        candidates = by_question[row["question"]]
        if len(candidates) == 1:
            if matched is not None:
                matched.add((candidates[0]["company"], candidates[0]["question"]))
            out.append(candidates[0])
        else:
            problems.append(f"{row['where']}: question matches {len(candidates)} manifest probes "
                            "in different companies and this row carries no company. Add the "
                            "company (or run each company's manifest separately); not guessing.")
            out.append(None)
    return out


# ── scoring ──────────────────────────────────────────────────────────────────

def score(export_rows: Sequence[dict[str, Any]], probes: Sequence[dict[str, Any] | None],
          log_rows: Sequence[dict[str, Any] | None]) -> dict[str, Any]:
    basis: Counter = Counter()
    shapes: Counter = Counter()
    labels: dict[str, Counter] = defaultdict(Counter)
    unanswerable_tab: Counter = Counter()      # (gate_state, label_role)
    answerable_tab: Counter = Counter()        # gate_state
    answerable_refused_by_label: Counter = Counter()
    headline = {"would_newly_refuse_answerable": 0, "hallucinations_caught": 0,
                "hallucinations_missed": 0, "references_without_content": 0}
    per_question: dict[str, dict[str, set]] = defaultdict(lambda: {"shapes": set(), "gate": set(),
                                                                   "labels": set()})
    question_pairs: set[tuple[str, str]] = set()
    rows_without_probe = 0
    rows_missing_reference_count = 0

    for row, probe, log_row in zip(export_rows, probes, log_rows):
        if probe is None:
            rows_without_probe += 1
            continue
        shape = probe["shape"]
        shapes[shape] += 1
        label = row["label"] or "unlabelled"
        role = LABEL_ROLES.get(label, "unlabelled")
        labels[shape][label] += 1
        state = gate_state(log_row, basis)
        per_question[row["question"]]["shapes"].add(shape)
        per_question[row["question"]]["gate"].add(state)
        per_question[row["question"]]["labels"].add(label)
        question_pairs.add((probe["company"], row["question"]))

        if log_row is not None:
            value = log_row["log"].get("grounding_references_without_content")
            if isinstance(value, int):
                headline["references_without_content"] += value
            else:
                rows_missing_reference_count += 1

        if shape == UNANSWERABLE_SHAPE:
            unanswerable_tab[(state, role)] += 1
            if role == "hallucinated":
                if state == "fired":
                    headline["hallucinations_caught"] += 1
                else:
                    headline["hallucinations_missed"] += 1
        else:
            answerable_tab[state] += 1
            if state == "fired":
                headline["would_newly_refuse_answerable"] += 1
                answerable_refused_by_label[label] += 1

    distinct_mixed = sum(1 for entry in per_question.values() if len(entry["gate"]) > 1
                         or len(entry["shapes"]) > 1 or len(entry["labels"]) > 1)
    return {
        "rows_scored": sum(shapes.values()),
        "rows_without_manifest_probe": rows_without_probe,
        "rows_by_shape": dict(shapes),
        "labels_by_shape": {shape: dict(counts) for shape, counts in labels.items()},
        "unanswerable_gate_x_label": {f"{state}|{role}": count
                                      for (state, role), count in sorted(unanswerable_tab.items())},
        "answerable_gate": dict(answerable_tab),
        "answerable_newly_refused_by_label": dict(answerable_refused_by_label),
        "headline": headline,
        "rows_missing_references_without_content_field": rows_missing_reference_count,
        "denominators": {
            "rows": len(export_rows),
            "rows_scored": sum(shapes.values()),
            "distinct_question_texts": len(per_question),
            "distinct_company_question_pairs": len(question_pairs),
            "distinct_questions_with_disagreement_between_rows": distinct_mixed,
        },
        "gate_decision_basis": dict(basis),
    }


# ── reporting ────────────────────────────────────────────────────────────────

def render(result: dict[str, Any], *, join_key: str | None, log_given: bool,
           json_output: bool) -> str:
    if json_output:
        payload = dict(result)
        payload["join_key"] = join_key
        payload["query_log_given"] = log_given
        return json.dumps(payload, indent=2, sort_keys=True)

    lines = ["M1 asked-identifier gate: run scores (COUNTS ONLY)",
             f"join key: {join_key or 'none (could not join)'}",
             f"query log supplied: {log_given}", ""]
    lines.append("HEADLINE")
    for key in ("would_newly_refuse_answerable", "hallucinations_caught",
                "hallucinations_missed", "references_without_content"):
        lines.append(f"  {key}: {result['headline'][key]}")
    lines.append("  (would_newly_refuse_answerable is the guard; it is reported even at 0)")

    lines.append("")
    lines.append("ROWS BY SHAPE")
    for shape, count in sorted(result["rows_by_shape"].items()):
        lines.append(f"  {shape}: {count}")
    if result["rows_without_manifest_probe"]:
        lines.append(f"  rows that matched no manifest probe: "
                     f"{result['rows_without_manifest_probe']}")
    lines.append(f"  manifest probe entries: {result['manifest_probe_entries']}; distinct "
                 f"company+question keys: {result['manifest_probe_keys']} "
                 f"({result['manifest_probe_entries_repeating_a_text']} entries repeat a question "
                 "text, which is normal in generated manifests); keys with no export row: "
                 f"{result['manifest_probe_keys_without_export_row']}")

    lines.append("")
    lines.append("LABELS AS ALREADY LABELLED IN THE EXPORT (per shape)")
    for shape, counts in sorted(result["labels_by_shape"].items()):
        rendered = ", ".join(f"{label}={count}" for label, count in sorted(counts.items()))
        lines.append(f"  {shape}: {rendered}")

    lines.append("")
    lines.append("UNANSWERABLE: gate state x label role")
    for cell, count in sorted(result["unanswerable_gate_x_label"].items()):
        lines.append(f"  {cell}: {count}")
    lines.append("  (fired = the asked identifier was absent from the evidence, so enforcement "
                 "would have refused; not_fired / no_identifier / not_checked = it would not have)")

    lines.append("")
    lines.append("ANSWERABLE (scoped + cross-company): gate state")
    for state in GATE_STATES:
        if state in result["answerable_gate"]:
            lines.append(f"  {state}: {result['answerable_gate'][state]}")
    if result["answerable_newly_refused_by_label"]:
        rendered = ", ".join(f"{label}={count}"
                             for label, count in sorted(result["answerable_newly_refused_by_label"].items()))
        lines.append(f"  newly refused, by label that would have been lost: {rendered}")

    lines.append("")
    lines.append("DENOMINATORS (say which one a rate uses)")
    for key, value in result["denominators"].items():
        lines.append(f"  {key}: {value}")
    lines.append("  rates above are per ROW unless stated; a distinct question whose rows "
                 "disagree is counted, not averaged")

    lines.append("")
    lines.append("HOW THE GATE DECISION WAS READ")
    for basis, count in sorted(result["gate_decision_basis"].items()):
        lines.append(f"  {basis}: {count}")
    if result["gate_decision_basis"].get("derived_conservative"):
        lines.append("  WARNING: some rows were scored from grounding_asked/grounding_unsupported "
                     "only, which cannot tell an unsupported ASKED code from an unsupported code "
                     "the answer added. That overstates would_newly_refuse_answerable. Re-run "
                     "with a gateway that logs grounding_supported / grounding_asked_unsupported.")
    if result["gate_decision_basis"].get("flag_off_in_this_run"):
        lines.append("  WARNING: some log lines have grounding_checked false — the flag was off "
                     "for those queries, so this run cannot answer the enforcement question.")
    if result["rows_missing_references_without_content_field"]:
        lines.append(f"  note: {result['rows_missing_references_without_content_field']} joined "
                     "line(s) carried no grounding_references_without_content value")
    return "\n".join(lines)


def apply_map(specs: Sequence[str], problems: list[str]) -> None:
    """`--map export:<theirs>=<ours>` adds a runner's field name to the declared aliases."""
    for spec in specs:
        if "=" not in spec:
            problems.append(f"--map {spec!r}: expected <theirs>=<ours>")
            continue
        theirs, ours = spec.split("=", 1)
        theirs, ours = theirs.strip(), ours.strip()
        if ours not in FIELD_ALIASES:
            problems.append(f"--map {spec!r}: '{ours}' is not a logical field "
                            f"({sorted(FIELD_ALIASES)}). Log-side names are fixed by "
                            "app/observability.emit_query_log and are not mappable.")
            continue
        if theirs not in FIELD_ALIASES[ours]:
            FIELD_ALIASES[ours] = (theirs,) + FIELD_ALIASES[ours]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Score a labelled probe run against the M1 asked-identifier gate. "
                    "Counts only: no question text, answers, identifiers or reference text.")
    parser.add_argument("--manifest", action="append", default=[], metavar="PATH",
                        help="generated company manifest.json (repeatable, one per company)")
    parser.add_argument("--results", required=True, metavar="PATH",
                        help="the runner's per-probe results export (*.results.jsonl)")
    parser.add_argument("--log", action="append", default=[], metavar="PATH",
                        help="gateway query-log lines from QUERY_LOG_PATH (repeatable)")
    parser.add_argument("--map", action="append", default=[], metavar="THEIRS=OURS",
                        help="alias a runner field name onto a logical field (repeatable)")
    parser.add_argument("--allow-unjoined", type=int, default=0, metavar="N",
                        help="tolerate up to N export rows with no log line (default 0)")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    problems: list[str] = []
    apply_map(args.map, problems)
    probes = load_manifests([Path(p) for p in args.manifest], problems)
    export_rows = load_export(Path(args.results), problems)
    log_lines = load_logs([Path(p) for p in args.log], problems)

    if not probes:
        problems.append("no usable manifest probes; without shapes there are no denominators")
    if not export_rows:
        problems.append("no usable export rows")

    join_key = choose_log_join(export_rows, log_lines, problems) if export_rows else None
    log_rows = join_to_log(export_rows, log_lines, join_key, problems)
    matched_probes: set[tuple[str, str]] = set()
    probe_rows = join_to_manifest(export_rows, probes, problems, matched_probes)
    if log_lines and not join_key:
        problems.append("a query log was supplied but no join key could be established")
    unjoined = sum(1 for row in log_rows if row is None)
    if log_lines and unjoined > args.allow_unjoined:
        problems.append(f"{unjoined} export row(s) could not be joined to the query log "
                        f"(tolerated: {args.allow_unjoined})")
    if not log_lines:
        problems.append("no query-log lines supplied: the gate verdicts cannot be read, so the "
                        "go/no-go question cannot be answered from this input. Point --log at "
                        "QUERY_LOG_PATH captured during the run, or at a docker-log capture of "
                        "the gateway for that window.")

    if problems:
        print("CANNOT SCORE — fix these before reading any number:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(f"\n{len(problems)} problem(s). No headline counts are printed on purpose.",
              file=sys.stderr)
        return 2

    result = score(export_rows, probe_rows, log_rows)
    # Coverage, not a failure: a run may legitimately be a subset of the manifest. It is printed
    # so nobody reads "6 of 12 refused" without knowing 12 ran.
    result["manifest_probe_entries"] = sum(probe["probe_entries"] for probe in probes.values())
    result["manifest_probe_keys"] = len(probes)
    result["manifest_probe_entries_repeating_a_text"] = (
        result["manifest_probe_entries"] - result["manifest_probe_keys"])
    result["manifest_probe_keys_without_export_row"] = len(set(probes) - matched_probes)
    print(render(result, join_key=join_key, log_given=bool(log_lines),
                 json_output=args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
