# docs/ops/loop — the files the overnight loop and the offline benchmark read

Nothing here is read by the app at runtime. These are the inputs an operator hands to
`scripts/overnight_loop.py` (`--experiments`) and `scripts/chunk_embed_bench.py`
(`--configs`). Every file is JSON with a `_readme` array: the runbook is in the file,
next to the arms it describes, because a runbook that lives somewhere else goes stale.

## Files

| file | what it is |
|---|---|
| `experiments.json`, `experiments-v2.json`, `experiments-e4.json`, `experiments-g.json` | historical arms already run; kept so a re-run reproduces an older comparison |
| `experiments-chunking.json` | the first chunking sweep (transcript prep, semantic/recursive chunker params) |
| `experiments-retrieval-sweep.json` | query-side sweep on the CURRENT store: no arm re-ingests |
| `experiments-chunk350.json` | one confirming arm, `C350`, CHUNK_SIZE 350 / OVERLAP 50 |
| `chunk-embed-bench-configs.json` | the offline chunk × embedder × reranker grid (no ingest, no LLM) |

## How an arm reaches the ingest

Three experiment keys change what gets posted to `/documents/text`. All are per-arm and
default off, and with them absent the request body is byte-identical to what this driver
posted before they existed:

- `"transcript_prep": true` → `app.transcript_prep.prepare_transcript`: speaker turns
  kept whole, a context header every ~200 tokens.
- `"chunking": {"strategy": ..., "params": {...}}` → the per-request chunker LightRAG
  1.5.7 accepts. Key names belong to LightRAG; the driver checks them structurally and
  forwards them untouched.
- `"structure_chunking": {"max_piece_tokens": 7500, "min_piece_tokens": 150,
  "merge_until": "floor", "header_tokens": 25, "headers": true}` →
  `app.structure_chunker` chooses every cut and the request tells LightRAG to cut ONLY
  there (`fixed_token` + `split_by_character_only`). Cannot be combined with `"chunking"`:
  two instructions about where the cuts fall means the result says nothing about which
  one worked.

The same three functions are what the worker runs behind `TRANSCRIPT_PREP_ENABLED` and
`STRUCTURE_CHUNKING_ENABLED`, and what the benchmark grid runs under
`"chunker": {"strategy": "structure"}`. A loop or benchmark result therefore describes the
shipped path rather than a driver's imitation of it.

## Structure chunking, in one paragraph per rule

`app/structure_chunker.py` is the specification in full; this is the shape of it.

