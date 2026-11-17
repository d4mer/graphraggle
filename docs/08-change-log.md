# Change Log

## Important Fixes Discovered During Setup

1. Correct LightRAG image tag is:

```yaml
ghcr.io/hkuds/lightrag:v1.5.7   # was v1.4.15; production moved on 2026-10-09 and
                                # compose.yml was reconciled on 2026-10-11
```

2. Correct embedding model is:

```bash
EMBEDDING_MODEL=mxbai-embed-large-v1   # SUPERSEDED 2026-10-18: see the BGE-M3 entry below
```

3. Text files must ingest via LightRAG `POST /documents/text`

4. Binary/office files should continue using `POST /documents/upload`

5. Ignore `source_docs/__enqueued__` in the worker

6. Uploads should remain in `uploads/`

7. `gateway-api` and `ingest-worker` must use local build config and `pull_policy: never`

8. LightRAG container command must pass flags only, not `lightrag-server` again

9. Gateway-to-LightRAG networking was validated internally using:

```bash
podman exec rag-gateway-api curl -i http://lightrag-server:9621/health
```

10. Final smoke test validated full retrieval and citations from `rag-smoke.txt`

## 2026-10-11 — LightRAG input directory (production fix, now carried in `compose.yml`)

Production was fixed so Office/PDF files ingest on LightRAG v1.5.7; the repo
`compose.yml` now matches it so the next deploy does not undo it.

1. **Root cause.** LightRAG uses two settings for one directory: the **`--input-dir`
   CLI argument** (where `/documents/upload` writes) and the **`INPUT_DIR` environment
   variable** (where the parser looks the file up; image default `/app/data/inputs`).
   They were different, so every binary upload parsed as:
   `Parse worker failed (native): legacy source file not found`.
2. **Second cause.** The input dir also overlapped the folder the ingest worker scans
   (`source_docs/`), so uploading a file that is already there returned:
   `HTTP 409 — Input directory already contains a file with the same canonical basename`.
3. **Fix.** LightRAG gets an input dir of its own, with `--input-dir` and `INPUT_DIR`
   equal to `/data/lightrag_inputs`, mounted from `./lightrag_inputs`.
   `./source_docs:/data/source_docs` stays mounted. Canary `.docx` and `.pdf` both
   ingested afterwards.
4. **Operator requirements.** `lightrag_inputs/` must **exist on the host and be
   world-writable (`777`)** before the first start (`mkdir -p lightrag_inputs && chmod
   777 lightrag_inputs`). It is gitignored. **Removing/recreating that host directory
   leaves the bind mount stale — restart `lightrag-server` after doing it.**
5. **Ports/images now in `compose.yml`:** LightRAG `v1.5.7` on host `9621` (was
   `v1.4.15` / `9622`), gateway host `8020` (was `8000`), Open WebUI `0.11.4` on host
   `3010` (was `main` / `3000`). *(Open WebUI has since moved: `compose.yml` pins
   `0.12.0` from **2026-10-14** — see the release entry below; the `0.11.4` above is
   what that 2026-10-11 cutover recorded.)*
6. **Secrets.** Open WebUI's HyperQwen key is parameterised:
   `OPENAI_API_KEYS: "${OPENWEBUI_OPENAI_API_KEYS:-}"`. Set **`OPENWEBUI_OPENAI_API_KEYS`**
   in the host `.env`; no key value ever goes in the repo. There is no `.env.example`.
7. **Guard test:** `tests/test_compose_config.py` fails if `--input-dir` and `INPUT_DIR`
   ever diverge, if the input dir becomes the worker-scanned `source_docs` dir, if an
   image tag drifts to `latest`/`main`, or if a literal key value appears under any
   `*KEY*` environment entry.

## Packet-24 — OpenWebUI task prompts are capped before forwarding

1. `BRIDGE_TASK_MAX_CHARS` (default `0` = off; set e.g. `24000` to enable) bounds the `### Task:` prompt the bridge forwards to LightRAG `mode=bypass`, where no retrieval truncation runs. When enabled the bridge keeps the first 25% of the budget (the task header) and the last 75% (the most recent chat turns), joined by `[...truncated...]`. The bridge log line always gains `prompt_chars`, plus `forwarded_chars` on task requests — lengths only, never prompt text, and they are on whether or not the cap is.

## Production Releases

All deploys are on the Thinkpad (`~/rag-project`, Docker). Rollback is always
`docker tag local/rag-gateway:<rollback-tag> local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`.

1. **2026-10-07 — packet-20** (gateway-supplied high/low-level keywords) deployed.
2. **2026-10-07 — packet-21** (standalone-query rewrite of follow-ups before retrieval) deployed.
3. **2026-10-08 — packet-23** (one retry when `/query/stream` returns an empty answer) deployed. Rollback tag `local/rag-gateway:pre-packet-23` = `31395c52e22e`.
4. **2026-10-09 — packet-24** (`prompt_chars` / `forwarded_chars` bridge logging; task-prompt cap shipped **OFF**, `BRIDGE_TASK_MAX_CHARS=0`) deployed as image `local/rag-gateway:p24` (`4629dcab0bea`, built from `c89b293`). Rollback tag `local/rag-gateway:pre-packet-24` = `68c1631d7dd3`.
5. **2026-10-09 — reboot drill:** all 20 containers on the host came back by themselves ~1 min after boot with 0 restarts; the Hermes agent seat does not come back and must be relaunched (see `11-first-5-minutes-after-reboot.md`).
6. **2026-10-09 — backup/restore drill:** SQLite online-backup API for `state/ingest.db` + quiescent copy of `lightrag_store/` (218 MB, 3 s) + sha256 manifest; restore verified (0 manifest mismatches, 272 documents, 12 store files equal). Backups in `~/rag-project/backups/`.
7. **2026-10-09 — rc1 quality baseline** (36 probes × 3 draws, automatic scoring): single-fact 1.00, multi-fact 0.83, follow-up 0.89, cross-document 0.56, unanswerable-refusal 0.67; 0 empty answers; median latency 178 s.
8. **2026-10-14 — Open WebUI `0.11.4` → `0.12.0`** deployed on the Thinkpad: canary on a data copy, then cutover. DB migrated (6 revisions), **downgrade unsupported**; rollback = restore the backup tarball. Bridge contract unchanged (task short-circuit verified). `compose.yml` and `05-known-good-config.md` now pin `0.12.0` so the next deploy does not downgrade production.
9. **2026-10-15 — rc-2 deployed.** Tag `rc-2` = commit `11b1833`. Both `rag-gateway-api` and `rag-ingest-worker` now run `local/rag-gateway:rc2` (image `d0f31398fec5`), built from the lead's git-archive tarball (23 files, sha256-verified). The previous image `4629dcab0bea` (`p24` = rc-1) is kept as `local/rag-gateway:pre-rc2`. No `.env` change; `COMPANY_AWARE_DEDUPE` stays absent, so it remains **off**.
   What is now live in the worker and the gateway: TODO #2 (lifecycle transition table + compare-and-set write-backs + the CAS refused-write log), TODO #3 (one duplicate rule shared by upload and scan, always-on skip rules), packet-27 (`python-pptx` text extraction with a pure-stdlib fallback), and the `tests/conftest.py` test-environment defaults. Packet-18's dedupe hardening was **already** live — the worker has run rc-1 (`4629dcab0bea`) since 2026-10-10, so the docs' earlier claim that it sat on a pre-packet-18 build (`d9d9ce2307a2`) was wrong and is corrected in `05-known-good-config.md`.
   State DB backup taken before the release: `~/rag-project/backups/pre-rc2-20261015T083456Z/`. Acceptance checks all passed; a 5-minute observation window after the cutover logged 0 errors and 0 refused writes, with document status counts unchanged (10 `ingested`).
   Rollback — gateway: `docker tag local/rag-gateway:pre-rc2 local/rag-gateway:latest && docker compose up -d --no-deps gateway-api`. Worker (needs an override file, because the worker is not retagged separately): `docker compose -f compose.yml -f compose.rollback-rc2.yml up -d --no-deps ingest-worker`. Note: **`compose.rollback-rc2.yml` exists only on the Thinkpad** — this repo contains just `compose.yml`, so the worker rollback path currently depends on a file that is not under version control. Flagged, not changed: committing it (or the `pre-rc2` tag convention) is an operator/lead call.
   Recorded with this release: the live Thinkpad `compose.yml` inlines the Open WebUI OpenAI key while the repo keeps `${OPENWEBUI_OPENAI_API_KEYS:-}`. The operator has **accepted** that difference as a risk (single-user machine on a LAN); no rotation is scheduled. No key value belongs in this repo.

