# Worker brief — GL-13: fresh Solvay held-out probe set for the go-live accuracy gate (Plane GRAG-68)

Base: branch from current `origin/master` (check 0 0 first). Own branch/worktree. Probe files are NEVER committed; they live in /home/maverick/work/solvay-probes (your existing exception: you may read SOURCE
transcripts in /home/maverick/work/solvay-src to author probes; you still never read the corpus/LightRAG content). Counts-only reports. Hermes copies and runs them.

## Why
The go-live accuracy bar (operator, 2026-10-31) is ok% >= 88 on a NEW held-out set per company, 0 hallucinations, refusal correctness >= 90%. Every existing Solvay probe set is consumed
(tuned against). Operator priority: accuracy over speed.

## Do
1. Author at least 40 NEW Solvay probes from the source documents in solvay-src, none duplicating a question in any existing set (check the existing sets' questions by ID/count and by a normalised-text overlap
   check you write; report the max overlap as a number, no text). Cover: fact lookup (single-document), multi-fact synthesis (the known weak spot), table/numeric lookup, questions about the PDF, DOCX and PPTX
   (not only transcripts), and questions whose answer sits in a speaker turn deep in a long transcript. Include at least 8 unanswerables (plausible, but the fact is in no document) and 4 near-miss
   unanswerables that borrow a neighbouring fact's wording.
2. Same file format and field names as the existing held-out sets (look at probe/run_prod_recall.py's loader and the v2/v3 files' schema in the repo or your probes folder), plus a `shape` field:
   `fact|synthesis|table|pdf|docx|pptx|deep-turn|unanswerable|near-miss`.
3. Every probe carries: expected answer essentials (short), source document + location, difficulty guess. Write a separate answer-key file; the question file must not contain answers.
4. Validate: each answerable probe's expected answer really is in the cited source (a script that checks the key phrases occur in the source file, report pass counts), and each unanswerable's distinctive phrase occurs in no source file.
5. Do NOT run anything against production or stack2. Put files under /home/maverick/work/solvay-probes/gl13/ and report their sha256s.

## Hand-back
Paths, counts per `shape`, overlap number, validation pass counts. `rig queue create --destination orch-lead@graphraggle`. No code change expected; if you add a validator script, put it under scripts/ with tests and the suite must pass bare.
