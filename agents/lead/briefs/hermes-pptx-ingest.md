# Hermes brief — first new-document ingest on the cutover stack: one PPTX (2026-10-30)

Goal: ingest `Solvay_OPR_ECCFlatfiles_v2.pptx` (staged, 3,180,819 B, the only staged file not yet ingested) through the live
structure-chunking worker, and show the new path works and did not hurt the existing store. This is the first new document
since the cutover; the worker has only been proven idle so far. Operator asked for this on 2026-10-30.
Corpus content stays with you: counts, ids, sizes, timings only in the report.

## Preconditions (stop and report if any fails; do not work around)
1. **Off-host backup done first.** Wait for `hermes-offhost-backup.md` to finish with bad=0 on deltahome. Do not ingest before.
2. **Stack2 A/B quiescent.** Finish or stop the stack2 R=2/3 A/B before ingesting (sigma contention, and production recall
   re-measurement must not overlap). Say which you did.
3. Production at rest: 10 processed / 0 failed / 0 processing; worker running; /health 200 on :9621 and :8020.

## Do
1. **Snapshot before:** online-backup `state/ingest.db` and copy `lightrag_store/` to `backups/pre-pptx-<UTC>/` with sha256 manifest.
   Record before-counts: documents by status, chunks, entities, relations.
2. **Dry-run the extractor and chunker first, outside production.** Run the tag image's PPTX extractor + structure chunker on a
   copy of the file in scratch. Report: slides, characters extracted, pieces, token min/median/max, warnings, whether tables,
   grouped shapes and speaker notes were captured (yes/no each), `kind_detected`. If extraction yields ~0 chars or any piece
   exceeds the 7500-token budget: stop and report.
3. **Ingest:** copy the file into `source_docs/Solvay/` (same layout the other 10 use). Do not edit `.env` or compose. Watch the
   worker log and state DB until the row is `ingested` and LightRAG shows it `processed`. Never interrupt it mid-extraction.
   Expect a long run (sigma); background with a log. Only the new document may be new: existing 10 must not be resubmitted.
4. **Verify:** after-counts for chunks/entities/relations (deltas), document by status = 11 processed / 0 failed; new-doc piece
   count equals the dry-run's; vector dim 1024 on a sample of the new chunks; no Traceback/ERROR in worker or server logs;
   5 further scan cycles with no resubmission; open-webui container id unchanged.
5. **Regression check:** re-run the 100-probe `probe/run_prod_recall.py` (:9621, same MAX_SECS=180 copy as 10-30).
   Baseline = 86/100 (solvay-dev 31/34, solvay-heldout 27/34, structure-dev 14/16, structure-heldout 14/16).
   Report counts per set and flips. A drop of more than 3 total is a finding: report, do not roll back on your own.
6. If a new-document smoke query is possible without reading corpus content, run 2 bridge queries and report only
   non-empty / has-Sources.

## Hard rules
- No `.env`/compose change, no restart, no `--allow-production`, no rerank/LLM/query parameter change.
- Rollback only on lead/operator instruction: restore from `backups/pre-pptx-<UTC>` and delete the new row; stop and report first.
- One bare command per approval; background long jobs with a log; do not retry a denied approval.

## Report
Section "PPTX INGEST 2026-10-30" in docs/ops/runs/structure-cutover.md, and update STATUS line 1. Include dry-run stats,
before/after counts, timings, probe counts, backup paths and sha256 results. Lead cannot be messaged from your seat:
write the file and say so in your pane; lead will read it.
