# Hermes — operator GO: quarantine a second batch of "unknown" files (2026-10-31)

Same method as B4: mv (not delete) into quarantine/gsk-20261031/ (keep relative paths), extend SHA256SUMS and MOVED.tsv, verify after.
APPROVED, exactly these 18 files:
- docs/ops/runs/pilot/C0-baseline/{answers,verdicts}.jsonl, docs/ops/runs/pilot/P1-transcript-prep/{answers,verdicts}.jsonl  (4)
- probe/loop/E0-baseline/verdicts.jsonl, probe/loop/E1-nogleaning/verdicts.jsonl, probe/loop/E2-chunk2000/verdicts.jsonl, probe/loop/failure-detail.jsonl  (4)
- probe/loop3/G1-more-context, G2-no-rerank, G3-bigger-budget, G4-less-context: answers.jsonl + verdicts.jsonl each  (8)
- probe/runs/lightrag-server.err.log  (1)
- probe/text/pdf_6.txt  (1)
NOT approved, leave in place: probe/loop4*, probe/loop5*, probe/loop6* (Solvay runs), probe/runs/chunker-dryrun/*, all *.stdout / *.log
not listed above, probe/omlx_60k.log, oversize_trace.log, cache_test.log, rejudge.stdout. Do not read contents. Nothing else changes.
Report: append "QUARANTINE BATCH 2" to structure-cutover.md: moved count, verify OK/BAD, the 25 remaining unknown files by name, STATUS line.