### Pending

1. ~~`rag-ingest-worker` is still running image `d9d9ce2307a2`, an old build from before packet-18.~~ **Corrected and resolved 2026-10-15:** that premise was stale — the worker has run rc-1 (`4629dcab0bea`) since 2026-10-10, and both gateway and worker now run rc-2 (`d0f31398fec5`).
2. ~~The repo `compose.yml` still pins `ghcr.io/hkuds/lightrag:v1.4.15` and maps LightRAG on host port `9622`.~~ **Resolved 2026-10-11 (packet-26):** `compose.yml` now pins `v1.5.7`, maps `9621:9621`, gives LightRAG its own `INPUT_DIR`/`--input-dir`, and is covered by `tests/test_compose_config.py`.
3. `BRIDGE_REWRITE_SEED_LL` (GRAG-41 seed) is off and undeployed.
4. ~~Repo changes not yet deployed (worker image still pre-packet-18): TODO #2, TODO #3, `COMPANY_AWARE_DEDUPE`, `tests/conftest.py`.~~ **Resolved 2026-10-15 by rc-2**, except that `COMPANY_AWARE_DEDUPE` is deliberately still off (see `05-known-good-config.md`, *Settings Shipped Dark*): the 2026-10-14 probe showed LightRAG already dedupes identical bytes globally, so that flag would only split duplicate accounting, not make duplicates queryable.

## 2026-10-18 — structure-aware ingest reaches PDF, DOCX and PPTX (flag-gated)

`STRUCTURE_CHUNKING_ENABLED` no longer covers only the text path. When it is on, the worker
extracts PDF (`pdftotext -layout` first, `pypdf` as fallback), DOCX (`python-docx`, with a
stdlib OOXML reader as fallback) and PPTX (already extracted) and submits them through
`POST /documents/text` with the `chunking` object, so `app/structure_chunker.py` chooses the
cuts and LightRAG is told to cut only there. Table rows arrive with the header row repeated;
PDF rows that the text layer split across 3-8 physical lines are reassembled into one row
before chunking; repeated page headers are dropped and counted.

- **This deliberately amends setup rule 4 above** ("binary/office files should continue using
  `POST /documents/upload`") for PDF/DOCX, and only while the flag is on. Flag off: the
  request body, the route and the state row are what they were before. The dedupe hash is
  still `sha256` of the original file bytes, so re-chunking can never change identity.
- Row shape change: `app/pptx_extract.py` now emits canonical pipe rows (`| a | b | c |`,
  leading and trailing bar). `app/structure_chunker.py` reads a leading bar as a table; a
  mid-line-only `a | b` reads as prose and loses the header-repetition protection.
- New dependencies in `requirements.txt`: `python-docx==1.1.2`, `pypdf==5.3.0`. Both are
  optional at run time (see fallbacks above). The pins were chosen without network access:
  confirm they resolve on the next image build.
- New settings are listed in `docs/ops/loop/README.md`. Every failure mode falls back to
  today's route with the reason on the state row.
- `scripts/chunk_embed_bench.py` gained `--embed-tokenizer` (the served embedder's own
  window test, local files only, never downloaded), `--text-copies-dir`, `--budgets`
  (`recall@T` by tokens) and the `LOST TEXT` gate (exit 4 for a structure arm that dropped
  text, `--allow-lost-text` to override). Probe fields `source_kind`, `row_key`, `row_value`
  drive the source-kind split and `row_intact%`.
- Not verified: `chunk_token_size` ≥ largest piece is asserted per document, but what
  LightRAG 1.5.7 does with a piece larger than `chunk_token_size` under
  `split_by_character_only` is still an unknown (Hermes to confirm from the image source).

## 2026-10-18 — BGE-M3 is the only embedder; chunk size has no cap, the window does

mxbai-embed-large-v1 is retired from defaults, docs, tests and the benchmark configs. BGE-M3
(`bge-m3-mlx-fp16`, dense 1024-dim, real window 8192 tokens) is the only embedder the stack
names. The older 512-token window is what made 1200-token chunks fail silently, so the
configuration is stated in terms of the window instead of a size target.

- **No chunk-size cap.** `STRUCTURE_CHUNKING_MAX_PIECE_TOKENS` (7500 = window − margin) is the
  only ceiling and the only reason to cut. A piece is a natural unit — a whole section, a whole
  table, a run of turns on one topic — split only when it cannot fit the window, and then at its
  best internal boundary (sub-heading → paragraph → table row with the header repeated → turn →
  sentence), never at a token count. `chunk_token_size` sent to LightRAG is
  `largest piece + margin`, asserted per document, upload fallback otherwise.
- **Merge floor, not a target.** `STRUCTURE_CHUNKING_MIN_PIECE_TOKENS` (150) welds a too-small
  unit to its neighbour, which is what fixes the 596-piece PDF without limiting the maximum. A
  heading never stands alone; a section heading rides on its first content, including before a
  large table. `STRUCTURE_CHUNKING_MERGE_UNTIL=floor` (default) keeps one natural unit per
  piece; `window` chains small neighbours to the window and is the benchmark's comparison knob.
- **Topic-shift segmentation** for flat text and transcripts (`STRUCTURE_CHUNKING_EMBED_URL`):
  windows of turns/sentences embedded with BGE-M3, cut at TextTiling depth minima. The call
  lives in `app/topic_embedder.py` — batched, bounded per document, cached by
  sha256(model+text), and it refuses the LightRAG (9621) and gateway (8020) ports. With no URL,
  or an unreachable server, the deterministic fallback cuts at unit boundaries near the window
  and `structure_chunking_topic_method` records which happened.
- **Transcript labels found by repeated behaviour, not line count**: `(Name)`, `(Name):`,
  `Name 12:34`, bracketed timestamps, and labels inside one long line (a monolithic minutes
  file is split into turns before anything is counted). A parenthesised label that alternates
  strictly with one other label — or heads two enormous lines — is a speaker even when the word
  is a pronoun (`(They)` / `(You)` across 40 lines), which is the shape five real transcripts
  were using. Stage directions are never rescued. A turn longer than the window is split inside
  the turn.
- **PDF backend is recorded** as `structure_extract_backend` (`pdftotext-layout` or `pypdf`),
  because row reassembly depends on the column positions only `pdftotext -layout` reproduces
  (Hermes' run: 803 rows rejoined with poppler, 0 with pypdf). `poppler-utils` is therefore a
  Containerfile/image dependency worth pinning — escalated to the lead.
- **`app/embed_tokenizer.py` reads BGE-M3's own files** (`tokenizer.json`,
  `tokenizer_config.json`, `special_tokens_map.json`): Unigram vocabulary, `added_tokens`
  specials, the XLM-R pre-tokenizer punctuation split, and byte fallback, so a character the
  vocabulary cannot represent costs its three UTF-8 byte tokens instead of one — the case most
  likely to be silently truncated. Local files only; nothing is ever downloaded.
- Benchmark configs are BGE-M3 only: `fixed-350-50` and `fixed-1200-100` as references, and
  `structure` arms at 700/1500 with headers true/false/shuffled. The mxbai arms are gone.
- Not verified offline: real BGE-M3 similarity behaviour (the threshold 0.30 was set on synthetic
  vocabularies, so the first real run should print the depth distribution before it is moved);
  the topic pass against the served oMLX embedder; `tokenizer.json` handling against the real
  249,997-entry BGE-M3 vocabulary (synthetic XLM-R-shaped files only — Hermes' load of the real
  files reported `unigram-249997-entry-local+2special`); and what LightRAG does with an oversize
  piece under `split_by_character_only`.

## 2026-10-19 — no structure-path piece is ever over the window; two-line and marker-broken transcripts are read

Hermes' ingest lost two documents to the structure path itself: a flat document arrived as one
unit, the topic pass found no topic change inside it and returned it whole, and the fit check
then sent the file back to LightRAG's default chunker — a worse cut *and* no structure. Three
fixes, all in `app/structure_chunker.py` / `app/transcript_prep.py`:

- **An over-window unit is always split, whatever produced it.** Every path that can hand back a
  body bigger than the window now goes through `_split_oversize`: the flat/topic path, the
  paragraph-block path, and an over-window turn inside a transcript. A final pass before pieces
  are built catches any unit that still exceeds it and records `unit-over-window(split)`. Pieces
  are **balanced**: a tail leftover is welded sideways into its neighbour while the neighbour
  still fits, so the splitter never emits "one full piece plus a stub". The one thing it will
  not do is invent text — a single word longer than the window stays, warned as
  `unit-over-window(one-unsplittable-word)`, and the document goes to the upload route as before.
- **A bug in the splitter's own sentence detection:** `_TERMINAL.match(word)` matches at the
  **start** of a word, so the branch that prefers sentence boundaries never fired on real text.
  Every over-budget block was being cut at whitespace *inside a sentence*. It is `search` now,
  which is what the other two call sites already used.
- **Two-line meetings are transcripts.** `uniform_label_speaker_set` accepts a file when every
  non-empty line opens with a short parenthetical label, at least two distinct labels appear, no
  label is a stage direction, and each label has a body of real length. That is shape, not
  repetition, so it catches a call written as `(They) …6,000 tokens…` and `(You) …6,000 tokens…`
  where nothing repeats. The label has to be name-shaped (≤2 words, ≤24 characters): a heading
  followed by a parenthesised paragraph, and a contents page of short paren labels, stay prose.
- **A stage direction no longer breaks a speaker rotation.** `(Ana) … (They) … (Cross-talk) …`
  used to fail the period-two test because the marker line interrupted the pattern. Markers are
  dropped from the sequence *before* the pattern is read, so the rotation is still recognised —
  and a file of only stage directions is still not a transcript.
- Tests: 21 new cases in `tests/test_structure_chunker.py` (window invariant across every kind,
  balanced splits, whole-word hard split of a sentence-less blob, unsplittable-word reporting,
  two-turn monoliths and their false positives, marker-broken rotations and the three-voice
  limit). Suite: **1107 passed, 166 subtests**.

## 2026-10-19 — a piece below the merge floor is welded, and an oversize split comes out even

Hermes' chunkrun6 rerun applied to 8 of 10 documents with no over-window pieces, but doc01 (the
two-turn monolith) ended in a 28-token piece: a trailing turn the turn packer could not absorb
because its neighbour was already full, so it stood alone with a context header costing more than
its content. Two fixes, both in `app/structure_chunker.py`:

