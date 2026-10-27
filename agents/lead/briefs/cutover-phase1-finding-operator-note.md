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

## Most likely cause (worker's diagnosis, 2026-10-27, ~80% confidence, not yet confirmed on the real PDF)
The text handed to the chunker probably differed: the PDF is extracted by `pdftotext -layout` when poppler is present
and by `pypdf` otherwise, and the row-rejoining step differs by 803 rows versus 0 on this corpus, enough to move every
boundary. Only the PDF differs (8 transcripts and the DOCX are byte-exact), which fits. The stricter heading rule in
`f7051e6` is now the unlikely cause (~5%): on synthetic text at the shipped window it changed the unit plan but not one
stored piece. A second candidate (~15%) is `transcript_prep` having run in one run and not the other. My earlier guess
(the heading rule) was wrong. The worker added a counts-only tool (`scripts/chunk_boundary_diff.py`, on master) so
Hermes can settle it on the real PDF without any text in a report. Two cheap read-only checks decide it (drafted in
`docs/briefs/hermes-s4-pdf-split-diagnosis.md`, needs your approval).

## Options
1. **Cut over with the exact validated build** (tag `0047a69` or its merge `9e1e308`). Reproduces 129/129 and keeps the
   91/100 evidence honest. Gives up the later heading fix and the key/value fix. Fastest.
2. **Keep the tag `f7051e6` and re-ingest the PDF on stack2 under it**, then re-run the 100 probes. Measures the build you
   would actually ship. Costs roughly a day of oMLX time and is one more non-deterministic extraction draw.
3. **Hold** until the two read-only checks above say whether the cause is the extractor, `transcript_prep`, or the chunker.

My recommendation: option 3 first (read-only, about an hour), then choose. If the cause is the extractor, the fix is to
pin the PDF backend and the 91/100 result likely still stands for the tag (option 2 only if the replay still differs
after pinning); if it is the chunker, option 1 or a re-ingest.

## Also found in phase 0 (for phase 3, no decision needed now)
- The production ingest-worker has no BGE-M3 tokenizer mount; phase 3 needs a read-only mount.
- The tag's PDF/DOCX extract defaults are false, so phase 3's env list is required, not redundant.
- Production still runs mxbai-embed-large-v1; the swap forces BGE-M3 in the same window.

## Reply needed
"Approve the diagnosis checks" (the read-only brief to Hermes, then I report and you choose), or "option 1", or "option 2".
Phases 2 and 3 stay unstarted until you choose.
