# Worker brief — GL-07: synthetic second company for cross-company testing (Plane GRAG-62)

Base: branch from current `origin/master`, own branch/worktree. You do NOT read corpus or LightRAG content, and the synthetic documents must not be derived from
the real Solvay text. Build from document FORMATS and the topic list below only. Nothing is committed that quotes real data.

## Why
We must test cross-company retrieval, attribution, scoping and duplicate handling before any real second company exists (operator decision 2026-10-31: synthetic first).
Synthetic data proves the mechanics, not real-world quality; real documents will be needed for the final gates.

## Deliverable
A deterministic generator `scripts/make_synthetic_company.py` (stdlib + the repo's existing deps only) with tests, that writes a fictional company's document set to an output dir
(NOT committed; default `/home/maverick/work/synthetic-co/<Company>/`). Company name configurable (default `Northwind Chemicals`, invented).
Seeded randomness; same seed = byte-identical output.
### Document set (mirror the real corpus's FORMS, with invented content)
- 8 text transcripts (meeting style: `Speaker: text` turns, a date in the filename, 3 to 6 speakers, messy ASR-like phrasing allowed).
- 1 DOCX with tables (an order-book style specification: headings, a table with a header row, notes).
- 1 PDF (an integration analysis style document: numbered headings, tables, running header/footer lines repeated per page, all-caps short headings, at least 40 pages).
- 1 PPTX (about 25 slides: titles, bullet lists, 1 table, speaker notes on some slides, one grouped shape).
### Topics (invented facts, same BUSINESS themes as an ERP migration programme)
Cover these shared-process themes, each described differently by the synthetic company than a real one would: order-to-cash handling, master-data cleansing before migration,
flat-file interfaces between legacy and new ERP, cutover rehearsal and go/no-go criteria, user acceptance testing, and integration with a cross-business-unit order book.
At least 10 topics must be ones where a second company could plausibly say "we did X this way" so cross-company probes are meaningful. Also include 3 topics unique to this company
(nothing like them elsewhere) so scoped probes have unambiguous answers.
### Planted test cases (listed in a machine-readable manifest `manifest.json` written next to the files; the manifest holds facts, not corpus text from real data)
1. Two exact duplicate files within this company's set, plus ONE file whose bytes you will later copy into a second synthetic company (provide a flag `--emit-shared-file <path>` that writes the same bytes the real set shares, for the cross-company duplicate test).
2. For every planted fact: a unique invented fact string, the document and company that holds it, so labelled probes can be generated: scoped (names the company), cross-company (needs both companies), and unanswerable (the fact is in neither).
3. A second company: support `--company "Contoso Pharma"` producing a different set with different invented facts but overlapping themes (the interconnection cases), so the same generator builds both companies.
### Tests
Determinism; the manifest matches the files; every planted fact string occurs in its document; no fact string occurs in the other company's set; files open with python-docx / pypdf / python-pptx / plain text; full suite passes bare:
`.venv/bin/python -m pytest tests/ -q`.

## Hand-back
Branch + patch path, the output dir path for both companies, the manifest format, the exact command lines, test counts.
`rig queue create --destination orch-lead@graphraggle` with a summary. The lead will copy the generated files to the Thinkpad staging area for Hermes (synthetic data, no sensitivity).
