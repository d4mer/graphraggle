# worker seat — durable context

Last updated: 2026-10-19, after handing off `qitem-20261019063829-7bb6594a`.

## Queue state

`rig queue list --owned` → `[]`. Nothing assigned. Do not invent work; wait for a row from
`orch-lead@graphraggle` or for the operator.

Latest handoff: `qitem-20261019063829-7bb6594a` (flat-path over-window split, two-line
monolith detection, name+pronoun rotation with stage markers) → `orch-lead@graphraggle`.
Successor row the lead now owns: `qitem-20261019072147-f2c8e39a`. **The work is uncommitted
in the worktree** — the lead integrates and pushes; I never push.

## Where the work lives

- Worktree `/home/maverick/work/graphraggle-ctx`, branch `context-chunking`, HEAD `63c9cc3`.
  Uncommitted: `app/structure_chunker.py`, `app/transcript_prep.py`,
  `tests/test_structure_chunker.py`, `docs/08-change-log.md`, `docs/ops/loop/README.md`
  (no new files this time, nothing to `git add`).
- Tests: `timeout 900 env -i PATH=$PATH HOME=$HOME /home/maverick/work/graphraggle/.venv/bin/python -m pytest tests/ -q`
  → **1107 passed, 2 warnings, 166 subtests in ~6.2s**.
- pyflakes: `PYTHONPATH=/tmp/pyflakes_lib …/python -m pyflakes <files>` — clean.
- Venv `/home/maverick/work/graphraggle/.venv/bin/python` (3.14). Missing: tiktoken, pypdf,
  python-docx, transformers, tokenizers, sentencepiece, numpy. PRESENT: python-pptx, httpx,
  `pdftotext`.

## What this last item produced (for follow-ups)

- **Window is an invariant.** `_split_oversize()` in `app/structure_chunker.py` splits any body
  over the window: balanced pieces (a tail leftover is welded sideways while the neighbour fits,
  stat `oversized_stub_welds`), never mid-word, boundary order sentence → clause → whitespace.
  Called from every path that can produce an oversize body (flat/topic units, paragraph blocks
  in `_split_unit_blocks`, over-window turns in `_transcript_units`), plus a final pass before
  pieces are built (`unit-over-window(split)`). A single word longer than the window stays,
  warned `unit-over-window(one-unsplittable-word)`; such a document still takes the upload route.
- **Latent bug found and fixed on the fix path:** `_split_run` used `_TERMINAL.match(word)`,
  which matches at the START of a word, so its sentence-boundary branch never fired and
  over-budget blocks were cut mid-sentence. Now `.search`. **Second `_TERMINAL.match` use at
  `structure_chunker.py:254` (heading detection) is reported, not touched** — likely wants
  `.search` too, but it is outside the brief.
- **`uniform_label_speaker_set(lines)`** (`app/transcript_prep.py`): every non-empty line opens
  with a paren label, labels ≤2 words / ≤24 chars, ≥2 distinct, no stage marker, bodies ≥300
  chars per line. The chunker's `_transcript_like` uniform bar is now *gated on this one rule*
  (`_scan_turns` returns `uniform_ok`) instead of keeping its own copy — the copy was accepting
  documents the rule had refused.
- **Stage markers and rotations:** `speaker_set_from_alternation` filters `_STAGE_MARKER_WORDS`
  out of the label sequence *before* the period-two test; it no longer rejects a set merely
  because a marker appears in the file. Markers-only files are still not speakers. Three-voice
  rotations (Ana/They/We) are NOT rescued — the rule is two-voice; say so if asked.

## Control harness

`/tmp/rotation_controls.py` — snapshot the six touched files, revert one behaviour, run the
victim test file, restore, sha256-check the tree. Last run: 10 controls, **8 RED**, tree
restored identically. Two GREEN and the reason is honest, not a test gap: reverting the
transcript turn split to `_split_run`, and deleting the final invariant pass, both leave the
product outcome unchanged because the other layer catches the same body (defence in depth).
Earlier harnesses: `/tmp/bgem3_controls.py` (25 controls, all RED).

## Open threads to name if asked

1. **Lead's call:** `transcript_prep`'s long-standing bars read a capitalised key/value list
   (`Vendor: depot 0. Cost: 1200.`) as a one-voice transcript. Reported, not fixed.
2. **`structure_chunker.py:254`** `_TERMINAL.match` in heading detection — see above.
3. **poppler-utils** as a Containerfile/image dependency (lead escalates).
4. **Unverified offline:** served BGE-M3 topic behaviour (threshold 0.30 came from synthetic
   vocabularies — a real run should print the depth distribution before it is moved), the real
   249,997-entry tokenizer vocabulary, the `python-docx` / `pypdf` pins, and LightRAG 1.5.7's
   behaviour on a piece over `chunk_token_size` under `split_by_character_only`.
5. `docs/10-printable-handbook.md` still says `lightrag:v1.4.15` vs `v1.5.7` elsewhere —
   pre-existing docs inconsistency, untouched.

## Discipline reminders for the next row

Claim before starting. Stay inside the brief; report side-findings instead of fixing them.
New tests must be shown RED on pre-change code. Assert a *product* property in tests, and check
your own fixture actually exercises the guard (two of my controls were green because a second
layer — `_leading_parenthetical`/`_classify_paren_label`, or the last-resort pass — produced the
same outcome). Patch files with the `edit` tool or exact `str.replace` with a count assert;
heredoc scripts that guess at file text keep aborting. Never push. Never let a test open a
socket to 9621/8020. `COMPANY_AWARE_DEDUPE` stays off. Before compact/restart: rewrite this file.
