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
