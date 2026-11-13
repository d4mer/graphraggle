# Worker brief — run-export scorer for the M1 asked-identifier gate (go/no-go evidence)

Base: current `origin/master` (f5af365 or later; check `git fetch && git rev-list --left-right --count HEAD...origin/master` reads `0 0`). Own branch/worktree, no push, one commit.
Bare full suite must pass: `.venv/bin/python -m pytest tests/ -q`. Hand back via `rig queue create --destination orch-lead@graphraggle --summary ...`.

## Why
M1 (`app/grounding.py`, flag `GROUNDING_CHECK_ASKED_IDENTIFIER`, flag-only) is merged. Whether to ENFORCE it (refuse when the asked identifier is not in the evidence) is decided
from one run's numbers, not from the generated-corpus harness: on the labelled probe set, how many unanswerable probes would be refused, how many hallucinations that
catches, and, the guard, how many answerable probes would be newly refused (target 0). Today those figures exist only in an unversioned stack2 export; you flagged this yourself.

## Task
A script `scripts/score_grounding_run.py` (stdlib + repo deps) that takes (a) a probe manifest (the GL-07 `manifest.json` probe rows, shape/expected labels) and (b) a run export
(the per-probe results the Hermes runner writes, `*.results.jsonl`, plus, when the flag was on, the gateway's per-query log lines carrying the `grounding_*` fields you added) and
prints COUNTS ONLY (no question text, no answers, no reference text):
- per shape (scoped / cross-company / unanswerable): n, correct / refused / partial / hallucinated as already labelled in the export;
- gate verdict cross-tabs: for unanswerable rows, gate fired (`supported` False) vs not, split by hallucinated vs refused-by-the-model; for answerable rows, gate fired / not /
  `supported is None` (no identifier asked);
- the headline: `would_newly_refuse_answerable` (must be reported even when 0), `hallucinations_caught`, `hallucinations_missed`, `references_without_content` total;
- distinct questions vs rows (the 55-row run is 41 distinct questions; say which denominator each rate uses).
It must join the export and the log on a stable key and FAIL LOUDLY on rows it cannot join or on a key it does not recognise; never guess a shape.

## The shape problem (do not guess)
You have not seen a real export and the runner (Hermes `scratch/gl08/probe_baseline.py`, on the thinkpad) is not in this repo. Look for a copy at
`/home/maverick/work/gl08-run-exports/` (synthetic probes only; Hermes may drop one there). If none exists: build the scorer and loader against the field names the
gateway/`emit_query_log` and your M1 commit define, test it with invented fixtures, and in the hand-back list EXACTLY which fields of the runner's export you assumed, so the lead
can have Hermes send one real export file. Do not invent labels beyond what the export carries.

## Tests
Invented fixtures only: the cross-tabs on a small hand-built run, a join failure, an unrecognised key, `supported is None` rows, zero answerable-newly-refused, references-without-content,
and a check that no question/answer/reference text appears in the output.
