# Task - fix two held-out probes and author 20 fresh held-out probes (operator-approved; same private-data rules as probe-authoring-v2)

This is NOT repo work. All files stay OUTSIDE the repo, are never committed, and no transcript text, question, answer or quote appears in any message, queue note or commit. Report COUNTS ONLY.

## Inputs (private, read-only)
`/home/maverick/work/solvay-src/heldout/` (2 transcripts) and `/home/maverick/work/solvay-src/dev/` (4). Existing probes: `/home/maverick/work/solvay-probes/solvay-heldout-v2.jsonl`, `solvay-dev-v2.jsonl`, `verify_probes.py`.
Read ONLY those. Do not read LightRAG, the gateway, the Thinkpad, or any answers/verdicts/results.

## Part A - fix two mislabelled probes (copy, do not edit the originals)
Held-out probes `ho20` and `ho39` are typed `unanswerable`, but an independent reader found their answer IS in the held-out transcripts. For each: re-read the transcripts, and
- if the answer is plainly and unambiguously present, rewrite the record as an answerable probe (real `evidence` quote, `expected_answer`, type single-fact/multi-fact, drop `absent_terms`);
- if the answer is only garbled/ambiguous, replace the probe with a genuinely unanswerable one on the same topic (valid `absent_terms`, verified absent), keeping id and type unanswerable.
Write the result as `solvay-heldout-v2.1.jsonl` (40 lines, same ids, all other lines byte-identical to v2). Also check ALL remaining unanswerable probes in v2.1 by grepping their `absent_terms` AND the key nouns of the question against the transcripts (case-insensitive); change any probe whose answer can be found. Report the counts of changed ids only.

## Part B - fresh held-out set v3 (never seen by anyone: not run, not tuned on)
Create `solvay-heldout-v3.jsonl`: 24 questions, ids hv01..hv24, drawn from the held-out transcripts (<=14 per transcript), on facts NOT used by any evidence quote in v2/v2.1/dev (check by non-overlap of evidence passages and different topic when possible).
Mix: ~9 single-fact, ~5 multi-fact, 3 follow-up (with `history`), 3 combine, 4 unanswerable (type unanswerable, `absent_terms` 3-6, verified absent; ask for a specific fact plausible for the meeting topic).
Same schema and the six lessons from probe-authoring-v2 (one defensible answer, no speaker attribution, no over-scoping, intelligible passages only, colleague phrasing and no yes/no, plausible unanswerables).
Extra rules: for every answerable probe, the evidence quote must contain the answer itself (not just the topic); for every unanswerable one run a second check that the question's topic nouns do not appear next to a value of the requested kind in the transcripts.

## Verification
Extend/reuse `verify_probes.py` (stays outside the repo) and run it on both new files; fix until both pass. Files: chmod 600, dir stays chmod 700.

## Report (counts only, via handoff note)
Part A: ids changed (ho20/ho39 and any others) and how each was resolved (answerable vs new unanswerable), v2.1 line count, verify result. Part B: line count, per-type and per-source counts, verify result, file paths.