- **Transcripts** (detected with `app.transcript_prep`'s parser and its bars) are cut at
  turn boundaries; short turns group until the budget; a turn longer than the budget
  splits at sentences, then clauses, then words — never mid-word. The header carries the
  meeting, date and the speakers that actually recur.
- **Headings** (`#`, `1.2.3`, short ALL-CAPS lines) start a piece and feed a section path
  that appears in the header: `[Doc: … | Date: … | Section: A / B | Part k/n]`.
- **Tables** (pipe, tab, or two-space columnar) stay whole when they fit. Otherwise they
  split **by row**, repeating the header row and its `---` rule at the top of every piece,
  so a number is never stranded away from its column names. A row that does not fit
  alongside the header splits at **cell** boundaries with the row key repeated; a cell is
  only split when it alone exceeds the budget, and then by words. A caption travels with
  its table, on the first piece only.
- **Running headers, footers and page markers** are dropped from bodies only on two pieces
  of evidence at once: the line is a short fragment with no sentence punctuation (and not
  a turn the transcript parser claims), and it repeats three or more times. Every dropped
  line is counted in the row's provenance, never silently removed.
- **Everything else** is paragraphs, then sentences, then words.
- **Budget is body + header.** Oversize is impossible by construction; the single
  exception is one word longer than the whole budget, which stands alone and is reported.
- **Nothing is invented or LLM-generated.** Title and date come from the file-name stem,
  speakers from observed labels, section path from the headings. Fields are dropped (path,
  then date, then speakers, then the title) until the header fits its cap.

## Guardrails that belong to every file here

- Only whitelisted env keys may be set (see `ENV_WHITELIST` in `scripts/overnight_loop.py`).
- Any arm with `"reingest": true` clears the LightRAG store it points at. Against
  production that is refused unless `--allow-production` is passed deliberately.
- `--probes-only` is the read-only mode: it scores a stack (production included) on the
  same probes and judge without writing anything, and it refuses any experiments file that
  asks for writes.
- `COMPANY_AWARE_DEDUPE` is do-not-enable (docs/08-change-log.md, 2026-10-15). It is not in
  any file here and must not be added to one.
- Reports and logs carry counts, positions and ids — never corpus text or file names.

## Benchmark flags that decide what a number means

| flag | what it changes | why it exists |
|---|---|---|
| `--embed-tokenizer <local dir>` | counts every chunk with the **served embedder's** vocabulary (WordPiece `vocab.txt`/`tokenizer.json`, or a Unigram `tokenizer.json`) and reports `embedder_tokens` p50/p90/p99/max plus `over_embedder_window_*` against the 512-token window **including special tokens** | cl100k is the right ruler for chunk sizes and the wrong one for the embedder window; names, numbers and STT garbles cost WordPiece more. Never downloaded: a hub id is refused with the fetch command to run yourself. If it is absent the report says `**NOT LOADED**` and the window columns are labelled as a stand-in. |
| `--text-copies-dir <dir>` | reads a `.pdf`/`.docx` corpus entry from `<dir>/manifest.json` (position → pre-extracted text copy) instead of refusing it | 59% of the corpus is binary; Hermes already keeps extracted copies. Without the flag nothing changes. |
| `--budgets 2000,4000,8000` | adds `recall@Ttok`: chunks are taken in rank order until the **token** total would exceed T | `recall@k` compares chunk counts; a 1200-token chunk and a 200-token chunk are not equal contributions to a context window. |
| `--allow-lost-text` | publishes a `structure` arm that dropped text instead of exiting 4 | see below. |

## Probe fields the bench reads

One JSONL item per probe. `id`, `question` and gold evidence (`evidence`, or
`evidence_turn`/`turn_id`/`evidence_ids`/`gold_span`) are required for an item to be scored
at all. Optional, and they change the report:

- `"type"` — free-form probe kind (e.g. `single-fact`, `multi-fact`, `unanswerable`). Reported
  per arm in `by_type`.
- `"source_kind"` — one of `transcript`, `table`, `pdf`, `docx`; default `transcript`. The
  report prints a **separate table for transcript vs structure-rich** probes with `n` on every
  row, and marks any row with `n < 15` as "n too small to compare".
- `"row_key"` and `"row_value"` (strings, both needed) — the row-integrity metric: does a
  single chunk contain **both**? Reported as `row_intact%` per arm over the probes that carry
  them, normalised exactly like a quote. A number retrieved away from its label is not the
  same fact.

## LOST TEXT: coverage is a separate measurement from retrieval

Every row carries `located` (the gold quote is in some chunk or seam), `in_source` (the quote
is in the raw document) and `lost_text` (in the document, in **no** chunk). Per arm the report
prints `located%` and `lost_text_ids`. For a `structure` arm a loss is a hard failure: the
run prints the ids under `LOST TEXT` and exits 4 unless `--allow-lost-text`. A chunker that
ate the text and a ranker that ranked it badly are different findings, and a number that mixes
them is not evidence about either.

## Structure-aware ingest: which documents get pre-cut, and what happens when a parser fails

With `STRUCTURE_CHUNKING_ENABLED=true` the worker parses **PDF, DOCX and PPTX itself** and
submits `POST /documents/text` with `chunking` (`fixed_token` + `split_by_character` +
`split_by_character_only=true`), so the cuts come from `app/structure_chunker.py`: table rows
with the header repeated, transcript turns, section paths. With the flag off, those files keep
going to `POST /documents/upload` exactly as before - **including no new metadata** - because
extracting them only to let LightRAG re-cut the text would trade one invisible cut for another.

| setting | default | meaning |
|---|---|---|
| `WORKER_PDF_EXTRACT_ENABLED` / `WORKER_DOCX_EXTRACT_ENABLED` | false | per-type opt-in (both also require `STRUCTURE_CHUNKING_ENABLED`) |
| `WORKER_PDF_TEXT_BACKEND` | `auto` | `auto` prefers poppler's `pdftotext -layout` (already in the image, and it reproduces column positions, which is what the row reassembly reads), then `pypdf`. Which one ran is recorded as `structure_extract_backend` |
| `WORKER_PDF_MAX_BYTES` / `_MAX_CHARS`, `WORKER_DOCX_MAX_BYTES` / `_MAX_CHARS` | 104857600 / 2000000 | byte and character guards |
| `STRUCTURE_CHUNKING_MAX_PIECE_TOKENS` | 7500 | the **window**: the only reason to cut, and it is the embedder window minus margin, not a size target |
| `STRUCTURE_CHUNKING_MIN_PIECE_TOKENS` | 150 | the **merge floor**: a unit smaller than this welds into its neighbour, so a heading never stands alone |
| `STRUCTURE_CHUNKING_MERGE_UNTIL` | `floor` | `floor` (default) stops a merge chain as soon as the piece clears the floor, keeping one natural unit per piece; `window` chains small neighbours all the way to the window |
| `STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS` | 8192 | BGE-M3's window, used for the fit check |
| `STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS` | 64 | `chunk_token_size` = largest piece + margin, so LightRAG cannot re-cut our pieces |
| `STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH` | empty | local tokenizer files for the fit check; never fetched. BGE-M3's `tokenizer.json` + `tokenizer_config.json` + `special_tokens_map.json` are read directly (Unigram, byte fallback, XLM-R pre-tokenizer split) |
| `STRUCTURE_CHUNKING_EMBED_URL` | empty | the model server for **topic-shift cuts** only. Empty means no topic pass and a recorded deterministic fallback. The LightRAG and gateway ports are refused |
| `STRUCTURE_CHUNKING_EMBED_MODEL` / `_EMBED_API_KEY_ENV` / `_EMBED_TIMEOUT_SECONDS` / `_EMBED_BATCH` | `bge-m3-mlx-fp16` / `OMLX_API_KEY` / 15 / 16 | the call is batched, cached by sha256(model+text), and bounded. The setting names the environment variable holding the key; the key is never a setting |
| `STRUCTURE_CHUNKING_TOPIC_THRESHOLD` / `_TOPIC_UNITS_PER_WINDOW` / `_TOPIC_MAX_WINDOWS` | 0.30 / 6 / 400 | TextTiling depth that counts as a topic change, window granularity, and the per-document cap on embedded windows |
| `STRUCTURE_CHUNKING_FAIL_ON_LOST_TEXT` | false | make a coverage gap fall back instead of only recording it |

### What a piece is

A piece is a **natural unit**: a whole section (heading plus everything nested under it), a
whole table, or a run of turns on one topic. A unit is split only when it cannot fit the
window, and then at its best *internal* boundary — sub-heading, paragraph, table row (header
repeated), turn change, sentence — never at a token count. A 7,000-token section is one piece;
a 12,000-token section is cut at its sub-headings; a page of sub-headings becomes a few
pieces, not a fragment each, because of the merge floor. A section's heading always rides on
its first content, including before a big table.

Both merge modes are supported deliberately. `floor` is the default because chaining unrelated
short sections into one mixed-topic piece dilutes the vector; `window` exists as the knob for
the benchmark to measure the difference rather than argue about it.

**The window is an invariant, not an aspiration.** Whichever path produced a unit — a section, a
table, a turn, or a flat document the topic pass had nothing to cut inside — a body larger than
the window is split, and a final pass checks every unit again before pieces are built. Splits are
**balanced**: a tail leftover is welded into its neighbour while the neighbour still fits, so the
output is never "one full piece plus a stub". A sentence boundary is preferred (`_split_run`
breaks after terminal punctuation), then a clause, then whitespace — never inside a word. The one
exception is a single word longer than the window: it stays, `unit-over-window(one-unsplittable-word)`
is recorded, and the document takes the upload route as it always did.

Two consequences of that rule are stated because they are measurable. **An oversize split is equal,
not greedy**: a 30,000-character turn split at a 7,500-token window comes out as roughly equal
pieces, and the piece *count* is kept, so an arm comparison is not disturbed by the balancing. **A
piece below the merge floor is welded after the fact**: any finished piece under
`STRUCTURE_CHUNKING_MIN_PIECE_TOKENS` joins its previous neighbour (or the next when there is none),
including a trailing turn of 28 tokens; if both joins would exceed the window the piece stays and the
state row carries `stub-piece-cannot-merge` with `stub_pieces_merged` / `stub_pieces_left` in the
stats. A part at or above the floor that is much smaller than its neighbours is a boundary problem
(equalise); a part below it is a stub problem (weld or report). Those are different rules, and the
coverage gate is what keeps them from being merged into one.

### How a cut survives the trip to LightRAG

The delimiter is a promise: under `split_by_character_only` it is the **only** cut LightRAG
will make, so it has to arrive intact. LightRAG sanitises the whole document field
(`sanitize_text_for_encoding`, `lightrag/utils.py:5529-5566`) *before* splitting, in this order:
strip the field, `html.unescape` **once**, delete lone surrogates and U+FFFE/U+FFFF, delete the
C0 range except tab, newline and **CR**, then strip again. It keeps every zero-width and format
character (U+00AD, U+200B-U+200F, U+2060, U+202A-U+202E, U+FEFF, U+FFF9 and friends), it keeps
carriage returns, and it never normalises unicode. A marker built from the ASCII record
separator (U+001E) therefore arrives as nothing - the eight-of-ten single-segment failure in the
stack2 run.

`app/lightrag_text.py` is a **parity** implementation, not an imitation: 62 fixed strings were
run through the real function inside `lightrag-server2` and recorded as hex
(`tests/fixtures/sancmp_tests.json`, `sancmp_real.json`), and `tests/test_sanitiser_parity.py`
asserts all 62 byte for byte. That fixture is the oracle. If the image changes, re-run the
comparison and change the module from the new fixture, not from memory of the source. So:

- markers are **printable only**: newline plus ASCII letters, digits, `<`, `>`, `-`. The default
  is `<<<CHUNK-BREAK>>>` with a blank line on each side; when every candidate is already in the
  text, a longer marker is derived from the document's sha256, so two runs of one file keep
  comparable pieces;
- an explicit delimiter built from characters the function deletes is **refused**, not obeyed;
- the payload is a **join, never a wrap**: the field is stripped before it is split, so a marker
  at either edge loses its newlines and the payload stops containing the delimiter the request
  named. `N` pieces carry `N-1` markers;
- the plan is built over the text with those **deletions already applied** - never the unescape,
  which must run exactly once, in the image, and never the field strip - so planned text and
  stored text are one string, and `structure_chunking_stripped_chars` names the codepoints taken
  out (a form feed from a page break is invisible in the output and moves where a piece reads as
  ending). Zero-width characters such as the 46 U+200B in `OrderBook.docx` **stay**: they are in
  the stored chunk, so they stay in the plan, and the extractor is not asked to compensate;
- entities are counted, never expanded here: `structure_chunking_html_entities` says how many
  `html.unescape` will expand. Usually harmless. When the entities *spell the marker* - the
  marker's word with its angle brackets and its newlines written as numeric character references
  - they are a cut the reader makes and we did not, so the picker skips that marker and the
  replay refuses the payload;
- before submitting, the worker replays the pipeline offline - sanitise, split on the delimiter,
  compare the segments with the **stored form** of each piece - and falls back to today's route on
  any mismatch. The comparison is exact, not stripped: the function's strip bites at the payload's
  two outer edges only (first piece lstripped, last rstripped, interior untouched).

State-row fields for this path: `structure_chunking_delimiters` (markers in the payload),
`structure_chunking_sanitiser_check` (`ok`, or the reason it refused),
`structure_chunking_stripped_chars`, `structure_chunking_html_entities`,
`structure_chunking_chunk_token_basis`,
`structure_chunking_chunk_token_size`, `structure_chunking_chunk_token_payload_gap`.

`chunk_token_size` is LightRAG's own budget and is counted the way **LightRAG** counts
(tiktoken `cl100k_base` when installed, otherwise the chunker's documented over-count), then
re-measured over the payload's own segments. It is never the embedder's count: doc08 of the
stack2 run went out with 2579 against a segment the image measured at 2592, and under
`split_by_character_only` that is a rejection rather than a re-cut. BGE-M3's count decides
whether a piece fits the 8192-token embedding window, which is a separate check with a separate
field (`structure_chunking_fit_basis`, `structure_chunking_embedder_tokens_max`).

### Documents with no headings and no tables

Flat text and transcripts get **topic-shift segmentation**: windows of turns or sentences are
embedded with BGE-M3 and cut where adjacent windows stop resembling each other (TextTiling
depth minima above the threshold). `structure_chunking_topic_method` on the state row says
which kind of cut happened — `embedder`, `embedder(no-shift-found)`, `fallback(no-embedder)`
or `fallback(embedder-unreachable)`. A model server that is down changes how a document is
cut, never whether it is ingested.

Transcript labels are recognised by **repeated behaviour, not line count**: `(Name)`,
`(Name):`, `Name 12:34`, bracketed timestamps, and labels sitting inside one long line. A
parenthesised label that alternates strictly with one other label (or heads two whole
thousand-token lines) is a speaker **even when the word is a pronoun** — "(They) / (You)"
across forty lines is a transcript, "(they) said" in a paragraph is not. Stage directions
(`(Cross-talk)`, `(Inaudible)`, `(Background noise)`) are never speakers **and no longer break
the rotation**: they are dropped from the label sequence before the alternation is read, so
`(Ana) … (They) … (Cross-talk) … (Ana)` is still two voices. A file where **every** non-empty
line opens with a short label is a transcript on shape alone, which is what a meeting typed as
two `(They)` / `(You)` lines of 6,000 tokens each looks like; a name-shaped label (≤2 words,
≤24 characters) is what keeps "(Introduction)" plus a parenthesised paragraph, and a contents
page of short parens, in the prose path. A turn longer than the window is split inside the turn.

### Table text shape

PDF, DOCX and PPTX tables all arrive in one shape: a canonical **pipe row**, header row first,
one logical row per line — `| Region | Q1 | Q2 |`. In DOCX each cell is one line; in PDF,
`pdftotext -layout` column positions are reassembled into that row (a cell that wrapped over
three to eight physical lines becomes one logical row). pypdf output has no column positions,
so it is passed through without reassembly rather than guessed at. Old quotes written in tab
form do not match this; they are regenerated, not accommodated.

Fallbacks are the point: a scan with no text layer, a corrupt archive, a piece over the
ceiling, a piece over the embedder window, or an unreadable tokenizer path all put the
document back on today's route with `structure_extract` / `structure_chunking_reason` on the
state row. A parser bug may change how a document is chunked; it must never change whether it
is ingested.
