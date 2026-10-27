# Worker brief: why does tag `f7051e6` cut the S/4 PDF differently from the validated build `0047a69`?

Branch from current `origin/master` (check `git fetch && git rev-list --left-right --count HEAD...origin/master` reads
`0 0`); use your own packet branch/worktree, do not commit or push, hand back a diff and a short report.
You may NOT read corpus, probe contents, staging, LightRAG data, or the real S/4 PDF or its extracted text. Synthetic
fixtures with invented text only. No network, no ssh. Bare-env full suite must pass:
`.venv/bin/python -m pytest tests/ -q`.

## What happened
Hermes replayed the 10 real documents through the tag `structure-cutover-1` (= `f7051e6`) and compared each piece's
sha256+length with stack2's stored chunks (made by `0047a69`, "chunkrun9"):
- 8 text transcripts: 21 pieces, 21 byte-exact. OrderBook.docx: 2 pieces, 2 byte-exact.
- **S/4 PDF: tag 98 pieces, stored 106, 0 sha overlap.** Sizes are in the same regime (tag p50 2,280 / max 14,503 chars;
  stored p50 2,273 / max 14,534); the boundaries differ. Tag meta for the PDF: kind=structured, pieces_merged 53,
  units_whole 151, max_piece_tokens budget 7,500.
The gate was 129/129, so this is a stop: the 91/100 recall result belongs to the `0047a69` chunking and may not carry
over to the tag. Hermes' audit ran with the "env identical to attempt 3" and printed the tag's defaults
`WORKER_PDF_EXTRACT_ENABLED=False`, `WORKER_DOCX_EXTRACT_ENABLED=False`, `TRANSCRIPT_PREP_ENABLED=False`.

## Everything that differs in app/ between the two commits
`git diff 0047a69 f7051e6 -- app` is small:
1. `app/structure_chunker.py` `_is_all_caps_heading`: `_TERMINAL.match(stripped)` became `_TERMINAL.search(stripped)`, so
   an all-caps line containing terminal punctuation ANYWHERE (not just at the start) is no longer a heading.
2. `app/transcript_prep.py` (+71): key/value-list detection so such lists are no longer read as a one-voice transcript.
3. `app/config.py`: `WORKER_PDF_EXTRACT_ENABLED` and `WORKER_DOCX_EXTRACT_ENABLED` defaults flipped from true to false.

## What I need to know (rank the candidates, do not guess)
A. Can change (1) alone move PDF boundaries? Read `_TERMINAL` and the call sites, then build a synthetic PDF-like text
   (page headers, all-caps section lines with and without internal punctuation such as a trailing period, a number
   with a period, "A.B. CORP"-style lines) and show piece counts under both versions of the function. Is the effect
   plausible at the size seen (106 to 98, i.e. fewer headings, so larger merged units)? The direction matters: a
   stricter heading rule should give FEWER pieces, which matches.
B. Can (2) apply to PDF text at all? Which code path runs `transcript_prep` and under what flags and document kinds? If
   it only runs for transcripts, say so with the line references.
C. Can (3) change what the chunker is fed? With `WORKER_PDF_EXTRACT_ENABLED` false the PDF goes by the upload route
   (LightRAG's own extraction) and the structure chunker never sees it; with true it is extracted by pypdf and chunked
   in the worker. Hermes' audit reports the tag cut the PDF, so extract must have been on in the audit, but confirm
   from the audit-facing code how the replay gets its text and whether a different extraction default or a pypdf
   version difference (the tag pins `pypdf==5.3.0`) could alter the text itself.
D. Name the single most likely cause and state how confident you are and why.

## Deliverable 1: a counts-only bisect tool for Hermes
Hermes can run it on the real PDF; you cannot. Write `scripts/chunk_boundary_diff.py`:
- Input: a text file path (or a PDF path, extracting with the same code the worker uses) and two repo trees or two
  commit-ish names (`--left`, `--right`, each resolved to a clean checkout in a temp dir, never the working tree).
- Output only counts and ids, never text: pieces per side, per-piece length and sha256, the first piece index where the
  two sides diverge, number of identical pieces, and for each differing region the character offsets and lengths
  (not content) on both sides. Plus a flag `--env KEY=VALUE` repeatable so the chunker's environment can be pinned.
- Optional `--only-function` mode that reports, for every line the heading detector examined, whether the two versions
  disagree, as (line number, length, verdict_left, verdict_right) with NO line text. This lets Hermes see how many
  headings flipped and where, without any content in the report.
- Tests with invented text. It must not import anything outside the repo and the standard library.

## Deliverable 2: the recommendation
One paragraph: is the new cut a strict fix (a heading that was wrongly split off) or a behaviour change that could
plausibly alter retrieval? Say what experiment would decide it (for example re-running the 32 structure probes on a
PDF re-ingested under the tag) without running it. Do not change the chunker.

## Not in scope
Changing `structure_chunker.py`, `transcript_prep.py` or `config.py`; re-tagging; touching production, the Thinkpad or
Hermes' helpers; any claim about retrieval quality you have not measured.

## Hand-back (counts only)
The diff, suite result, the ranked candidate list A to D with line references, the tool's usage line, and the
recommendation paragraph.