- **The merge floor now applies to the finished document, not only inside one unit.** After every
  cut is decided, any piece below `STRUCTURE_CHUNKING_MIN_PIECE_TOKENS` (150) that is not the
  document's only piece is welded into the **previous** piece — a document reads forward — or the
  next one when there is no previous. It covers every kind, including a trailing standalone turn
  and a label-only line. If both joins would push past the window the piece is left alone and the
  row says so: warning `stub-piece-cannot-merge`, stats `stub_pieces_merged` / `stub_pieces_left`.
  Nothing is ever dropped to make a piece fit.
- **An oversize split is balanced.** `_split_oversize` used to fill to the window and dump the
  remainder, so a monolithic turn came out `13,500 / 13,500 / 3,700` characters: two pieces a
  reader can use and one orphan. `_rebalance_parts` re-places the boundaries at the sentence nearest
  each k-th of the total, so the pieces are roughly equal **and the piece count is a promise, not an
  outcome** — a document that split evenly keeps exactly the pieces it had (the chunkrun7 benchmark
  build compares counts, and an unnecessary move there would be a regression).
- **Where the two rules hand over.** A part at or above the merge floor that is far smaller than its
  neighbours is a badly drawn boundary → equalise it. A part *below* the floor is the stub question
  → the document rule answers it (weld, or leave and warn). Letting the rebalancer handle the
  below-floor case instead pushes every piece well under the window and costs retrieval capacity:
  that variant is caught by the benchmark's coverage gate, which is why the handover is explicit and
  controlled.
- Tests: 10 new cases in `tests/test_structure_chunker.py` (trailing stub turn, leading stub,
  label-only trailing line, a stub whose every join would break the window, previous-then-next
  neighbour preference, a one-piece document keeps its small piece, a no-stub-under-the-floor sweep
  across kinds, equal-thirds balancing, an already-even split left byte-identical, balance holding at
  every window that forces a split). Suite: **1117 passed, 174 subtests**.
- Verified unchanged, piece counts and sizes identical: the 19k/56k/130k-token flat documents, the
  flat document with the topic pass on, the sentence-less blob, the 7,500-window two-turn monolith,
  the 40-section headed document, the 1,200-row table, and the name+pronoun rotation. Only
  stub-bearing shapes and one runt-bearing split moved.

## 2026-10-19 — the cut marker survives LightRAG's input sanitiser (stack2 post-mortem)

Ten documents went through the structure path in the stack2 run and eight arrived as **one
segment**. The cause was ours: the piece delimiter was `\n\n\x1e\n\n`, and LightRAG sanitises
the whole document field (`sanitize_text_for_encoding` → `strip_control_characters`) **before**
it splits anything. `\x1e` is in the C0 range it deletes, so the marker vanished, the pieces
joined into one block, and under `split_by_character_only` the result was either one whole
document or `ChunkTokenLimitExceededError`. A ninth document, doc08, failed on
`chunk_token_size` 2579 against a segment LightRAG counted at 2592: the size was measured in
the wrong tokenizer family.

Fixed in `app/`:

- **`app/lightrag_text.py` (new)** — the offline stand-in for that pipeline:
  `normalise_for_lightrag` (C0 except tab/newline, DEL, lone surrogates, zero-width and format
  characters, CRLF folding), `sanitize_like_lightrag`, `stripped_characters`,
  `is_survivable_delimiter`, `is_conservative_marker`, and `pieces_survive`, which replays
  sanitise → split → compare against the plan.
- **Printable markers only.** `DELIMITER_CANDIDATES` is now
  `\n\n<<<CHUNK-BREAK>>>\n\n` and three longer siblings. When every candidate already sits in
  the text, the picker escalates to a marker built from the document's own sha256 — same input,
  same marker, every run. An explicit delimiter made of characters the pipeline strips is
  **refused instead of obeyed**.
- **The payload is a join, not a wrap.** No marker at either end: the field is stripped before
  it is split, and an edge marker loses its newlines, which leaves a payload that no longer
  contains the delimiter it named. `N` pieces carry `N-1` markers.
- **The plan is stated over the stored text.** `chunk_document` normalises on entry, so a form
  feed from a page break cannot sit in a piece and disappear on the way in. Which characters
  went, and how many, is recorded: `structure_chunking_stripped_chars` on the state row.
- **Four checks before submission**, any one of which falls back to today's route: marker count
  (`N-1`), payload sizing, the survival replay (`structure_chunking_sanitiser_check`), and the
  embedder-window check.
- **`chunk_token_size` is measured in LightRAG's tokenizer family** — tiktoken `cl100k_base`
  when installed, otherwise the chunker's documented conservative over-count — never in BGE-M3
  tokens, and then **re-measured against the payload's own segments**, which is what the image
  will count. The basis is recorded as `structure_chunking_chunk_token_basis`; the embedder count
  stays where it belongs, on the 8192-token window.

Tests: `tests/test_lightrag_text.py` (14) plus delimiter, sizing and worker replay cases in
`tests/test_structure_chunker.py`; the shape-invariant helper now replays the sanitiser for
**every** fixture at three budgets. Suite: 1148 passed, 174 subtests. Eleven sabotage controls,
all red, tree restored byte-identical.

## 2026-10-20 — the sanitiser imitation is now a parity implementation (golden fixture)

Hermes ran the real `lightrag.utils.sanitize_text_for_encoding` inside `lightrag-server2` over
62 fixed strings and compared it with `sanitize_like_lightrag`: **18 of 62 differed**. Attempt 2
of the ingest brief stopped at that gate, as designed, and nothing was ingested. The imitation
had been written from the documented behaviour, and the documentation was not the function.

What the image actually does, in order (`utils.py:5529-5566`):

1. `text.strip()` — the whole field
2. `html.unescape(text)` — **one** pass (`&amp;lt;` → `&lt;`)
3. delete lone surrogates and `U+FFFE`/`U+FFFF`
4. delete C0 except `\t` `\n` **and `\r`**, plus DEL
5. `text.strip()`

So the earlier imitation was wrong in both directions: it folded CR (the image keeps every
carriage return) and it deleted zero-width/format characters (the image keeps all of them,
including the 46 `U+200B` in `OrderBook.docx`), and it never expanded entities.

