# Hermes brief: minimal chunking pilot (P1 header prep vs current chunker) on stack2

**Status: DRAFT, NOT APPROVED.** Operator decision 2026-10-15: chunking is not parked; run
the absolute minimum test of the one method we believe fits this corpus. Stack2 only
(127.0.0.1:9622 / :8021). Never production, never `--allow-production`.

## Hypothesis (the only one tested)
Real transcripts are `(Label) text` lines, 40-51% of turns under 10 tokens. Fixed-token
chunks lose who is speaking and which meeting. P1 (`transcript_prep`: a header every ~200
tokens, `[Meeting | Date | Speakers | Part k/n]`) should lift multi-fact and cross-document
answers. C2/C1/P2 and all other arms are dropped.

## Design (smallest that can say yes or no)
- Corpus: **2 real transcripts** (pick the 2 with the most dev-probe questions), as a
  scratch `loop_corpus.json` listing their on-Thinkpad paths. Same 2 files in both arms.
- Probes: `solvay-dev-v2.jsonl` only (40). Held-out is NOT spent on the pilot.
- Arms, same stack, same settings (gleaning off, rerank on): **C0** current fixed-token,
  then **P1** (`"transcript_prep": true`). Each arm = ingest 2 docs + 40 probes.
- Read-out: dev accuracy C0 vs P1, hallucinations, refusals, and the number of dev
  questions whose source is one of the 2 files. A signal = P1 beats C0 by >= 2 of those
  questions with hallucination not higher. No signal = stop; do not run the 6-doc run.
- Expected cost: a few hours total, not 9 h per arm. Report your own estimate first.

## Step 0 (read-only, do before any go)
1. Explain the evidence for "`--dry-run` really cleared and recreated the stack" (which
   command, which log line, which store path). The driver's dry run uses a fake Ops, so I
   need to know what actually changed. Do not rerun it.
2. What are the 6 paths in `probe/loop_corpus.json`: do the files at those paths exist,
   are they 90-117 chars, where do they live? Counts and sizes only.
3. Per dev-probe question: how many target each of the 6 real transcripts (counts only,
   masked names) and each transcript's token count. Recommend the 2 files.

## Hard rules
No content leaves the host. Stack2 only. No ingest until the lead sends a separate
written go for step 1 of the run (C0), and another for P1.
