# structure-cutover-2: closing note (2026-10-31)

Production now serves the structure-ingested store. Source of truth for the run: Hermes' report
`~/rag-project/docs/ops/runs/structure-cutover.md` on the Thinkpad (counts only). Plan and briefs: `docs/briefs/hermes-structure-cutover.md`,
`agents/lead/briefs/hermes-cutover-prerequisites.md`, `hermes-offhost-backup.md`, `hermes-pptx-ingest.md`.

- Code: tag `structure-cutover-2` on `0047a69` (the build that made the validated store). `f7051e6` was rejected: it cuts the S/4 PDF into 98
  pieces instead of 106 because 13 all-caps heading verdicts flip. Its heading fix is not deployed.
- Phase 3 executed 2026-10-30 with the operator's GO: store swapped, BGE-M3 applied, structure chunking and PDF/DOCX extraction on.
  Downtime 9 m 38 s (plan target was under 5 minutes), no failures. Acceptance 86/100 with `RELATED_CHUNK_NUMBER=1`
  (validated uncapped build 91/100; the 7 held-out losses are the depth cost of the cap), against 85/100 for the legacy production it replaced.
- Follow-up change with the operator's GO: `RERANK_TIMEOUT` 240 -> 600, downtime 45 s.
- First new document: `Solvay_OPR_ECCFlatfiles_v2.pptx` ingested (6 chunks, +46 entities, +74 relations). Store: 11 documents, 135 chunks,
  1,483 entities, 1,867 relations. Recall unchanged at 86/100, zero flips.
- Backups verified bad=0 and copied to deltahome: pre-cutover and pre-PPTX.
- Not done: R=5 stack2 arm, EMBEDDING_TIMEOUT raise, rerank HTTP timeout patch (operator decisions); heading-fix validation.
