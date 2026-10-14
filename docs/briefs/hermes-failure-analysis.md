# Brief - failure analysis of the Solvay probe results (reads saved files only; NO LightRAG, gateway or sigma requests)

Context: ~/rag-project/probe/loop/ holds per-experiment results (E0-baseline, E1-nogleaning, E2-chunk2000): `<exp>/answers.jsonl`, `<exp>/verdicts.jsonl` (judge verdicts + one-line reasons)
and `results-rejudged.md`. A second run (loop2, E4) is using LightRAG right now: do NOT call LightRAG (:9621), the gateway (:8020), sigma (:1234) or the oMLX judge. Files only.
Held-out accuracy is stuck at 80% and dev at ~82% across E0-E2, so the configs tried so far are not the limiting factor. We need to know WHY questions fail.

## Task (you are the only agent allowed to read the answers; do not paste transcript content, questions or answers into any message to the lead)
1. Take E1-nogleaning (primary) and E2/E0 (to check consistency). For every DEV and HELD-OUT question whose verdict is not correct/refused_ok (the failures), read: question, expected_answer, evidence, the system's answer, the judge verdict+reason, and the source transcript text.
2. Classify each failure into exactly one cause:
   A) judge_wrong       - the answer is actually fine/equivalent; the judge was too strict or misread
   B) probe_bad         - the question/expected_answer is ambiguous, wrong against the transcript, or based on garbled ASR text
   C) not_in_graph      - the needed fact is in the transcript but the answer shows the system never saw it AND the LightRAG store files on disk (kv_store_text_chunks.json / vdb files, read-only, copy to /tmp first) have no chunk containing the evidence phrase => extraction/chunking lost it
   D) retrieval_miss    - the evidence IS in a stored chunk but the answer is missing it / uses other material (refused although answerable counts here)
   E) synthesis_wrong   - relevant material was plausibly retrieved but the answer mis-combines, drops a part, or contradicts it
   F) other / cannot tell (say why in one line)
   Note: the store currently being rebuilt by loop2 may differ from the one E1 used, so for C/D rely on chunk text rather than assuming; if the store no longer contains the transcripts (mid-rebuild) classify C/D as "unknown-store" and say so.
3. Also report: judge reliability = of failures, how many you judge A; and for 5 randomly chosen CORRECT verdicts, whether you agree (agreement x/5).
4. Deliver ONE heredoc report (not write_file) to ~/rag-project/docs/ops/runs/failure-analysis.md with: counts per cause (dev, held-out, per experiment), the question TYPES that fail most (single-fact/multi-fact/follow-up/combine/unanswerable), and 5-8 generic lessons stated WITHOUT quoting transcript content (e.g. "multi-fact questions fail when facts sit in different chunks 3+ apart"). Per-question detail may be kept in ~/rag-project/probe/loop/failure-detail.jsonl (stays on the Thinkpad).
5. Message the lead ONLY: file paths, counts per cause, judge-agreement number, and the generic lessons.
Constraints: read-only except the report, the detail file and /tmp copies; no docker, no rm/mv/chmod/chown; unlisted approval dialogs: do not accept, report.
