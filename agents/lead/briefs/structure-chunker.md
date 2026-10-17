# Worker brief: context-aware chunking, track A: structure-aware pre-chunker (branch `context-chunking`)

New worktree: /home/maverick/work/graphraggle-ctx (branch `context-chunking`, from `chunking`
4642925). Work ONLY there; the lead commits. You may NOT read corpus, probes, staging or any
LightRAG data; synthetic fixtures only. Bare-env full suite must pass (use the main repo's
venv: /home/maverick/work/graphraggle/.venv/bin/python). No network, socket guard stays.

## Why
Fixed-token cutting loses context and will damage tables and non-standard layouts (the
embedder also only sees 512 tokens, so every piece must be small AND coherent). Operator wants
a method that decides where and how to cut. LightRAG 1.5.7 cannot take a custom chunker via
the API, but fixed_token accepts `split_by_character` + `split_by_character_only`
(POST /documents/text `chunking` = {"strategy":"fixed_token","params":{"chunk_token_size":N,
"split_by_character":"<delimiter>","split_by_character_only":true}}). So our code decides every
cut, inserts a delimiter between pieces, and LightRAG obeys. Offline benchmark result to
design for: pieces of <= ~350 cl100k tokens (header included) work best with mxbai.
Read `docs/09-retrieval-accuracy-plan.md`, `docs/research/decision-embed-rerank-chunk.md`,
`app/transcript_prep.py` (reuse its turn parser and header helpers) and
`scripts/chunk_embed_bench.py`.

## Build
1. `app/structure_chunker.py` (pure stdlib; tokens via tiktoken cl100k if importable else a
   conservative estimator that OVER-counts; expose which was used):
   `chunk_document(text, *, filename="", budget_tokens=350, header_tokens=25, kind="auto")
   -> ChunkPlan(pieces=[Piece(body, header, kind, section_path, token_estimate, part_index,
   part_total)], kind_detected, warnings, stats)`.
   Detection and cut rules, in priority order, never exceeding the budget (body + header):
   - Transcript (`(Label) text` turns, reuse transcript_prep's parser): cut at turn
     boundaries; group short turns until the budget; a turn longer than the budget splits at
     sentence breaks, then clauses, then words, never mid-word; header carries meeting/date/
     speakers as transcript_prep does.
   - Markdown/plain structure: headings (`#`, numbered `1.2.3`, ALL-CAPS short lines) start
     new pieces and feed a `section_path` in the header; paragraphs (blank-line separated) are
     the unit; lists stay with their lead-in line when they fit.
   - Tables (pipe rows, tab-separated, columnar runs of 2+ spaces, markdown tables with a
     `---` separator): keep the whole table in one piece if it fits; otherwise split BY ROW and
     repeat the header row (and separator) at the top of every piece; a row longer than the
     budget splits at cell boundaries with the row key repeated; never split inside a cell
     when avoidable. Caption lines directly above/below a table stay with it.
   - Repeated running headers/footers and page markers (a short line repeated >= 3 times) are
     dropped from piece bodies, counted in stats (never silently losing other text).
   - Plain unstructured text: paragraphs, then sentences, then words.
   - Header: deterministic `[Doc: <title> | <date> | <section path> | Part k/n]`, title/date
     from the filename pattern (`Title_`, dates), <= header_tokens, omitted fields dropped;
     NEVER LLM-generated. Option `headers=False`.
   - Invariant (property tests): removing headers and repeated table headers, the pieces'
     bodies concatenated equal the input modulo whitespace and dropped running headers; every
     piece <= budget; no mid-word cut; no piece is empty; the delimiter never occurs in the
     input (pick one that does not, fail loudly otherwise).
   - `render_for_lightrag(plan, delimiter) -> (text, chunking_params)`.
2. Integration, behind flags default OFF: `STRUCTURE_CHUNKING_ENABLED` (+ budget settings) in
   app/config.py; app/worker.py text branch: when on, chunk the extracted text with
   `chunk_document`, submit with the `chunking` params above; dedupe hash stays on the original
   bytes; provenance (kind, pieces, tokens p50/p95/max, dropped lines) in warnings_json via
   merge_warning_metadata, as transcript_prep does; if detection fails or anything raises, fall
   back to the unmodified text and record why. Do NOT touch the PDF/DOCX binary upload path
   (LightRAG parses those; an extraction route is a separate decision pending Hermes' shape
   report).
3. Benchmark: extend `scripts/chunk_embed_bench.py` configs with `"chunker": {"strategy":
   "structure", "budget_tokens": N, "headers": true}` using `app.structure_chunker`, so the
   same code is benchmarked and shipped; add configs to the grid file for structure-350 with
   and without headers (plus a header-shuffled placebo option `headers: "shuffled"`).
4. Loop: `scripts/overnight_loop.py` experiment key `structure_chunking` (budget, headers) so
   an arm can ingest through the same function (mirror the transcript_prep key).
5. Tests: fixtures for each rule (markdown table split by row with repeated header, wide
   columnar text, nested lists, transcript turns with 10-token turns and one 7k-token turn,
   headings with section paths, repeated footers, tab tables, numbers that look like timestamps,
   CRLF, unicode), the invariants, the worker flag on/off, the fallback, and a test that the
   delimiter handling matches the LightRAG params shape. Document the rules in the module
   docstring and docs/ops/loop README.
Hand back: diff summary, suite result, the rules you were unsure about, and what you could not
verify (e.g. how LightRAG treats a piece over chunk_token_size under split_by_character_only:
the lead will have Hermes read the image source; make oversize impossible by construction).
