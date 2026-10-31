# Hermes brief — GL-07 + GL-08: ingest the synthetic companies on stack2 and test duplicate handling (Plane GRAG-62, GRAG-63)

DRAFT, send only when (1) the worker has delivered the generator and the lead has copied the synthetic files to the Thinkpad, and (2) your probe brief and depth test are reported
(stack2 is shared). Operator priority: accuracy over speed. Stack2 ONLY; production untouched (docker-inspect + .env-mtime snapshot start vs end, diff empty).
The documents are synthetic (invented companies "Northwind Chemicals" and "Contoso Pharma", invented facts), so you may read them; the manifest lists planted facts.

## Do
1. Snapshot stack2 (store + state DB, sha256). Record before-counts.
2. Ingest Northwind Chemicals into stack2 with the production-equivalent settings (structure chunking on, BGE-M3), then Contoso Pharma, as `source_docs/<Company>/...` in stack2's layout.
   Per company report: documents by status, chunks, entities, relations, vector dim, errors. Report ingest duration per document.
3. GL-08 duplicate test, in this order, one at a time, counts only:
   a. With COMPANY_AWARE_DEDUPE=OFF (production default): add the planted shared file (same bytes) to the second company. Report the exact outcome (status, HTTP code, `dup-*` placeholder or not, store counts before/after) and whether the second company can be cited for it.
   b. With COMPANY_AWARE_DEDUPE=ON (stack2 only): repeat with a fresh copy. Report the same, and whether one indexed copy + two company rows results, and entity/relation duplication (counts).
   c. Report which of (a)/(b) leaves both companies retrievable; do not change any production setting.
4. Run the probe sets the worker's manifest generates (scoped, cross-company, unanswerable) against stack2 as-is (before the GL-09/10 code exists) to record the BASELINE: for each probe, which companies' sources came back (company-of-source counts), correct/refused/partial/halluc, and fallbacks/timeouts. This is the "before" for GL-10.
5. Write nothing about the content of any synthetic document beyond counts and probe ids.

## Rules
One bare command per approval; stop on first failure and report; do not retry a denied approval. Stack2 images only.
## Report
Section "GL-07/GL-08 2026-10-31" in structure-cutover.md; STATUS line 1. Lead cannot be messaged from your seat: write the file and say so in your pane.