- `app/lightrag_text.py` rewritten to that order. Parity is asserted over the fixture pair
  `tests/fixtures/sancmp_tests.json` + `sancmp_real.json` in `tests/test_sanitiser_parity.py`:
  **62/62 byte-identical** (inputs and outputs travel as hex; lone surrogates are decoded with
  `surrogatepass`).
- `normalise_for_lightrag` (plan time) applies the **deletions only** — never the unescape, which
  must run exactly once in the image, and never the field strip. Format characters stay in the
  plan because they stay in the stored chunk.
- Entities can **invent a cut**: `&#10;&#10;&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;&#10;&#10;`
  is not a marker in the file and is one after step 2. `pick_delimiter` now tests every candidate
  against the unescaped text as well, and `pieces_survive` refuses any payload whose delimiter
  count changes under unescaping — falling back to `/documents/upload` with
  `structure_chunking_reason` naming the entity hazard. `structure_chunking_html_entities` records
  how many expansions to expect.
- The replay compares **exactly**: `_stored_form` models the real function's strip at the
  payload's two outer edges only (first piece lstripped, last rstripped, interior untouched).

Suite: 1179 passed, 2 warnings, 236 subtests (62 of them the golden parity) in 6.8s. Controls:
`/tmp/parity_controls.py` — 15 sabotages, **13 RED**, 2 honestly green: removing the *leading*
field strip, and removing both strips, change nothing on the fixture — for this composition
`strip ∘ f` and `f ∘ strip` agree, verified over 9,261 generated strings, so the fixture cannot
pin step 1 on its own.

## 2026-10-20 — all-caps headings and key/value lists (two chunker defects)

- `app/structure_chunker.py` `_is_all_caps_heading` used `_TERMINAL.match(stripped)`. `_TERMINAL`
  is `[.!?\u2026]$`, so `.match` can only ever succeed on a one-character string: an all-caps
  **sentence** ("FREIGHT COSTS ROSE IN MARCH.") passed the terminal test and became a heading,
  opening a section and writing itself into every header of the pieces under it
  (`Section: FREIGHT COSTS ROSE IN MARCH.`). Now `.search`, as every other use of `_TERMINAL`
  already was. `"FREIGHT COSTS"` is still a heading; `"FREIGHT COSTS."` is not.
- `app/transcript_prep.py` read a capitalised key/value list as a transcript: `Vendor: depot 0.`
  ×N is one label heading every line, which is exactly what the `single_voice` gate accepts as a
  one-voice recording. Two rules, each with its own test:
  1. **Field keys are not speakers** (`_FIELD_KEY_LABELS`, rejected in `_clean_name`, the same
     family that already refused `Note:` and `Q:`/`A:`): attribute nouns — vendor, contact,
     status, price, quantity, model, serial, depot, tariff … Human **roles** are deliberately
     absent (`Chair:`, `Witness:`, `Counsel:` are speakers in minutes and depositions).
  2. **A conversation comes back to a voice; a list does not.** When every label's lines sit in
     one contiguous block and no label ever returns after another intervened, and the median
     labelled body is a value rather than speech (≤ 6 words), the document is left alone even if
     no lexicon knows the field names.
  Both report the cause: `field-key-labels(n/lines)` and `key-value-list(labels/lines)`.
  Trade-off, stated plainly: a grouped list whose field names are unknown AND whose lines
  alternate record-by-record is still read as a transcript (it is structurally a conversation),
  and a two-voice transcript where each voice speaks exactly once in one unbroken block of short
  turns is now left untouched. A field noun used as a form of address loses its turns.

Suite: 1185 passed, 2 warnings, 239 subtests in 6.8s (baseline before the two fixes: 1179 passed,
2 warnings, 236 subtests). Red before the fix:
`test_an_all_caps_line_ending_in_a_full_stop_is_not_a_heading`,
`test_an_all_caps_sentence_does_not_open_a_section`,
`test_a_key_value_list_is_left_untouched` (3 subtests),
`test_field_key_labels_are_counted_for_the_diagnosis`,
`test_a_grouped_field_list_is_named_as_a_list_even_when_repeated`. No existing expectation was
edited: the only pre-existing test touching the changed branch still passes as written.

## 2026-10-21 — docs/05 "defaults as shipped" corrected to what `app/config.py` actually defaults

`docs/05-known-good-config.md` documented `STRUCTURE_CHUNKING_ENABLED=true` and
`STRUCTURE_CHUNKING_EMBED_URL=http://192.168.1.190:1234/v1` in a block headed "defaults as
shipped". Both are wrong: `structure_chunking_enabled` defaults to **false** and
`structure_chunking_embed_url` to **empty** (empty = no topic pass, deterministic fallback). An
operator reading the doc would have believed the worker structure-chunks out of the box, and that
topic-shift cuts were running.

- The block is now labelled **code defaults as shipped**, with the source named
  (`app/config.py`, verified 2026-10-21), and the production values moved to a separate,
  explicitly labelled **"to turn it on"** block: `STRUCTURE_CHUNKING_ENABLED=true`,
  `WORKER_PDF_EXTRACT_ENABLED=true`, `WORKER_DOCX_EXTRACT_ENABLED=true`, plus the embed URL.
- Recorded in the same place: `WORKER_PPTX_EXTRACT_ENABLED` ships **true**, so with structure
  chunking on the worker already extracts `.pptx` itself, while `.pdf` and `.docx` stay on
  `/documents/upload` until their flags are set. That asymmetry is the easy one to get wrong.
- No other doc states a default for these flags wrongly (checked `docs/ops/loop/README.md`
  setting table, `docs/08-change-log.md`, the Hermes briefs, `packet-27-pptx-extract.md`,
  `README.md`, `scripts/README.md`, `compose.yml`). Only `docs/05` needed changing; **no code
  default was touched.**
- New `tests/test_docs_defaults_match_config.py` parses that block and compares it with the
  `Field(default=...)` of the matching `Settings` alias, so the block and the code cannot drift
  apart silently again. It was red before the doc fix - it named both mismatches.

Suite: 1189 passed, 2 warnings, 248 subtests (baseline 1186/245 at `39ce48b`).

## 2026-10-26 — retrieval is now graded, not just present-or-absent (`scripts/ranking_metrics.py`)

`evidence_in_context` answered one binary question, so the recall comparisons
(production 85, stack2 91, legacy+BGE-M3 83) could not say whether the evidence sat near the
top of the returned context, whether a multi-fact probe got all of its facts or one of three,
or whether the best evidence outranked merely supporting evidence.

- New `scripts/ranking_metrics.py`: pure arithmetic, no I/O, **imports nothing from this repo**
  so the Thinkpad helpers `probe/run_stack2_recall.py` / `run_prod_recall.py` can adopt it
  unchanged. `recall_fraction`, `graded_recall_fraction`, `first_hit_rank`, `reciprocal_rank`,
  `average_precision`, `ndcg_at_k` (gain `2^grade - 1` over the `log2(position + 1)` discount,
  divided by the ideal ordering), `hit_at_k`, and `score_probe` for the whole row at once.
  Each gold quote is credited **once**, in the first returned string that contains it.
- The unit is the returned **string**, not a page: this corpus has no page numbers, so the
  reference instrument's "graded relevant page" has no counterpart here.
- `normalise_quote` and the response walk **moved into that module** and are re-exported by
  `overnight_loop` (`context_strings` now delegates to `ranking_metrics.context_units`). One
  definition of "the same text" for the loop, `chunk_embed_bench.py` and any future helper -
  a pinned test asserts all three hold the same function object.
- Probe schema: optional `evidence_grades`, ints 0-2 aligned with the `evidence` list
  (2 = contains the answer, 1 = partly relevant, 0 = irrelevant). Absent, short, or out of
  range means 2, which is what an ungraded probe already asserted, so **every existing probe
  file scores exactly as before**. Turn-identifier keys (`turn_id`, `evidence_turn`,
  `evidence_ids`) never take a grade slot.
- Per arm, over the gold-evidence probes only: `evidence_recall`, `graded_recall`, `mrr`,
  `average_precision`, `hit@1`, `hit@5`, `ndcg@5`, `any_hit_pct`, `all_hit_pct` (so this
  instrument and the Hermes helpers, which require EVERY quote, can be reconciled rather than
  argued about). `evidence_in_context_pct` is **unchanged and still gates the run**; na never
  becomes a 0.0. `--compare` now also bootstraps `evidence_recall` and `mrr` per probe.
