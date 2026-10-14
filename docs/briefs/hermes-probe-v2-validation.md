# Brief - independent validation of probe set v2 (files only; do NOT touch LightRAG, the gateway, sigma or the loop)

The dev-worker authored two probe sets (operator-approved) from the Solvay transcripts: ~/rag-project/probe/solvay-dev-v2.jsonl (40) and solvay-heldout-v2.jsonl (40). The author already ran a verbatim-evidence check. You are the independent second reader.
Sources (read-only): ~/rag-project/staging/Solvay/ (dev set uses: Cross-GBU, S/4HANA Soda Ash, OMP Profile Setup, 10:10 AM recording; held-out uses: 9:30 AM recording, "It seems there were no meeting summary notes..." transcript).

## Task
1. For each set take a random sample of 12 questions (python random.seed(20261013); make sure the sample includes at least 2 unanswerable and 1 follow-up if present). For each sampled question read the question, history, expected_answer, evidence and the source transcript, and classify:
   V  valid        - one defensible answer, expected_answer correct per the transcript, evidence supports it (or, for unanswerable: the fact is really absent, not merely worded differently)
   A  ambiguous    - two defensible answers, or answerable only with context the question does not give
   X  wrong        - expected_answer contradicts or is not supported by the transcript
   G  garbled      - the supporting passage is too garbled to support a clear answer
   S  speaker/scope trap - depends on who said it / over-scoped attribution
   U  unanswerable_but_present - marked unanswerable but the transcript does answer it (any wording)
2. Do NOT edit the probe files. Keep per-question detail locally in ~/rag-project/probe/probe-v2-validation-detail.jsonl.
3. Write ONE heredoc summary (not write_file, max 25 lines, NO question/answer/transcript text) to ~/rag-project/docs/ops/runs/probe-v2-validation-summary.md: per set counts of V/A/X/G/S/U among the 12, the ids of the non-V questions (ids only, e.g. dev07), and one overall line: PASS if each set has <=2 non-V, else FAIL.
4. Reply 'done' in your pane. The lead reads only that summary file. Do not message the lead.
Constraints: read-only except the two output files; no docker, no rm/mv/chmod/chown; unlisted approval dialogs: do not accept, report.
