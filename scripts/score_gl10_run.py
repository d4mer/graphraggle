#!/usr/bin/env python3
"""Score a GL-10 run (the company named in the question) — COUNTS ONLY.

Sibling of `scripts/score_grounding_run.py`: same join-by-`request_id` rule, same refusal to guess,
same output discipline. It reads the three probe-set manifests (`scripts/make_gl10_probes.py`), the
runner's `*.results.jsonl`, the gateway's query-log captures for R0/R1/R2, and optionally the
judge's `*.verdicts.jsonl`, and prints the acceptance numbers for the run:

    other_company_citations                  target 0
    other_company_codes_in_scoped_answers    target 0
    cross_company_named_companies_cited      target >= 80%
    unscoped_rows_changed_vs_baseline        target 0
    scoped_correct / refused_ok              from the judge's verdicts, reported per run

Nothing printed is question text, answer text, or reference content. Row references are `gl10-…` ids,
which are invented probe labels from this repo's generator, so they are safe in a ticket.

Refusal rules, all exit 2 with no headline numbers, so a half-joined run can never produce a
go/no-go number:

- a results row with no probe id, or an id that is not in the probe sets;
- a run with results but no matching `--log` capture;
- any results row without `request_id` while the same probe question appears more than once (the
  GL-08 run has repeated questions, so repeats without ids are exactly the case that must not be
  guessed);
- a probe id appearing twice in the sets, or a `request_id` appearing twice in a log capture;
- a verdict for a row that was not joined.

Field names are declared in FIELD_ALIASES and never inferred from values; `--map theirs=ours` adds an
alias without editing code, because the runner lives on the Hermes side.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable, Sequence

PROBE_ID_RE = re.compile(r"^gl10-[a-z0-9]+-[a-z]{4}\d{2}$")

# Runner side. ASSUMED names, in order of preference, because the runner is Hermes-side scratch and
# its vocabulary changes between runs. Documented expectation from
# docs/briefs/hermes-solvay-probes.md: "Record: id, question, answer (full), latency_s,
# citations/sources if returned, error" plus, for GL-10, `request_id`.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "probe_id", "probe", "case_id"),
    "question": ("question", "query", "prompt"),
    "answer": ("answer", "answer_text", "response", "text"),
    "request_id": ("request_id", "requestid", "trace_id", "correlation_id"),
    "citations": ("citations", "sources", "references", "citation_list"),
    "citation_companies": ("citation_companies", "source_companies", "companies_cited"),
    "latency_s": ("latency_s", "latency", "duration_s", "elapsed_s"),
    "error": ("error", "error_message", "failure"),
}
VERDICT_ALIASES: dict[str, tuple[str, ...]] = {
    "id": ("id", "probe_id", "probe"),
    "verdict": ("verdict", "label", "grade", "judgement", "judgment"),
}
REFUSAL_VERDICTS = {"refused", "refused_ok"}
ANSWERED_VERDICTS = {"correct", "partial", "wrong", "hallucinated"}
UNANSWERABLE_SHAPE = "unanswerable"
# Shapes whose answer is expected to carry a code belonging to a company the question named.
# `unscoped` is deliberately absent: an unscoped answer may cite any company by design, and
# `unanswerable` is absent because its correct outcome is a refusal.
CODE_CHECKED_SHAPES = ("scoped", "shared_file", "upload_door", "alias", "punctuation_case",
                       "cross_company")
SHAPES_CITING_ALL = ("cross_company",)


class ScoreError(Exception):
    """A refusal to score. Carries the reason, never the content that caused it."""


def _pick(row: dict[str, Any], names: Sequence[str]) -> tuple[str | None, Any]:
    for name in names:
        if name in row and row[name] is not None:
            return name, row[name]
    return None, None


def load_jsonl(path: Path, what: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ScoreError(f"cannot read {what} {path}: {exc}")
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            raise ScoreError(f"{what} {path} line {number} is not a JSON object")
        if not isinstance(value, dict):
            raise ScoreError(f"{what} {path} line {number} is not an object")
        rows.append(value)
    return rows


def load_probe_sets(paths: Sequence[Path]) -> dict[str, dict[str, Any]]:
    probes: dict[str, dict[str, Any]] = {}
    company_codes: dict[str, str] = {}
    for path in paths:
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ScoreError(f"cannot read probe manifest {path}: {exc}")
        if not isinstance(manifest, dict) or "probes" not in manifest:
            raise ScoreError(f"probe manifest {path} has no `probes` block")
        company_codes.update({str(k): str(v) for k, v in (manifest.get("company_codes") or {}).items()})
        for shape, rows in (manifest.get("probes") or {}).items():
            for row in rows or []:
                row_id = str(row.get("id", ""))
                if not row_id:
                    raise ScoreError(f"probe manifest {path} shape {shape} has a row with no id")
                if row_id in probes:
                    raise ScoreError(f"probe id {row_id} appears in more than one manifest")
                merged = dict(row)
                merged.setdefault("shape", shape)
                probes[row_id] = merged
    if not probes:
        raise ScoreError("no probe rows found in the manifests given")
    return {"probes": probes, "company_codes": company_codes}


def join_rows(results: list[dict[str, Any]], log: list[dict[str, Any]],
              probes: dict[str, dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join results to log lines. request_id first; question text only when it is unique."""
    counts: dict[str, int] = {"results": len(results), "log_lines": len(log)}
    by_request: dict[str, dict[str, Any]] = {}
    for line in log:
        request_id = line.get("request_id")
        if request_id is None:
            continue
        if str(request_id) in by_request:
            raise ScoreError(f"request_id {request_id} appears twice in the log capture; the join "
                             "cannot be made without ambiguity")
        by_request[str(request_id)] = line

    questions_in_results = Counter()
    for row in results:
        _, question = _pick(row, FIELD_ALIASES["question"])
        if question is not None:
            questions_in_results[str(question)] += 1

    joined: list[dict[str, Any]] = []
    unmatched = 0
    for index, row in enumerate(results):
        _, probe_id = _pick(row, FIELD_ALIASES["id"])
        if probe_id is None:
            raise ScoreError(f"results row {index + 1} carries no probe id under any of "
                             f"{'/'.join(FIELD_ALIASES['id'])}")
        probe_id = str(probe_id)
        if probe_id not in probes:
            unmatched += 1
            continue
        _, request_id = _pick(row, FIELD_ALIASES["request_id"])
        log_line = None
        if request_id is not None:
            log_line = by_request.get(str(request_id))
        else:
            _, question = _pick(row, FIELD_ALIASES["question"])
            if question is None:
                raise ScoreError(f"results row for {probe_id} has no request_id and no question "
                                 "field to fall back on")
            if questions_in_results[str(question)] > 1:
                raise ScoreError(f"probe question for {probe_id} repeats in the export and the row "
                                 "carries no request_id; refusing to guess which attempt is which")
            matches = [line for line in log if line.get("query") == question]
            if len(matches) > 1:
                raise ScoreError(f"the log capture holds {len(matches)} lines for one repeated "
                                 "question and the export carries no request_id")
            log_line = matches[0] if matches else None
        if log_line is None:
            unmatched += 1
            continue
        joined.append({"probe_id": probe_id, "probe": probes[probe_id], "row": row,
                       "log": log_line})
    counts["joined"] = len(joined)
    counts["unmatched"] = unmatched
    if not joined:
        raise ScoreError("no results row joined a log line; the run cannot be scored")
    return joined, counts