- Deliberate gap, pinned by a test: the binary check joins the returned strings, so a gold
  quote straddling a unit boundary is a hit there and a miss per unit. The gate keeps the join,
  the rank metrics need the unit, and both numbers are reported so the difference is visible.
- **Unverified, and marked as such in every output:** rank means "position in the response
  walk". Nobody has confirmed that LightRAG lists chunks in retrieval order under
  `only_need_context`, so `summary.json` carries `"rank_order": "unverified-order"` and the
  comparison section says so. Confirming it against a real response is a stack-side job.

Suite: **1232 passed, 2 warnings, 248 subtests** (baseline 1189/248 at `87abcd7`). Red before
the code: 18 tests (the graded-schema, per-arm-column, driver-measurement and paired-interval
ones), proven by `git stash push -- scripts/overnight_loop.py` and restored byte-identical.
No existing test expectation was edited: `git diff --numstat` shows `tests/test_overnight_loop.py`
+91/-0.

## 2026-10-27 — `scripts/chunk_boundary_diff.py`: counts-only A/B of the chunker across two commits

A replay through tag `structure-cutover-1` (`f7051e6`) cut the S/4 PDF into 98 pieces where the
stored build (`0047a69`, chunkrun9) stored 106, with **zero sha overlap**, while eight text
transcripts and a DOCX came back byte-exact. Comparing two builds over one document needs a tool
that can be pasted into a report, so this adds one. No app/ behaviour changed.

- `scripts/chunk_boundary_diff.py` — two commit-ishes (exported with `git archive` into a temp dir,
  so the working tree is never the thing under test) or two repo trees, one document, and **counts
  only**: pieces per side, per-piece length + sha256, the first diverging index, how many pieces are
  identical at the same index, sha OVERLAP between the sides, and per differing position the
  character offsets and lengths on both sides. `--only-function` mode reports heading-detector
  disagreements as (line number, character length, verdict_left, verdict_right) with **no line text**.
  `--env KEY=VALUE` (repeatable) pins the child environment. Standard library only.
- Each side runs in its **own interpreter** rooted at its own checkout. `app.structure_chunker`
  imports its siblings relatively, so one process can never hold both versions.
- Digest of the finding, measured on invented text (280-line PDF-like document, 20 planted all-caps
  lines ending in a full stop): the rule change flips exactly those 20 lines
  (`_is_all_caps_heading` 40 → 20, composite `_is_heading` 60 → 40) and moves `units_whole` 50 → 30,
  but at the shipped window (`max_piece_tokens` 7500) it changed **no stored piece** — 10 pieces per
  side, identical at every index. Only when the window binds does it move cuts (budget 200: 41 pieces
  per side, 0 overlap). Piece **counts** never differed between the two builds at any budget tested,
  so "stricter heading rule means fewer pieces" is not the mechanism here.
- Same commit, only the extracted text differing (page marks dropped, column runs collapsed, some
  wrapped lines joined): 10 pieces per side, **0 identical, 0 sha overlap**, p50 2,318/2,279 chars —
  the audit's signature. `extract_pdf_text_with_backend` (`app/pdf_extract.py:201`) says why:
  `pdftotext -layout` reproduces the column positions the row reassembly reads (Hermes measured 803
  rows rejoined with poppler, 0 with pypdf), and the backend is recorded on the row as
  `structure_extract_backend` (`app/worker.py:972`).
- New `tests/test_chunk_boundary_diff.py` (18 tests) compares the two real commits over invented
  text and asserts the report leaks no input text, no fixture word, and no line content.

Suite: **1250 passed, 2 warnings, 248 subtests** (baseline at `b462f24`: 1232/248; two new files,
no existing test edited). The patch also applies to a `git archive` export of `master` and there
runs **1242 passed, 8 skipped** - the eight skips are the end-to-end class, which needs both
comparison commits and skips by design without git history.

## 2026-11-13 — three ingest fixes from the GL-07/08 synthetic-company run (Plane GRAG-62/63)

Hermes ingested Northwind Chemicals and Contoso Pharma on stack2 on 2026-11-12. Three
defects showed up in every company folder. Fixes 1 and 2 are implemented; fix 3 is a
diagnosis with the decision left to the lead (see below).

### Fix 1 — `manifest.json` is never ingested

Every synthetic company folder carries a harness `manifest.json`. `.json` is in
`SUPPORTED`/`TEXT_EXTENSIONS`, so the worker submitted it and LightRAG failed it in
every folder: `Chunk token length 3431 exceeds chunk_token_size 3060`.

- New setting `INGEST_SKIP_BASENAMES` (default `manifest.json`,
  `app/config.py:ingest_skip_basenames`). Comma-separated, matched on the **whole
  basename**, case-insensitive, never on an extension: `orders.json` still ingests,
  `company_manifest.json` still ingests, `manifest.json.bak` is not matched (it gets
  the normal `unsupported_extension` row).
- A refused file gets a **terminal `skipped` row** (`error_code: skipped_by_name`)
  naming the rule, not a failure row and not silence. Scans after that write nothing.
- The rule does **not** take down a row that already has content upstream or in
  flight (`ingested`/`submitted`/`processing`): the skip list is not a deletion
  mechanism. Removing content from the store stays a separate explicit operation.
- **Limit, tested for:** `/upload` stages files as `<id>--<name>`, so a basename rule
  cannot see an API-uploaded `manifest.json`. Only the filesystem scan door is covered.
- New `tests/test_ingest_skip_rule.py` (12 tests).

### Fix 2 — a skipped duplicate reaches a terminal status

The skip happens before any submit, so there is no track to poll. The row was written
`accepted`, which every consumer reads as "waiting to be submitted", and it never
terminated: one stuck row in Northwind, two in Contoso.

- `skipped` is the **tenth lifecycle status** (see the ADR 0001 amendment). Written by
  `mark_duplicate_candidate` in place of `accepted`, and by the new skip rule.
- Both stuck shapes are terminated: the scan door's `accepted` row and the **upload
  door's `pending` row** (`app/api.py` flags `duplicate_content` and never submits).
- A row the old build left at `accepted` is promoted by the next worker scan, once.
- `skipped` is not ACTIVE: not in `worker.ACTIVE_DUPLICATE_STATUSES` nor
  `state_store.ACTIVE_SUPERSEDING_STATUSES`, so it cannot suppress a later copy or
  supersede a row. It is never polled (no `track_id`) and never retried (the retry
  block is gated on `status == "failed"`).
- `derive_readiness` now answers `duplicate_content` / `skipped`; the company join is
  unchanged (`scope_query_citations` joins by path, no status filter), which is tested.
- New `tests/test_skipped_duplicate_rows.py` (14 tests); three existing assertions in
  `test_company_aware_dedupe.py`, `test_worker_dedupe.py` and
  `test_state_store_lifecycle.py` updated to the new status, plus one new promotion test.

### Migration note — existing `accepted` duplicate rows (NOT run by anything)

Nothing in `app/` or `scripts/` runs this. The next worker scan promotes those rows by
itself, so this is only for an operator who wants the `by_status` counts to move before
that scan. Preview first; it touches no row that has a track.

```sql
-- Read-only preview (state DB, e.g. ~/rag-project/state/ingest.db).
-- Shape 1: the scan door's parked row, written by the old build.
SELECT path, status, error_code, updated_at
  FROM documents
 WHERE status='accepted'
   AND error_code='duplicate_content'
   AND track_id IS NULL
   AND json_extract(warnings_json,'$.worker.dedupe_action')='skip_duplicate_content';

-- Shape 2: the upload door's row (the door flags it and never submits it).
SELECT path, status, error_code, updated_at
  FROM documents
 WHERE status='pending'
   AND error_code='duplicate_content'
   AND track_id IS NULL;

-- Apply each shape with the same predicate, swapping SELECT for
-- `UPDATE documents SET status='skipped', updated_at=datetime('now')`.
```

`json_extract` needs SQLite's JSON1 (present in the worker image's Python: verified
against SQLite 3.54.0 here). A row whose `status` is already `skipped` matches nothing,
so it is safe to run twice.

### Fix 3 — PDF chunk-token overflow: diagnosed, **not fixed**, decision reported

`Chunk token length 505 exceeds chunk_token_size 481` on the synthetic PDFs is the same
family as the production S/4 PDF and the `manifest.json` failures.

1. **481 is our number, not LightRAG's setting.** `render_for_lightrag` sends
   `chunk_token_size` per document, and its value is the largest rendered piece counted
   **in our counter** plus `STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS` (64). Two different
   documents produced two different limits (481, 3060); a configured limit would be one
   number for every document.
