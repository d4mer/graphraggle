# Task - author probe set v2 for the Solvay accuracy tests (operator-approved: dev-worker writes the questions; Hermes validates and runs them)

Do packet-34 (judge rubric) FIRST, then this. This task is NOT repo work: all files stay OUTSIDE the repo, are never committed, and no transcript text, question, answer or quote may appear in any message, queue note or commit.
Report back with COUNTS ONLY.

## Inputs (private, read-only for you)
`/home/maverick/work/solvay-src/dev/`      4 meeting transcripts (speech-to-text, noisy)  -> source for the DEV set
`/home/maverick/work/solvay-src/heldout/`  2 meeting transcripts                          -> source for the HELD-OUT set (never use these for dev questions)
Read ONLY these files. Do not read LightRAG, the gateway, the Thinkpad, or any probe/results/answer files. Do not run any question through any system.

## Outputs (create /home/maverick/work/solvay-probes/ ; chmod 700)
- `solvay-dev-v2.jsonl`      40 questions, ids dev01..dev40, from the dev transcripts (<=14 per transcript)
- `solvay-heldout-v2.jsonl`  40 questions, ids ho01..ho40, from the held-out transcripts (<=25 per transcript)
- `verify_probes.py`         stdlib script: `python3 verify_probes.py <jsonl> <source_dir>` -> checks schema, id format, that every non-empty `evidence` quote appears in one of the source files (case-insensitive, whitespace-normalised, quote
                             must be <=300 chars), type counts, unanswerable-term absence (below); prints counts only; exit 1 on any failure. Run it and fix until both files pass.
One JSON object per line with fields: `id`, `question`, `source_doc` (file name), `evidence` (verbatim quote, "" for unanswerable), `expected_answer` (1-3 sentences; for unanswerable exactly "not in the documents"),
`type` (single-fact | multi-fact | follow-up | combine | unanswerable), `history` (only for follow-up: list of {"role":"user"|"assistant","content":...} giving the preceding turn(s)), and for unanswerable also `absent_terms` (3-6 distinctive words/phrases that
the question depends on; verify_probes.py must confirm NONE of them occur in any source transcript).
Mix per set: ~45% single-fact, ~20% multi-fact (2-3 facts in the same transcript), ~10% follow-up (4 per set; the follow-up question must be unintelligible without the history), ~10% combine (facts from two places in the same transcript), >=12% unanswerable (>=5 per set).

## Lessons from the last run (these caused false failures - avoid them)
1. Self-contained and unambiguous: exactly ONE defensible answer. If the text contains two candidate answers, put discriminating context in the question or pick another fact.
2. Do NOT ask who said something or attribute statements to named people; speaker labels in these transcripts are unreliable ("Speaker 0", "You"). Ask about topics, decisions, numbers, dates, system names, plans, risks, responsibilities-in-general.
3. Do not over-scope: if the text says something applies to everybody or to "me", do not ask "why did group Y ...".
4. The transcripts are noisy ASR with garbled words. Use only passages that are clearly intelligible; prefer facts stated plainly. If the garbled term is the answer, skip it. In expected_answer you may add acceptable alternates in parentheses.
5. Questions are written the way a colleague would ask, not by copying the text's wording; no yes/no questions; no questions about the meeting's recording logistics or small talk.
6. Unanswerable questions must be plausible for the meeting topic (same systems/people/process) but ask for a specific fact that is verifiably not in the transcripts; the `absent_terms` check enforces it.

## Report (counts only; to the lead via your normal handoff note)
Per set: lines, per-type counts, per-source counts, verify_probes.py result (checked/passed), and the file paths. Nothing else.
