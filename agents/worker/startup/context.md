# worker seat — durable context

Last updated: 2026-10-21 07:50Z. **Current row `qitem-20261021073708-b5cb3ead` (docs/05 defaults)
is handed off** — successor `qitem-20261021074501-050f5ab8` carries the full report as its body
(use `--body-file` on handoff; that is what the lead now expects). Worktree
`/home/maverick/work/graphraggle-docs05`, branch `worker/docs05` off `master` `39ce48b`, **uncommitted**:
`docs/05-known-good-config.md` (+22/−3), `docs/08-change-log.md` (+26), new
`tests/test_docs_defaults_match_config.py`. Suite there: **1189 passed, 2 warnings, 248 subtests**
(baseline at `39ce48b`: 1186/245).

Earlier work **is integrated**: the lead committed my chunker fixes as `f7051e6` and master has
moved on since (`39ce48b` = DRAFT Hermes briefs; `c8480d9` = rig spec/agent specs). Nothing of mine
is uncommitted outside the docs05 worktree. **Nothing else in my queue; do not invent work.**

## Queue state

`rig queue list --owned` → `[]`. Active rows are the lead's: `qitem-20261021074501-050f5ab8`
(my docs/05 hand-back, body = my report) and `qitem-recovery-45db4492066282a3` (OpenRig recovery,
not mine). Do not invent work; wait for a row from `orch-lead@graphraggle` or for the operator.
Watchdog `parked-owner-consumer` (job `01M4AYEG0WK885GGJPDGRNVGD5`) is daemon-registered, not mine
to cancel.

**OpenRig friction, still unresolved:** `rig send orch-lead@graphraggle` and every queue nudge are
refused — `Refused: The bound foreground names a different Claude conversation. No text was sent.`
The lead seat is alive and working (`rig capture` shows it committing at 14:51), and the wake ladder
escalated the stuck baton back to me. Root cause is the lead seat's session binding, i.e. OpenRig
harness state my brief put off-limits (`agents/`, `rig.yaml`) — the operator has to reseat it. Use
`rig capture <seat>` + the queue as ground truth instead of `rig send` while it is broken. If the
same escalation repeats, re-check ground (`git log`, `rig capture`), annotate the stale row, close
the escalation as `no-follow-on`, and tell the operator — do not retry the wake.