2. **Our counter is the fallback and it is not an upper bound.** `requirements.txt` does
   not install tiktoken, so `app/structure_chunker.py` uses `conservative-overcount`
   = `max(ceil(chars/3.6), whitespace_units)`. That over-counts prose (measured 0.62x of
   a lower bound on LightRAG's own count — the safe direction the module docstring
   promises) and **under-counts** the classes a `pdftotext -layout` PDF and a JSON
   manifest produce: 1.94x under on an invented part-code/date/money table, 3.0x under
   on dotted number runs, 1.04x under on JSON. LightRAG counts with tiktoken cl100k, so
   the segment it measures can exceed the limit we asked it to enforce. The stack2
   shortfalls (+21%, +14.5%) sit inside that range.
3. **`STRUCTURE_CHUNKING_MAX_PIECE_TOKENS` (7500) is orthogonal.** It is the BGE-M3
   8192-token window less a margin, checked against a different question. It never
   bounds a piece against LightRAG's tokenizer. Note the same weak counter also carries
   the window check when `STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH` is empty — that is
   the silent-truncation variant of the same instrument error.
4. The lower bound in (2) is provable offline without a vocabulary: cl100k pre-tokenizes
   before BPE and each pre-token chunk is at least one token, so counting the published
   pre-tokenizer's chunks bounds LightRAG's count from below.
- New `tests/test_pdf_chunk_token_overflow.py` (11 tests), including
  `TestTheGapIsStillOpen` — a characterisation test that fails loudly if anyone makes
  the path claim a bound it does not have, and that must be inverted when a fix lands.
- Options reported to the lead, not chosen: (A) count in LightRAG's tokenizer family
  (tiktoken, or a vendored local cl100k file read like `app/embed_tokenizer.py` reads the
  embed tokenizer — "never fetched"); (B) inflate `chunk_token_size` by a relative
  safety factor (changes no piece boundary, so the 8 transcripts + DOCX stay byte-exact,
  but it stops LightRAG's check being a guard against oversized chunks); (C) split pieces
  against a LightRAG-aware budget (changes boundaries only for documents that fail
  today); (D) raise `STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS` — **proven insufficient**, the
  shortfall scales with the piece (the manifest case needed +371 tokens).
- Nothing in `app/` changed for fix 3.

Suite: **1337 passed, 2 skipped, 2 warnings, 263 subtests passed in 9.74s** (baseline
at `a8f04bb`: 1299 passed, 2 skipped, 2 warnings, 248 subtests). Three new files and
38 new tests; four existing assertions updated to the new status.

## 2026-11-13 — chunk-token overflow, option B: a relative safety factor on `chunk_token_size`

Operator decision on the GL-07/08 fix-3 report: **B now, A later**. Hermes confirmed
`STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH=/opt/bgem3-tok` is set and the tokenizer files exist on
production and stack2, so the embedder-window check is **not** running on the weak counter and this
change does not widen that window.

- Two new settings, both read where the limit is built (`app/structure_chunker.py`, called from
  `app/worker.py`):
  - `STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY` (default **3.2**) — the limit is now
    `ceil(largest_piece_in_our_units x SAFETY) + STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS`. The default
    is not a round number: it is the largest measured under-count in
    `tests/test_pdf_chunk_token_overflow.py` (prose 0.62x, JSON 1.04x, code 1.55x, PDF pipe tables
    1.94x, dotted number runs 3.00x) plus headroom, against the two stack2 rejections at 1.21x and
    1.145x. A test pins the default to those measurements so lowering it below a measured ratio
    fails here.
  - `STRUCTURE_CHUNKING_CHUNK_TOKEN_CEILING` (default **8192**, `0` disables) — never ask LightRAG to
    enforce a chunk bigger than the served embedder's window. The cap can never pull the number
    below the pre-factor `largest + FIT_MARGIN`, so a misconfigured ceiling cannot create a rejection
    that did not already exist. Values below 1.0 for the factor are clamped to 1.0.
- **No piece boundary moves.** The submitted text is byte-identical with the factor on or off;
  pinned per document by sha256 (8 invented transcripts + a DOCX-shaped spec body) in
  `tests/test_pdf_chunk_token_overflow.py::TestTheGapIsClosedByTheSafetyFactor`.
- `TestTheGapIsStillOpen` is **inverted**, as that report promised. It is now
  `TestTheGapUnderThePreFactorConfig` (the defect at `SAFETY=1.0`, kept so the remedy cannot be
  removed silently) plus `TestTheGapIsClosedByTheSafetyFactor` (no submitted segment's cl100k
  lower-bound count exceeds the limit we ask for, on the same adversarial fixtures).
- Diagnosability on the row: `structure_chunking_chunk_token_safety` (after clamping),
  `..._largest_piece`, `..._ceiling`, `..._uncapped`, `..._capped`. Counts only, never text.
- Consequence stated plainly: a bigger limit makes LightRAG's size check a weaker guard against
  oversized chunks. The ceiling is what keeps that from becoming "accept anything"; the window check
  in the worker is the real oversized-chunk gate, and it runs on the embedder's own tokens here.
- **A remains the proper fix.** Every ratio above is a *lower* bound, so 3.2 bounds the cases we
  measured, not every case there is. Counting in LightRAG's tokenizer family (tiktoken, or a
  vendored local cl100k read the way `app/embed_tokenizer.py` reads the embed tokenizer — never
  fetched) removes the guesswork. Not implemented here, by instruction.

Suite: **1370 passed, 2 skipped, 2 warnings, 279 subtests passed** (immediately before this change,
on the same branch: 1359 passed, 2 skipped, 2 warnings, 263 subtests). +11 tests, +16 subtests;
nothing removed.

## 2026-11-13 - M1 asked-identifier grounding: flag only, no behaviour change

- `app/grounding.py` (new): the rule the GL-08 investigation proposed - does the identifier
  the QUESTION named appear verbatim, by token match, in the references of the pass that
  produced the answer? `support_gate()` classifies; it never raises or refuses. Three traps
  are encoded in it rather than left to the caller: token match, never substring (a company
  code is a substring of every planted code in the store, so `code in evidence` is a rubber
  stamp); the question sanitised with `sanitize_like_lightrag` before extraction (LightRAG
  deletes C0 controls from stored text, so raw question text compares false against clean
  evidence); and evidence read from the pass's references, not from the caller's
  company-scoped, `CITATION_TOP_K`-truncated citation list.
- `GROUNDING_CHECK_ASKED_IDENTIFIER` (default **false**): when on, one added key,
  `query_scope.grounding`, and six flat query-log fields carrying counts and the caller's own
  identifiers - never reference text. When off, the key does not exist at all, so the shipped
  envelope is byte-identical and the payload to LightRAG is unchanged.
- `scripts/unanswerable_grounding.py` now imports `identifiers`/`verbatim_support`/
  `support_gate` from `app.grounding` instead of carrying its own copy: the harness cannot
  certify a rule the gateway no longer runs.
- `tests/test_grounding_gate.py` (new, 17 tests) pins both halves - off is byte-identical,
  on reports without changing - because an enforcement commit that quietly altered the
  response would otherwise look like a passing measurement.
- Deferred by design: acting on the verdict (a 422 or a grounded `no_evidence` answer), which
  needs a measured run on the real probe sets first. The rule's reach is stated in `docs/05`:
  on the GL-08 stack2 run it would have fired on 4 of 8 hallucinations and on 0 of the 43
  answerable probes, so it cannot cost their recall - and says nothing about them either.
- Gap named, not papered over: those two figures come from the stack2 run export, which was an
  off-repo artefact and is not versioned, and the in-repo harness scores only a *generated
  corpus* (`python scripts/unanswerable_grounding.py <corpus>`), not a run export. So the
  corpus half of the GL-08 finding is reproducible here and the run half is not. A scorer for
  run exports is the prerequisite for the go/no-go on enforcement, and it should be written
  against the real export shape rather than a guessed one.

## 2026-11-13 - run-export scorer for the M1 gate (`scripts/score_grounding_run.py`)

- New script turns a labelled probe run + the gateway query log into the go/no-go numbers for
  ENFORCING M1, counts only: `would_newly_refuse_answerable` (the guard, printed even at 0),
  `hallucinations_caught` / `_missed`, `references_without_content`, per-shape row counts, the
  labels as the export already labelled them, gate cross-tabs, and rows vs distinct-question
  denominators. No question text, answers, identifiers or reference content in the report - it is
  meant to be pasted into a ticket.
