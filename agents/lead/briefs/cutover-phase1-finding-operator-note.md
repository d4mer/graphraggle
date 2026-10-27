# Operator note: cutover phase 1 failed, the tag differs from the validated build

To: operator. From: lead. Date: 2026-10-27. Re: `docs/briefs/hermes-structure-cutover.md` (phases 0 to 2 approved 2026-10-26).
Source: Hermes' report `~/rag-project/docs/ops/runs/structure-cutover.md` on the Thinkpad. Production was not touched.

## Result in one paragraph
Phase 0 passed. Phase 1 failed its gate: the tag `structure-cutover-1` (commit `f7051e6`) does not reproduce stack2's
129 stored chunks. Hermes stopped, as the brief requires. Phase 2 and phase 3 have not started and nothing was changed.

## What failed
| document | tag pieces | stored pieces | byte-exact |
|---|---|---|---|
| 8 text transcripts | 21 | 21 | 21 |
| OrderBook.docx | 2 | 2 | 2 |
| S/4 PDF | 98 | 106 | 0 |

23 of 129 pieces match; all of the difference is the S/4 PDF. Piece sizes are similar, the boundaries differ.

## Why it matters
- The 91/100 recall result (and its +8 over legacy chunking) was measured on the build `0047a69`. The tag contains two
  later chunker/extract changes, so the number is not proven for the tag as it stands.
- Most of the +8 comes from the structure probe sets, which depend on the PDF. That is the document that now cuts differently.
- My earlier statement that the tag was safe because later commits touched nothing under `app/` was wrong in effect:
  `f7051e6` itself changed chunker behaviour compared with the validated build. The brief's phase 1 exists to catch
  exactly this, and it did.

## Diagnosis (settled by Hermes, 2026-10-27, `s4-pdf-split-diagnosis.md`)
The cause is the chunker, not the extractor. On one fixed text dump, `0047a69` reproduces the stored 106 PDF pieces
byte-exactly, while `f7051e6` cuts 98 from the same bytes. Exactly 13 all-caps-heading verdicts flip (short all-caps lines
ending in terminal punctuation, the case `f7051e6` was written to stop treating as headings), which drops units 164 to 151
and the merge-floor merges 58 to 53. The extractor, poppler and pypdf versions, and `transcript_prep` were identical in
both runs. The worker's earlier guess (extractor, ~80%) was wrong; its synthetic test could not show the real-text effect.

## Decision (operator chose option 1, 2026-10-27)
Cut over with the validated build: tag `structure-cutover-2` on `0047a69`. It reproduces 129/129 and the 91/100 evidence
applies exactly. What is given up: the heading fix, which affects 13 lines in one document, plus the key/value fix and the
extract-default flip, neither of which has any effect while `TRANSCRIPT_PREP_ENABLED` is false. The heading fix can ship
later after its own validation. Nothing outside `app/` differs between the two commits; the suite at `0047a69` passes.
Next: Hermes repeats phase 1 on the new tag (expected 129/129), then runs phase 2. Phase 3 still needs your separate go.

## Also found in phase 0 (for phase 3, no decision needed now)
- The production ingest-worker has no BGE-M3 tokenizer mount; phase 3 needs a read-only mount.
- The tag's PDF/DOCX extract defaults are false, so phase 3's env list is required, not redundant.
- Production still runs mxbai-embed-large-v1; the swap forces BGE-M3 in the same window.

## Reply needed
None for now. Phases 0 to 2 remain approved; I will ask for your separate "go for phase 3" only after Hermes' phase 2
report and the phase 3 plan are in.
