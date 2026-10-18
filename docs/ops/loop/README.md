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
- `"structure_chunking": {"budget_tokens": 350, "header_tokens": 25, "headers": true}` →
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
| `WORKER_PDF_EXTRACT_ENABLED` / `WORKER_DOCX_EXTRACT_ENABLED` | true | per-type kill switch (still requires `STRUCTURE_CHUNKING_ENABLED`) |
| `WORKER_PDF_TEXT_BACKEND` | `auto` | `auto` prefers poppler's `pdftotext -layout` (already in the image, and it reproduces column positions, which is what the row reassembly reads), then `pypdf` |
| `WORKER_PDF_MAX_BYTES` / `_MAX_CHARS`, `WORKER_DOCX_MAX_BYTES` / `_MAX_CHARS` | 104857600 / 2000000 | byte and character guards |
| `STRUCTURE_CHUNKING_MAX_PIECE_TOKENS` | 450 | a plan with a bigger piece is not submitted chunked |
| `STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS` | 64 | `chunk_token_size` = largest piece + margin, so LightRAG cannot re-cut our pieces |
| `STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS` | 512 | the served embedder's window |
| `STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH` | empty | local tokenizer files for the fit check; never fetched |
| `STRUCTURE_CHUNKING_FAIL_ON_LOST_TEXT` | false | make a coverage gap fall back instead of only recording it |

Fallbacks are the point: a scan with no text layer, a corrupt archive, a piece over the
ceiling, a piece over the embedder window, or an unreadable tokenizer path all put the
document back on today's route with `structure_extract` / `structure_chunking_reason` on the
state row. A parser bug may change how a document is chunked; it must never change whether it
is ingested.