- It fails loudly instead of guessing: an export row with no log line, an unknown label value, an
  unknown manifest shape key, a repeated question with no `request_id` to disambiguate, or a run
  with no query log all exit 2 with **no headline numbers printed**, so a half-joined run can
  never produce a decision number.
- Join key is chosen and reported: `request_id` (the gateway puts the same id in the response
  `meta.request_id` and in the query log, so a runner that records it joins unambiguously even
  with repeats), falling back to question+company only when that is unambiguous.
- The runner's export field names are ASSUMED and declared in one place (`FIELD_ALIASES`), because
  the runner lives outside this repo and no real export was on disk when this was written. Unknown
  names are a `--map theirs=ours` one-liner, and a failure message lists the keys a row did carry
  (key names only) so the mapping is fixable without reading the corpus.
- Found while building it, and fixed: the generated manifests repeat question text within a
  company (scoped probes are per FACT and several facts share a topic label). That is why the 55
  row GL-08 run is 41 distinct questions. A first draft rejected the repeats, which would have
  made the real run unscorable; it now counts them and reports entries vs distinct keys. A text
  that appears in two different shapes is still a hard failure.
- `app/observability.emit_query_log` gains `grounding_supported` and `grounding_asked_unsupported`.
  The M1 projection logged `grounding_unsupported`, which is asked-OR-asserted, so the guard
  metric could not be computed from it exactly - an older log is still scorable but only
  conservatively, and the script labels that and warns.

## 2026-11-14 - identifier-bearing answerable probes for the M1 guard (`scripts/make_identifier_probes.py`)

- The GL-08 `would_newly_refuse_answerable = 0` was structural, not a result: all 43 answerable
  questions named no identifier, so the gate reported `no_identifier` and could not have refused
  anything. This generates the probes that make the guard measurable: one per planted fact code
  (39 per company), phrased over 7 deterministic templates (code at start / middle / end, quoted,
  with a company mention), plus edge variants on ~20%, a few two-code questions, and the existing
  unanswerable rows carried over unchanged as controls.
- Output goes outside the repo: `/home/maverick/work/synthetic-co-probes/<Company>/manifest.json`
  (the shape `scripts/score_grounding_run.py` and the Hermes runner consume) plus
  `edge-variants.json` and `RUN-NOTES.md`. Buckets are the existing `scoped` / `cross_company` /
  `unanswerable`, because the scorer rejects unknown probe keys.
- Every row carries `expected_gate` (`fire` / `not_fire` / `no_identifier`), DERIVED by running the
  gate's own rule (`app.grounding.identifiers` + `verbatim_support`) against the pieces
  `app.structure_chunker` cuts from the document holding the code - so the expectation is a
  prediction the run can be scored against, not a hand-written label.
- Validation stops the run: a code that is not planted, not verbatim in its document, or not a
  standalone token in a text-route chunk piece; a question containing a planted answer sentence; a
  duplicate or non-namespaced id; an "unanswerable" control whose code is actually in the corpus;
  and output the repo's own scorer refuses to load.
- What the set deliberately surfaces about the gate, all measured rather than asserted: a question
  whose code has one hyphen opened to a space fires on an answerable probe (3 per company) - that is
  what a line-wrapped transcript, an ASR transcript or a human typing from memory produces; a
  two-code question fires if ANY asked code is missing from the returned evidence union, so
  retrieval coverage becomes a refusal cause; a lowercase code is invisible to the identifier rule,
  which is unprotected rather than unsafe. Expected-fire ids are listed per company in
  `edge-variants.json`, and the acceptance rule is stated in `RUN-NOTES.md`: a row that fires
  outside that list is a genuine false refusal.
- Nothing in `app/grounding.py`, `app/api.py` or the flag changed. Gate behaviour untouched.

## 2026-11-15 - why the M1 gate is blind online (investigation, tooling only)

Task `qitem-20261115182714-dd468158e0f17600`. **No `app/` change, no flag change.** The 2026-11-14
stack2 run fired the M1 gate on 96 of 102 identifier probes, 86 of them unexpected, with
`grounding_references_without_content` = 10-11 on every row. Report:
`docs/ops/runs/2026-11-15-m1-evidence-blindness.md` (`docs/ops/runs/*.md` is gitignored,
so the same text is handed over at
`/home/maverick/work/patches/m1-evidence-blindness-report.md`).

What the measurements say:

- **The run reconciles to empty evidence, exactly.** Recomputed from the probe set alone with
  evidence = `""` and the shipped rule: 102 answerable rows, 96 identifier-asking, 96 would fire,
  10 by design, **86 unexpected** - Hermes' numbers to the unit. The 6 that did not fire are the
  lowercase rows, which the shipped regex (`app/grounding.py` `IDENTIFIER_RE`, uppercase initial)
  never extracts. No rule defect is needed to explain the run.
- **Nothing on the gateway path strips chunk text**: `app/api.py:1306` keeps `references` verbatim,
  `enrich_citation` (`app/api.py:822-833`) copies dicts, `truncate_citations` slices. What is real is
  that this repo has **three readers of different width**: the gate accepts only `content` as a
  string (`app/grounding.py:88-93`), the document generator expects `content` as a **list**
  (`app/generation.py:29-32`), and the reranker accepts `content`/`text`/`chunk_text`/`body` as
  string or list (`app/rerank.py:33-41`). If upstream sends the list form, the blindness is ours.
  No raw upstream response is recorded anywhere in the repo, and the LightRAG image is not on the dev
  host, so that half needs one dump - `scripts/m1_reference_shape_probe.py` prints the two
  discriminating counts and no corpus text.
- **Separate finding: a gate on the gateway path does not cover the production path.** Open WebUI goes
  through the bridge, which never asks for chunk content (`app/api.py:234-235`) and reduces every
  reference to `{"file_path"}` (`app/api.py:629`).
- **The co-location fear does not exist in this corpus.** All 78 planted fact sentences name their
  code inline and pass the gate's token test on their own; 78/78 codes are token-supported in their
  own document's pieces; 0 of 78 facts have code and sentence in different pieces, at the configured
  7500-token window or at 1200/400. So "the fact text was in context but the code was not" cannot be
  true of the same chunk - they are the same bytes.
- **The decision that matters is the evidence set.** With the shipped rule and corpus-side evidence the
  simulation lands on the by-design floor (10 refusals, all expected, 12/12 controls caught); with
  one-document evidence it adds 4 two-code refusals (their codes live in different documents). Of the
  normalisation variants only `reconstruct_if_known` (join adjacent tokens **only** when the joined
  form occurs in the evidence) fixes the hyphen-opened rows without breaking two-code rows: 4
  refusals, all expected. Case-folding is safe (6 more rows ask, 0 more refuse). Stricter
  "fire only if every code is unsupported" **loses** the 4 one-absent catches.
- **Two unmeasurable decisions surfaced:** no probe asks the bare company code (0 of 207 pieces carry
  it as a standalone token, so such a question can never be supported), and no probe carries a
  lowercase invented code. Both need rows before either rule is enforced.

Added: `scripts/m1_evidence_offline.py` (co-location, piece-window sensitivity, run reconciliation,
gate-rule simulation under three evidence models; counts only), `scripts/m1_reference_shape_probe.py`
(run on the stack; key paths/types/lengths/hashes and membership booleans, never chunk text),
`tests/test_m1_evidence_offline.py` (35 tests; the real-corpus class skips when the generated GL-07
corpora are absent). Suite: `1452 passed, 2 skipped, 279 subtests passed`.

## 2026-11-15 - M1 evidence reader: shape-tolerant and self-reporting (flag still OFF)

The M1 evidence investigation found four readers of the same LightRAG reference dicts, and the
grounding gate was the narrowest: `app/grounding.py` read only a **string-valued `content`**, while
`app/rerank.py:33-41`, `app/graphrag.py:46-63` and `app/generation.py:29-32` all accept
`content`/`text`/`chunk_text`/`body` and accept a **list** of chunk strings. A response that let the
reranker score a chunk and the prompt cite it could leave the gate with an empty evidence string and
a `no_evidence` verdict for a question the system demonstrably had the text to answer. The narrowest
reader in the repo was deciding whether the store supports an answer.