**Handoff mechanics learned (do this next time):** `rig queue handoff` closes the source row and
creates a NEW row for `--to`; if you pass no `--body`/`--body-file` the successor keeps the
**source body** (the lead's brief), and `--note` is recorded only on the **source** row's
transition log — the lead never sees it on their own row. So either pass `--body-file` with the
hand-back report, or follow the handoff with `rig queue update <successor-id> --note "<report>"`
(no `--body-file` variant on `update`). I did the latter for both rows this time.

## Where the work lives (changed this item)

- `context-chunking` is merged into `master` (`a9138dd`, pushed). The old worktrees
  `graphraggle-chunking` / `graphraggle-ctx` and their branches are **gone**.
- My worktree now: `/home/maverick/work/graphraggle-fixes`, branch `worker/chunker-fixes`,
  based on `master` at `a9138dd`. **Uncommitted** (the lead commits; I never push):
  `app/structure_chunker.py` (1 line), `app/transcript_prep.py` (+68/-1),
  `tests/test_structure_chunker.py` (+32), `tests/test_transcript_prep.py` (+65),
  `docs/08-change-log.md` (+34, added by repo convention — not asked for in the brief, the lead
  may drop it).
- Follow-up round (the lead reviewed that diff and found one bug): `_FIELD_KEY_LINE` was a plain
  `r"..."`, so `{_TS}` stayed literal and a bracketed timestamp prefix never matched. Now two
  `rf"..."` fragments with `{{1,30}}` escaped; the optional prefix follows `_TURN_PATTERNS`
  (`[hh:mm(:ss][.ms]`, bare `hh:mm`, or `>>`). New test
  `test_a_timestamped_key_value_list_is_named_for_its_field_keys` (+6 subtests pinning the line
  shapes). Isolated the slip to check the lead's diagnosis: with ONLY that slip the timestamped
  list is still left byte-identical but reports `no-speaker-labels` — a diagnosis bug, not a data
  bug. Repo-wide scan (35 files, `app/` + `scripts/`) found no other raw non-f string carrying a
  `{_PLACEHOLDER}`; nothing else changed for it.
- Suite after the follow-up: **1186 passed, 2 warnings, 245 subtests in 6.6s**.
- Tests (bare env, from the worktree):
  `timeout 900 env -i PATH=$PATH HOME=$HOME /home/maverick/work/graphraggle/.venv/bin/python -m pytest tests/ -q`
  → **1186 passed, 2 warnings, 245 subtests in 6.6s** (1185/239 before the follow-up test).
  Baseline at `a9138dd` before any of my fixes:
  **1179 passed, 2 warnings, 236 subtests**.
- pyflakes clean: `PYTHONPATH=/tmp/pyflakes_lib /home/maverick/work/graphraggle/.venv/bin/python -m pyflakes <files>`.
- Venv is the main checkout's: `/home/maverick/work/graphraggle/.venv/bin/python` (3.14). Missing:
  tiktoken, pypdf, python-docx, transformers, tokenizers, sentencepiece, numpy. PRESENT:
  python-pptx, httpx, `pdftotext`.

## What this item produced

1. `app/structure_chunker.py:268` `_is_all_caps_heading`: `_TERMINAL.match` → `.search`.
   `_TERMINAL = [.!?\u2026]$`, so `.match` only ever succeeds on a one-character string; an
   all-caps sentence became a heading and opened a section (`Section: FREIGHT COSTS ROSE IN
   MARCH.` in every header under it). Tests: `test_an_all_caps_line_ending_in_a_full_stop_is_not_a_heading`,
   `test_an_all_caps_sentence_does_not_open_a_section` (fixture asserts the bad path at budgets
   60/90/120). **No existing expectation shifted** — full suite green with 0 deleted test lines.
2. `app/transcript_prep.py` — a capitalised key/value list (`Vendor: depot 0.` ×N) was detected
   as a transcript (the `single_voice` gate accepts one label heading every line). Two rules:
   - `_FIELD_KEY_LABELS` (rejected in `_clean_name`, same family that already refused `Note:`,
     `Q:`/`A:`): attribute nouns (vendor, contact, status, price, quantity, model, serial, depot,
     tariff …). Human **roles** deliberately excluded — `Chair:`/`Witness:`/`Counsel:` are real
     speakers in minutes and depositions.
   - structural: `label_order` runs → `returns_to_a_voice`; with `bodies_are_values` (median
     labelled body ≤ `_LIST_MAX_MEDIAN_VALUE_WORDS` = 6 words) → `field_list_shape` blocks
     `labelled_transcript` only (never `single_voice`, so the one-voice recording test stands).
   - Reasons name the cause: `field-key-labels(n/lines)` when nothing parsed, `key-value-list(labels/lines)`
     when the shape is a grouped list. Counts only, never words from the file.
   Behaviour map I measured (worth re-deriving only if the rule is revisited): single-key list,
   grouped known keys, grouped unknown keys, record-per-line **known** keys → all `applied=False`;
   record-per-line **unknown** keys → still detected (structurally a conversation); two voices in
   one block each with long turns → detected; two voices in one block each with **terse** turns →
   now untouched (the honest false negative of the structural rule); one-voice recording → detected.
   Tests: `test_a_key_value_list_is_left_untouched` (3 subtests),
   `test_a_grouped_field_list_is_named_as_a_list_even_when_repeated`,
   `test_field_key_labels_are_counted_for_the_diagnosis`,
   `test_a_voice_that_returns_after_another_still_makes_a_transcript` (regression guard, green
   before the fix too).

Red before the fixes (proved by `git stash push -- app/...` and re-running): 2 structure-chunker
tests; `test_a_key_value_list_is_left_untouched` (3 subtests),
`test_field_key_labels_are_counted_for_the_diagnosis`,
`test_a_grouped_field_list_is_named_as_a_list_even_when_repeated`.

## Not verified / open

- I could not check the **corpus** shape (brief forbade reading corpus/probe/staging/LightRAG
  data): whether the file that triggered this was single-key, grouped or record-per-line, and
  whether its field names are in `_FIELD_KEY_LABELS`. A next stack run shows it: the skip reason
  is now `field-key-labels(...)` or `key-value-list(...)` instead of `detected`.
- No sandbox/stack run for these two fixes (offline only); no fixture from the real image needed.
- Still-standing from earlier items (do not re-litigate): BGE-M3 the only embedder, window 8192,
  merge floor ~150 tokens not a cap; printable markers `<<<CHUNK-BREAK>>>` + blank lines, payload
  is a join (N pieces → N-1 markers); `chunk_token_size` in LightRAG's counter, never BGE-M3
  tokens; structure chunker stays pure; tests never open a socket to 9621/8020.
- Open threads to name if asked: (a) ask the next stack run for `structure_chunking_sanitiser_check`
  (expect `ok`), `structure_chunking_chunk_token_payload_gap`, `structure_chunking_html_entities`;
  (b) sanitiser fixture gap — step 1 (`strip` before unescape) is not pinned by the 62 strings;
  (c) `poppler-utils` — the lead says it is already in the Containerfile; (d) unverified offline:
  tiktoken in the served image, LightRAG's behaviour on a piece over `chunk_token_size` under
  `split_by_character_only`, the real 249,997-entry BGE-M3 vocabulary, `python-docx`/`pypdf` pins.

## Discipline reminders for the next row

Claim before starting. Stay inside the brief; report side-findings, don't fix them unasked.
Prove red-before-green (`git stash push -- <app file>`, run, `git stash pop`) — the stash round
trip also gives the honest baseline count, **but a stash reverts the whole file, so it proves more
than the one bug you are pinning**: to isolate a single slip, copy the file to /tmp, patch just
that line back, run, restore, and `cmp` to prove the restore. Watch raw strings carrying `{...}`:
`r"…{_TS}…"` silently becomes literal braces — house style is `rf"…"` with `{{1,30}}` for real
quantifiers. A fixture/oracle beats hand-derivation. Re-read the
file region before patching; write prose with quoted heredocs (`<<'MD'`) and re-scan for control
characters. Check `git diff --numstat` to prove no existing test expectation was edited.
Never push. `COMPANY_AWARE_DEDUPE` off. Before compact/restart: rewrite this file.
