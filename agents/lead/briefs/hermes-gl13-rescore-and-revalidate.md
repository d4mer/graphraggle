# Hermes brief — judge-only re-score of the stored GL-13/GL-13b answers against the audited keys, plus production-reader revalidation (2026-11-16)

SEND ONLY AFTER the stack2 identifier re-run has reported (one task at a time; both touch the judge/sigma models).

Context: the dev-worker's authoring audit (it never saw your answers) found 8 of the 14 non-ok ids in gl13b were ANSWER-KEY faults (the builder checked the evidence quote but not the rest of expected_answer). It re-keyed 7 rows from their
anchors, retired gl13b/dc07 and gl13/hq04, and moved gl13/hq08 to a later history round. The scored sets are the record of what you ran and stay untouched. Corrected pool = gl13-v2 (49) + gl13b-v2 (68) = 117.
The same STORED answers score 106/117 = 90.6% in the worst case (no re-keyed row flips) and 113/117 = 96.6% in the best. This re-score only changes verdicts through the key, no re-querying.
All files are in ~/briefs/probes-solvay/ (chmod 600/700; derived from real Solvay documents, counts and ids only in any report, as before).

## 1. Judge-only re-score (no queries to production, no stack changes)
Inputs: gl13b-v2/rescore-set.jsonl (10 rows: 7 re-keyed dc05 dc10 dc12 dt01 pp01 pp04 sy02 + controls pd19 pp02 pp07) and gl13-v2/rescore-set.jsonl (1 control: cx07), run-set schema, same ids.
Take the STORED answers for these ids from scratch/gl13/gl13*.answers.jsonl, judge them with your SAME judge (qwen3.6-35b on oMLX, rubric v2, temp 0, the judge script as fixed on 11-16 with the 'Bearer ' header line intact; do not re-edit that line with
the patch tool). Verify the sha256 of each rescore-set against the README values first. Report per id: old verdict -> new verdict. Control rows (pd19 pp02 pp07 cx07) have byte-identical keys, so any flip there is judge variance: report it.
Then the corrected-pool tallies (gl13-v2, gl13b-v2, combined) in the same format as the scored-run section: n, correct, refused_ok, partial, wrong, halluc, ok%, per set and per source doc (pdf / docx / pptx) for gl13b-v2. Keep the original scored-run section untouched; add a new section.

## 2. Revalidate the keys under the readers PRODUCTION uses (counts and ids only)
In the production worker container (or the checkout with pypdf, python-docx, python-pptx installed; say which) run, from scratch/gl-deploy at the repo path you use:
  python ~/briefs/probes-solvay/tools/revalidate_production_readers.py --repo <repo> --sets ~/briefs/probes-solvay/gl13-v2 --sets ~/briefs/probes-solvay/gl13b-v2 --pdf-backend pypdf --out revalidation-production-readers.json
  python ~/briefs/probes-solvay/tools/key_content_gate.py --repo <repo> --sets ~/briefs/probes-solvay/gl13-v2 --sets ~/briefs/probes-solvay/gl13b-v2 --pdf-backend pypdf --report gate-stack.json
(If a script needs the source documents, they are in staging; use the same files production ingested. The worker's host had no pypdf/python-docx, so this is the first measurement of "verbatim under pypdf / python-docx".) Report: rows checked, rows verbatim, ids not verbatim,
and the gate's hard-finding counts. If a reader or file is missing say so, do not guess.

## Rules
One bare command per approval; stop on first failure; do not retry a denied approval; do not message the lead mid-turn. Persist the /query `references` (file_path + chunk presence, ids only) in any future run you do. Report in structure-cutover.md,
section "GL-13 CORRECTED-KEY RE-SCORE 2026-11-16", STATUS line 1.