- New `app/citation_text.py`: `CITATION_TEXT_KEYS` plus `citation_content()` (string or list, joined
  the way the generation prompt joins it), `citation_content_shape()` and `content_shape_counts()`.
  `grounding.evidence_texts`, `rerank.citation_to_text` and
  `graphrag.citation_to_text_for_entity_extraction` now share it; each caller keeps its own length
  cap and JSON fallback. `app/generation.py` is deliberately untouched - widening the prompt is an
  answer-path change and is not in this brief.
- `query_scope["grounding"]` gains `references_with_content` and `references_content_shapes`
  (`string` / `list` / `empty` / `absent` reference counts), mirrored into the query log as
  `grounding_references_with_content` and `grounding_references_content_shapes`. Counts only: a run
  can now tell "the store held the text and the answer invented the code" apart from "the response
  carried no chunk text at all", without storing a single reference.
- Not changed: no flag default, no answer/citation/status behaviour, no upstream payload. With
  `GROUNDING_CHECK_ASKED_IDENTIFIER=false` (the shipped state) the envelope stays byte-identical and
  the existing byte-identity test still pins that. `reconstruct_if_known`, case-folding and
  company-code handling are **not** implemented here; they wait for the reference-shape facts from
  the stack and the lead's go.
- Tests: 6 new in `tests/test_grounding_gate.py` - list-valued `content`, text under each alternative
  key, `empty` and `absent` counted apart, a whitespace-only chunk is not evidence, and a contract
  test tying the gate's key list to the reranker's and the graph scanner's by function identity so
  the readers cannot re-fork. The query-log tests carry the shape census. Suite:
  `1470 passed, 2 skipped, 290 subtests passed`.

## 2026-11-15 - the M1 reference-shape probe could not see what it was sent (two command fixes)

Hermes ran `scripts/m1_reference_shape_probe.py` on stack2 on 2026-11-15 and both legs failed for
command-surface reasons, not measurement reasons:

1. The gateway request body always carried `{"company": ...}`, which **scopes** the query. The scoped
   question came back with `data.citations n=0`, so the probe printed the shape of nothing and the
   reference-shape question stayed open. `--company` is now optional and the default asks **unscoped**,
   the way the probe runner asks; the probe row is located by `--probe-id` across
   `--probe-root/*/manifest.json`, and an id that appears under two companies is an error asking for
   `--company` rather than a silent pick. Passing `--company` still scopes, for a deliberate run.
2. The raw LightRAG leg authenticated with `Authorization: Bearer` and stock `lightrag:v1.5.7`
   answered **HTTP 401**; `X-API-Key` is the header that image accepts. New
   `--lightrag-auth-header bearer|x-api-key`, **default unchanged** (`bearer`, so the first run's
   command line still runs verbatim), and a 401 now prints the flag to retry with.

**Read the 2026-11-15 second probe output with this in mind:** that run used a local copy of the
script carrying these two changes, so its output is not reproducible from `51e3c39` alone - it needs
this commit. Any earlier "n=0 references" reading is the scoping defect, not a fact about LightRAG.

Tests: +11 in `tests/test_m1_evidence_offline.py` (`ProbeCommandSurface`: unscoped by default, scoped
on request, id-resolves-manifest without a company, duplicate id across company manifests demands
`--company`, unknown id exits 2 without naming question text, `auth_headers` styles, `post()` sends the
selected header, raw-leg auth style is passed and hinted on 401, default stays `bearer`), and the
existing end-to-end CLI test now pins the scoped body plus the bearer default. Suite:
`1481 passed, 2 skipped, 290 subtests passed` (base `0d83376`: 1470).

## 2026-11-17 — GL-10 part 1+2: the company named in the question scopes the query

The operator's rule is that a bare mention is enough: name a company and the answer comes from that
company, name several and it comes from those, name none and nothing changes. Nothing in the query
path read a company out of the question text; scope came only from the `company` request field
(`app/api.py:1273`), and the caller that matters — Open WebUI through the bridge — never sends one
(`build_ollama_bridge_query_payload`, `app/api.py:216`). Part 1 is the map
(`docs/decisions/gl10-company-from-question-map.md`, every claim a file:line at this base); part 2
is the reading of the question, behind `COMPANY_FROM_QUESTION` (default **off**).

- New `app/company_scope.py`. Matching is against the store's own company names, never guessed
  organisations: a wrong scope hides every other company's answer and reads like the system lost its
  memory, while a missed scope only leaves today's behaviour. Case- and punctuation-insensitive
  whole-phrase matching, longest name first, a shorter name cannot match inside a longer one's span
  ("Acme Chemicals and Acme Corp" names two companies, "Acmeist" names none). A one-word company
  whose name is also ordinary English needs a capital in the question, so "what did the target say"
  does not scope to a company called Target. `COMPANY_ALIASES` bridges what a person types to the
  directory name and is validated against the known list, so an alias typo cannot scope a query to a
  company with no documents. A question naming an unknown organisation stays unscoped and records it
  as `company_candidates`.
- `app/state_store.py:648` `list_distinct_companies()` — the known list is real rows; `skipped`
  duplicates count, because a skipped duplicate still proves that company owns that path.
- `scope_query_citations(..., allowed_companies=[...])` takes a company **set** for the several-
  companies case; the single `requested_company` comparison and the unscoped path are unchanged.
- Wired on both doors: `/query` (explicit field still wins over detection) and the direct bridge,
  the path production uses. With the flag on the bridge filters the references the Sources block is
  built from and logs `company_scope`; with it off that block never runs.
- **Honest limit of this commit:** it restricts the citations returned and shown, not what LightRAG
  retrieved or what the model was shown. Making retrieval itself company-scoped is part 3 and depends
  on facts only the stack can give (does v1.5.7 `/query` accept a document/path filter; what
  `only_need_context` returns per chunk) — recorded as open questions in the map.
- Tests: 28 new in `tests/test_company_from_question.py` — detection rules, alias validation,
  `list_distinct_companies`, the company-set filter including the missing-row exclusion, and the
  bridge on/off/cross-company/unknown-name cases. Suite:
  `1509 passed, 2 skipped, 297 subtests passed` (base `317c077`: 1481).

## 2026-11-17 — GL-10 part 3: the company named in the question scopes the answer, not just the Sources

Part 2 filtered the citations after the answer existed, which leaves the model having answered from
another company's material. Part 3 is `app/scoped_pass.py`: retrieve with `include_chunk_content`,
drop every reference whose `documents` row does not belong to a company the question named, and
generate again from what remains using `mode: bypass` — the retrieve-then-generate pattern
`app/generation.py` already ships, so no unprobed upstream capability is assumed.

- `COMPANY_SCOPE_ENFORCEMENT=citations` (default) is part 2's behaviour; `retrieval` turns the
  scoped pass on. The master gate stays `COMPANY_FROM_QUESTION=false`, so with the shipped defaults
  no part of this runs.
- The evidence-**adding** stages are skipped while a scoped pass runs: multi-query rewrites, the
  naive weak-signal fallback, and graph expansion would each put the excluded companies' material
  back into the answer. They are recorded as `stages_skipped_for_scope`.
- Attribution is the boundary, and it is honest about itself: a reference with no joinable
  `documents` row (the upload-door basename case, `app/lightrag_client.py:25-31`) is dropped as
  `unattributable` rather than assumed to belong to the company asked about. If nothing attributable
  survives the answer says so — no second model call, and never a silent fall back to the unscoped
  draft, including when the regeneration itself comes back blank.
- The trade-off, stated in the docs rather than buried: `retrieval` costs a second model call and,
  on the Open WebUI bridge, the token stream (`/query/stream` is not called at all on that path).
  That is why it is an explicit value and why the operator, not the code, decides.
- Log fields (counts and company names, never reference text): `companies_detected`,
  `company_detected_from`, `company_candidates`, `scoped_pass`,
  `scoped_pass_references_kept`, `..._dropped_other_company`, `..._unattributable`,
  `..._dropped_by_context_cap`, `..._second_model_call`, `..._declined_reason`.
- Tests: 22 new in `tests/test_scoped_pass.py`. The load-bearing one asserts that the prompt of the
  second call contains the named company's chunk text and **not** the other company's. Also pinned:
  two-company questions keep both; unattributable references refuse rather than guess; kept
  references with no chunk text refuse while still listing the documents; a blank regeneration
  refuses; the context cap drops whole references; `/query` makes exactly one upstream call at the
  default enforcement and two at `retrieval`; the bridge does not stream when scoped.
  Suite: `1531 passed, 2 skipped, 300 subtests passed` (base `317c077`: 1481).