def citation_companies(row: dict[str, Any], available: bool) -> list[str] | None:
    """Which companies the citations of one row belong to, as the gateway labelled them.

    Three shapes are tried, in order: the runner recorded companies directly; the runner recorded
    citation objects with a `company` field (what `scope_query_citations` enriches); or neither, in
    which case the honest answer is None and the report says the matrix is unavailable rather than
    inferring it from a path.
    """
    if not available:
        return None
    _, direct = _pick(row, FIELD_ALIASES["citation_companies"])
    if isinstance(direct, list):
        return [str(item) for item in direct if isinstance(item, str)]
    _, citations = _pick(row, FIELD_ALIASES["citations"])
    if isinstance(citations, list):
        found = [str(citation.get("company")) for citation in citations
                 if isinstance(citation, dict) and citation.get("company")]
        return found
    return None


def codes_by_company(answer: str, company_codes: dict[str, str]) -> dict[str, int]:
    """How many planted codes of each company appear in one answer (counts, not the codes)."""
    counts: dict[str, int] = {}
    for company, code in company_codes.items():
        if not code:
            continue
        counts[company] = len(re.findall(re.escape(code) + r"\b", answer))
    return counts


def score_run(name: str, joined: list[dict[str, Any]], probes_by_shape: dict[str, dict[str, Any]],
              company_codes: dict[str, str], verdicts: dict[str, str],
              baseline: dict[str, dict[str, Any]] | None) -> dict[str, Any]:
    report: dict[str, Any] = {"run": name, "join": joined["counts"]}
    rows = joined["rows"]
    by_shape: Counter[str] = Counter()
    by_detected_from: Counter[str] = Counter()
    by_detected_mode: Counter[str] = Counter()
    detection = Counter()
    matrix: Counter[tuple[str, str]] = Counter()
    matrix_available = False
    # Every guard starts at zero and stays in the report: an absent key would leave a reader
    # guessing whether the number was zero or the check never ran.
    code_guard = Counter({"other_company_citations": 0, "asked_company_code_present": 0,
                          "asked_company_code_absent": 0,
                          "other_company_codes_in_scoped_answers": 0,
                          "cross_company_all_named_cited": 0, "cross_company_missing_named": 0})
    scoped_sums: Counter[str] = Counter()
    declined: Counter[str] = Counter()
    retrieval_routes: Counter[str] = Counter()
    retrieval_fallbacks: Counter[str] = Counter()
    latency: list[float] = []
    unscoped_diff = 0
    citations_present = any(
        _pick(row["row"], FIELD_ALIASES["citations"])[1] is not None
        or _pick(row["row"], FIELD_ALIASES["citation_companies"])[1] is not None for row in rows)

    for entry in rows:
        probe = entry["probe"]
        log_line = entry["log"]
        shape = str(probe.get("shape", "unknown"))
        by_shape[shape] += 1
        detected = [str(item) for item in (log_line.get("companies_detected") or [])]
        by_detected_from[str(log_line.get("company_detected_from") or "absent")] += 1
        by_detected_mode["unscoped" if not detected else ("single" if len(detected) == 1
                                                          else "multi")] += 1
        expected = sorted(str(item) for item in (probe.get("expected_companies") or []))
        if sorted(detected) == expected:
            detection["correct"] += 1
        elif set(detected) > set(expected):
            detection["over_scoped"] += 1
        elif set(detected) < set(expected):
            detection["under_scoped"] += 1
        else:
            detection["wrong"] += 1

        companies = citation_companies(entry["row"], citations_present)
        if companies is not None:
            matrix_available = True
            asked = sorted(expected or detected)
            asked_labels = asked or ["(unscoped)"]
            for asked_company in asked_labels:
                for cited in (companies or ["(none cited)"]):
                    matrix[(asked_company, cited)] += 1
                if expected and any(cited not in expected for cited in companies):
                    code_guard["other_company_citations"] += 1

        _, answer_value = _pick(entry["row"], FIELD_ALIASES["answer"])
        answer = str(answer_value or "")
        counts = codes_by_company(answer, company_codes)
        asked = set(expected)
        if shape in CODE_CHECKED_SHAPES:
            if any(counts.get(company, 0) for company in asked):
                code_guard["asked_company_code_present"] += 1
            else:
                code_guard["asked_company_code_absent"] += 1
            if any(count for company, count in counts.items() if company not in asked):
                code_guard["other_company_codes_in_scoped_answers"] += 1
        if shape in SHAPES_CITING_ALL and companies is not None:
            cited_set = set(companies)
            if set(expected) <= cited_set:
                code_guard["cross_company_all_named_cited"] += 1
            else:
                code_guard["cross_company_missing_named"] += 1

        scoped_meta = log_line.get("scoped_pass") or {}
        if scoped_meta:
            scoped_sums["rows_with_scoped_pass"] += 1
            for key in ("references_kept", "references_dropped_other_company",
                        "references_unattributable", "references_dropped_by_context_cap",
                        "model_calls"):
                scoped_sums[key] += int(scoped_meta.get(key) or 0)
            if scoped_meta.get("second_model_call"):
                scoped_sums["second_model_calls"] += 1
            # Which retrieval route ran, and why it fell back. A scoped question should cost one
            # retrieval plus one generation; `model_calls` above is the number that proves it.
            if scoped_meta.get("retrieval_route"):
                retrieval_routes[str(scoped_meta["retrieval_route"])] += 1
            if scoped_meta.get("retrieval_fallback_reason"):
                retrieval_fallbacks[str(scoped_meta["retrieval_fallback_reason"])] += 1
            if scoped_meta.get("keyword_hint_applied"):
                scoped_sums["keyword_hint_applied"] += 1
            reason = scoped_meta.get("declined_reason")
            if reason:
                declined[str(reason)] += 1

        duration = log_line.get("duration_ms")
        if isinstance(duration, (int, float)):
            latency.append(float(duration))

        if shape == "unscoped" and baseline is not None:
            other = baseline.get(entry["probe_id"])
            if other is not None and _fingerprint(other) != _fingerprint(log_line):
                unscoped_diff += 1

    report["rows_by_shape"] = dict(sorted(by_shape.items()))
    report["rows_by_detected_from"] = dict(sorted(by_detected_from.items()))
    report["rows_by_detected_mode"] = dict(sorted(by_detected_mode.items()))
    report["detection_vs_expectation"] = dict(sorted(detection.items()))
    report["citations_by_company"] = ({"available": matrix_available,
                                       "matrix": {f"{a} -> {b}": count
                                                  for (a, b), count in sorted(matrix.items())}}
                                      if matrix_available else {"available": False})
    report["guards"] = dict(sorted(code_guard.items()))
    report["scoped_pass"] = {"rows": scoped_sums["rows_with_scoped_pass"],
                             **{key: scoped_sums[key] for key in
                                ("references_kept", "references_dropped_other_company",
                                 "references_unattributable", "references_dropped_by_context_cap",
                                 "second_model_calls", "model_calls", "keyword_hint_applied")},
                             "retrieval_routes": dict(sorted(retrieval_routes.items())),
                             "retrieval_fallbacks": dict(sorted(retrieval_fallbacks.items(),
                                                                key=lambda item: -item[1])),
                             "declined_reasons": dict(sorted(declined.items(),
                                                             key=lambda item: -item[1]))}
    report["latency_ms"] = _latency(latency)
    report["unscoped_rows_changed_vs_baseline"] = unscoped_diff

    if verdicts:
        verdict_counts: Counter[str] = Counter()
        unanswerable_total = 0
        unanswerable_refused = 0
        scoped_ok = 0
        scoped_total = 0
        for entry in rows:
            probe = entry["probe"]
            verdict = verdicts.get(entry["probe_id"])
            shape = str(probe.get("shape", "unknown"))
            if verdict is None:
                verdict_counts["no_verdict"] += 1
                continue
            verdict_counts[str(verdict)] += 1
            if shape == UNANSWERABLE_SHAPE:
                unanswerable_total += 1
                if str(verdict) in REFUSAL_VERDICTS:
                    unanswerable_refused += 1
            if shape in CODE_CHECKED_SHAPES:
                scoped_total += 1
                if str(verdict) in {"correct", "partial"}:
                    scoped_ok += 1
        report["verdicts"] = {"counts": dict(sorted(verdict_counts.items())),
                              "unanswerable_refused": f"{unanswerable_refused}/{unanswerable_total}"
                              if unanswerable_total else "0/0",
                              "answerable_correct_or_partial": f"{scoped_ok}/{scoped_total}"
                              if scoped_total else "0/0"}
    else:
        report["verdicts"] = {"available": False,
                              "note": "correct% and refusal% need the judge's verdicts file"}
    return report


