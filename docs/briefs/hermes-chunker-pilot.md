# Hermes brief: minimal chunking pilot (P1 header prep vs current chunker) on stack2

**Status: APPROVED in writing by the operator on 2026-10-15: step 0, then the C0 run, then
the P1 run, unattended overnight, no further go needed between them.** Operator is asleep:
the run must complete without anyone answering prompts. Stack2 only (127.0.0.1:9622 /
:8021). Never production, never `--allow-production`, never the :9621/:8020 URLs.

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

## Unattended execution (so it finishes by morning)
Approval prompts only appear in your pane and time out. So: get every approval you need
**up front, in one batch**, then run detached (`nohup`/tmux window, `setsid`) with no
further prompts: write ONE script `~/rag-project/stack2/pilot.sh` that does, in order:
1. Step 0 facts (report-only, never blocks the run; if the 2-file choice is ambiguous
   take the 2 transcripts with the most dev-probe questions, tie-break smaller first).
2. Pilot corpus file + experiments copy with ONLY arms C0 then P1, scratch dir under
   `~/rag-project/scratch/pilot/`.
3. Loop run for C0 then P1 with the full explicit stack2 flag set from the second-stack
   brief and `--max-hours 7`, `--resume` capable, output under
   `~/rag-project/docs/ops/runs/pilot/`.
4. Write `~/rag-project/docs/ops/runs/chunker-pilot.md` incrementally: a status line
   (RUNNING/DONE/FAILED + UTC time) updated after every stage, then the read-out.
Stop conditions (write FAILED + reason and stop, never improvise): any request seen on
:9621/:8020, production counts change (check before and after each arm: processed 10 /
ingested 10), stack2 health lost for 10 min, disk < 5 GB free. Do not stop the worker,
do not touch `.env`, source_docs or uploads. Leave stack2 UP afterwards (teardown is a
separate go). Reply nothing via `rig send`; the lead reads the file.

## Hard rules
No content leaves the host (counts, accuracy numbers, masked names only). Anything not
listed above is out of scope; if blocked, record it in the report file and stop.
