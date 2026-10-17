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