def _fingerprint(log_line: dict[str, Any]) -> tuple[Any, ...]:
    """What 'unchanged' means for an unscoped row: same citation counts, same scope, same verdict.

    Deliberately excludes `company_scope`, which the flag adds by design, and anything content-shaped.
    """
    keys = ("citations_raw", "citations_merged", "citations_scoped", "citations_final",
            "mode_used", "rerank_applied", "grounding_verdict", "answer_chars", "status")
    return tuple(log_line.get(key) for key in keys)


def _latency(values: Iterable[float]) -> dict[str, Any]:
    data = sorted(values)
    if not data:
        return {"available": False}
    return {"median": int(round(statistics.median(data))),
            "p95": int(round(data[min(len(data) - 1, int(len(data) * 0.95))])),
            "max": int(round(data[-1])), "samples": len(data)}


def acceptance(reports: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for report in reports:
        run = report["run"]
        guards = report["guards"]
        lines.append(f"{run}: other_company_citations="
                     f"{guards.get('other_company_citations', 0)} (target 0) "
                     f"{'PASS' if guards.get('other_company_citations', 0) == 0 else 'FAIL'}")
        others = guards.get("other_company_codes_in_scoped_answers", 0)
        lines.append(f"{run}: other_company_codes_in_scoped_answers={others} (target 0) "
                     f"{'PASS' if others == 0 else 'FAIL'}")
        named = guards.get("cross_company_all_named_cited", 0)
        missing = guards.get("cross_company_missing_named", 0)
        total = named + missing
        if total:
            rate = named / total * 100
            lines.append(f"{run}: cross_company_named_companies_cited={named}/{total} "
                         f"({rate:.0f}%, target >= 80%) {'PASS' if rate >= 80 else 'FAIL'}")
        else:
            lines.append(f"{run}: cross_company_named_companies_cited=not computable "
                         "(the export carried no citation companies)")
        changed = report["unscoped_rows_changed_vs_baseline"]
        lines.append(f"{run}: unscoped_rows_changed_vs_baseline={changed} (target 0) "
                     f"{'PASS' if changed == 0 else 'FAIL'}")
        verdicts = report.get("verdicts") or {}
        if verdicts.get("available") is False:
            lines.append(f"{run}: scoped_correct=not computable (no verdicts file)")
        else:
            lines.append(f"{run}: scoped_correct={verdicts.get('answerable_correct_or_partial')} "
                         "refused_unanswerable="
                         f"{verdicts.get('unanswerable_refused')} (compare across runs)")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--probe-manifest", action="append", default=[],
                        help="gl10 probe-set manifest.json (repeat)")
    parser.add_argument("--run", action="append", default=[], metavar="NAME=RESULTS.jsonl",
                        help="a run to score (repeat: R0=… R1=… R2=…)")
    parser.add_argument("--log", action="append", default=[], metavar="NAME=QUERY-LOG.jsonl",
                        help="the query-log capture for that run (repeat, same NAME)")
    parser.add_argument("--verdicts", action="append", default=[],
                        metavar="NAME=RESULTS.verdicts.jsonl", help="the judge's verdicts (repeat)")
    parser.add_argument("--baseline", default=None, help="run name to compare unscoped rows against")
    parser.add_argument("--map", action="append", default=[], metavar="THEIRS=OURS",
                        help="add a field alias, e.g. --map trace_id=request_id")
    args = parser.parse_args(argv)

    for pair in args.map:
        theirs, _, ours = pair.partition("=")
        if ours not in FIELD_ALIASES:
            print(f"unknown field {ours}; known: {', '.join(sorted(FIELD_ALIASES))}",
                  file=sys.stderr)
            return 2
        FIELD_ALIASES[ours] = (theirs,) + FIELD_ALIASES[ours]

    try:
        if not args.probe_manifest:
            raise ScoreError("no --probe-manifest given")
        sets = load_probe_sets([Path(path) for path in args.probe_manifest])
        probes = sets["probes"]
        logs: dict[str, list[dict[str, Any]]] = {}
        for pair in args.log:
            name, _, path = pair.partition("=")
            logs[name] = load_jsonl(Path(path), "query log")
        if args.run and not logs:
            raise ScoreError("runs given but no --log capture: the scoping numbers come from the log")
        baseline_name = args.baseline or (args.run[0].split("=")[0] if args.run else None)
        joined_by_run: dict[str, dict[str, Any]] = {}
        verdict_files: dict[str, list[dict[str, Any]]] = {}
        for pair in args.verdicts:
            name, _, path = pair.partition("=")
            verdict_files[name] = load_jsonl(Path(path), "verdicts")
        for pair in args.run:
            name, _, path = pair.partition("=")
            results = load_jsonl(Path(path), "results")
            if name not in logs:
                raise ScoreError(f"run {name} has no --log capture; refusing to score it alone")
            rows, counts = join_rows(results, logs[name], probes)
            joined_by_run[name] = {"rows": rows, "counts": counts}
        if not joined_by_run:
            raise ScoreError("nothing to score: no --run given")

        baseline_rows: dict[str, dict[str, Any]] | None = None
        if baseline_name in joined_by_run:
            baseline_rows = {entry["probe_id"]: entry["log"]
                             for entry in joined_by_run[baseline_name]["rows"]}
        reports: list[dict[str, Any]] = []
        for pair in args.run:
            name = pair.split("=")[0]
            verdicts: dict[str, str] = {}
            for verdict_row in verdict_files.get(name, []):
                _, probe_id = _pick(verdict_row, VERDICT_ALIASES["id"])
                _, verdict = _pick(verdict_row, VERDICT_ALIASES["verdict"])
                if probe_id is None or verdict is None:
                    raise ScoreError(f"verdicts for run {name} contain a row without an id or a "
                                     "verdict")
                if str(probe_id) not in probes:
                    raise ScoreError(f"verdict row {probe_id} is not a probe in this run")
                verdicts[str(probe_id)] = str(verdict)
            reports.append(score_run(name, joined_by_run[name], probes, sets["company_codes"],
                                     verdicts, baseline_rows))
    except ScoreError as exc:
        print(f"could not score: {exc}", file=sys.stderr)
        return 2

    lines = acceptance(reports)
    # One JSON document, so a report can be read by a person and by a script without slicing.
    print(json.dumps({"runs": reports, "acceptance": lines,
                      "probe_rows": len(probes),
                      "company_codes": len(sets["company_codes"])},
                     indent=2, sort_keys=True))
    print("\nacceptance:")
    for line in lines:
        print(f"  {line}")
    print(f"\nprobe rows in the sets: {len(probes)} | company codes: "
          f"{len(sets['company_codes'])} | probe ids are invented labels, safe to quote")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
