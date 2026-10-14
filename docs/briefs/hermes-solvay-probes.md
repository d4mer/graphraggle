# Brief - Solvay probe sets (dev + held-out) and runner (read-only on the stack)

Operator GO 2026-10-12 for an accuracy loop (build tests -> run -> analyse -> correct -> re-test). Do this AFTER the 6-transcript
ingestion batch has finished and its report is written. Read-only on the stack: no config changes, no deploys, no restarts.
Reuse what you built before in ~/rag-project/probe (build_probe_set.py, p24_accept.py, rc1_run.py) and the format in
/tmp/probe-set-generation.md (same jsonl fields). The old probe-set-v1 is for the wiped GSK corpus: do not use it.

## Sets (read the ORIGINAL files in ~/rag-project/staging/Solvay/, never LightRAG output; never run questions through the system to write expectations)
- DEV set  -> ~/rag-project/probe/solvay-dev-v1.jsonl : 2 transcripts, ~20 questions each (min 16):
    "Cross-GBU OMP Prioritization Update-transcript.txt", "Title_   S_4HANA and OMP Implementation for Soda Ash-transcript.txt"
- HELD-OUT -> ~/rag-project/probe/solvay-heldout-v1.jsonl : 1 transcript, 15 questions (min 12):
    "Oct 12, 2026 at 9_30 AM Recording-transcript.txt"
  The held-out set is NEVER used for tuning. Keep its questions and per-question results out of messages to me: report only aggregates.
Mix per set: ~50% single-fact, ~20% multi-fact (same doc), ~10% follow-up (include `history`), ~15% answerable only by combining 2+
places in the doc, and at least 3 (dev) / 2 (held-out) UNANSWERABLE questions plausible for the topic but absent from the text
(expected_answer = "not in the documents"). Questions phrased like a person asking, not copying the text; no yes/no. Each answerable
question needs a verbatim `evidence` quote (<=300 chars) located in the original file. These are noisy speech-to-text transcripts: quote what is
actually there, and skip passages that are too garbled to support a clear answer.
Script a verification: every evidence quote must be found (whitespace-normalised) in its source file; drop failures. Report checked/passed.

## Runner: ~/rag-project/probe/run_probes.py  --set dev|heldout  --out <results.jsonl>
For each question call the SAME path OpenWebUI uses (gateway /api/chat Ollama bridge on :8020, or whatever p24_accept.py used - copy its
call, headers from .env via variables, never print keys). Include `history` for follow-ups. Record: id, question, answer (full), latency_s,
citations/sources if returned, error. Run sequentially, 1 request at a time, 600 s timeout per question; resumable (skip ids already in --out).
Do NOT run while the ingestion batch is still processing.

## Scoring: ~/rag-project/probe/score_probes.py + your own judgment
Step A (script): for each answerable question, key-fact check (expected key terms/numbers present in answer) as a hint only.
Step B (you, per question): verdict in {correct, partial, wrong, hallucinated, refused_ok (unanswerable correctly refused),
refused_wrong (answerable but refused)} + one-line reason, judged against `evidence` and `expected_answer`, never against the system's own text.
Write verdicts to <results>.verdicts.jsonl. Aggregate: overall correct%, (correct+0.5*partial)%, hallucination%, refusal accuracy on
unanswerables, by type, latency p50/p95. Per-question table for DEV only goes in the report; held-out gets aggregates only.

## Delivery
1. First run: build both sets, verify, run DEV only once to smoke-test the runner and produce the baseline (v0). Report to
   ~/rag-project/docs/ops/runs/solvay-probe-baseline.md (heredoc, not write_file): set sizes, verification counts, aggregate scores, per-question
   DEV failures with reason (what the answer said vs evidence) and a root-cause guess per failure: not-extracted / not-retrieved /
   retrieved-but-wrong / transcript-garbled. Then run HELD-OUT once and add aggregates only.
2. Message me with ONLY file paths, counts and the aggregate numbers. No transcript content in messages.
Constraints: no writes outside ~/rag-project/probe and docs/ops/runs; no rm/mv/chmod/chown; unknown approval dialogs: do not accept, report.
