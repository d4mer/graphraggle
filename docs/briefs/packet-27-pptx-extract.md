# Packet 27 — richer PPTX extraction in the ingest worker

Work in your own worktree: `git fetch origin && git worktree add ../graphraggle-p27 -b packet-27-impl origin/packet-27-pptx-extract`. Push `packet-27-impl` when done. Do NOT switch the shared checkout.
Tests: `RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q` (479 pass on master).
Install the library in the dev venv for tests: `.venv/bin/pip install python-pptx` (pin the version you tested with).

## Goal (operator decision 2026-10-12: "richer extraction")
.pptx files already ingest, but LightRAG's built-in extractor (`lightrag/parser/legacy/extractors.py::_extract_pptx`) is crude:
it concatenates `shape.text` of top-level shapes only. It therefore DROPS: table cells (graphic frames have no `.text`), text inside grouped shapes,
speaker notes, and any notion of which slide text came from. Answers cannot cite a slide, and tables / notes are not searchable.

Make the worker extract PPTX itself, with python-pptx, into structured text, and submit it through the existing TEXT path
(`/documents/text`, `file_source=<path>`), exactly as `.txt/.md` are submitted. This also sidesteps LightRAG's file-upload path for pptx.

## Required behaviour
1. New module `app/pptx_extract.py` with a pure function `extract_pptx_text(path: Path) -> str` (no network). Output is plain text/markdown, per slide, in slide order:
   ```
   ## Slide N: <title or "(untitled)">
   <body text, one paragraph per line, in reading order: top-to-bottom, then left-to-right by shape position>
   [Table]
   col1 | col2 | col3        (one line per row; escape/flatten newlines inside cells)
   [Notes]
   <speaker notes text>
   ```
   - Title = the slide title placeholder if any; do not repeat it in the body.
   - Recurse into group shapes. Include table cells. Include text frames of all shapes with text, skip empty ones. Skip pure pictures (note `[Image]` NOT required).
   - Chart: if a chart has a title / category labels / series names include them as `[Chart] title; categories; series` (best-effort, wrapped in try/except, never fail the file for a chart).
   - Hidden slides: include, tag title with ` (hidden)` if detectable; best-effort.
   - Deterministic output; collapse runs of blank lines; strip control characters.
2. In `app/worker.py` `submit_file`: for `.pptx` call `extract_pptx_text` and submit via `/documents/text`. Reuse the existing text-payload plumbing (`normalize_text_submission_payload`, the same `file_source`, same state-DB fields).
   Add `.pptx` to a new set (e.g. `PPTX_EXTENSIONS`), NOT to `TEXT_EXTENSIONS` (which means "read bytes as text").
   Feature flag `WORKER_PPTX_EXTRACT_ENABLED` (config.py, default **true**); false = current behaviour (upload path).
3. Failure handling: if python-pptx is missing, the file is corrupt, or extraction returns empty text -> the row becomes `failed` with `error_stage="extract"`, `error_code` one of
   `pptx_lib_missing` / `pptx_extract_failed` / `pptx_empty`, retryable=False, a clear message; never crash `scan_once`. A password-protected or non-ZIP file falls in `pptx_extract_failed`.
4. Dedupe/hash logic unchanged (hash is of the file bytes). Do not touch the upload path for pdf/docx/xlsx.
5. `requirements.txt`: add the pinned `python-pptx==<version you tested>` (the file is now tracked in the repo; `Containerfile` installs from it). Do not change anything else in those two files.
6. Size guard: refuse files over a configurable limit `WORKER_PPTX_MAX_BYTES` (default 100 MB -> `pptx_extract_failed`, message "too large"), and cap extracted text at `WORKER_PPTX_MAX_CHARS` (default 2,000,000, truncate with a marker line). Mention the zip-bomb class: python-pptx loads the whole zip; the byte limit is the guard.

## Tests (offline; build .pptx fixtures in the test with python-pptx itself — no binary files in git)
- Slide order, title vs body, `(untitled)` fallback.
- Table rows rendered one per line with ` | ` separators; cell newlines flattened.
- Group shape text included (nested two levels).
- Speaker notes included under `[Notes]`; slide without notes has no `[Notes]`.
- Reading order: two text boxes placed left/right and top/bottom come out in the documented order.
- Chart best-effort does not raise on a chart fixture or a chart-less deck.
- Empty deck -> `pptx_empty`; garbage bytes with `.pptx` suffix -> `pptx_extract_failed`; over-size -> failed; flag off -> upload path used (fake client asserts).
- Worker integration with a fake client: a .pptx goes to `/documents/text` with `file_source` = the path and the extracted text; status `submitted`; NO `/documents/upload` call.
- Existing 479 tests still pass. Show full pytest output.

## Out of scope
Images/diagram description or OCR (separate packet), reindexing the 8 pptx already in LightRAG (lead/ops decision after you ship), compose/Containerfile changes beyond requirements.txt, gateway/bridge code.

## Report back
Files changed, test count, sample of `extract_pptx_text` output for a small fixture (paste it), and any python-pptx behaviours you could not test offline (e.g. real-world decks with embedded OLE objects, SmartArt) so the lead can ask ops to try real decks.
