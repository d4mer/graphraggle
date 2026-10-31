# Hermes brief — LightRAG 1.5.7 probes that decide the company-scoping design (GL-09/GL-10, Plane GRAG-64/65)

Read-only on production. Do the probes on STACK2 (:9622) with synthetic or tiny throwaway documents you create yourself (a few paragraphs of invented text, 2 invented "companies"),
never production, never real corpus text in the report. Stack2 may need to be started (compose up) and stopped again afterwards; say what you did.
Counts, field names, status codes and response SHAPES only. Source of the questions: worker design note `docs/decisions/gl09-gl10-company-scoping-design.md`
(on origin after merge; the worker's branch `worker/gl09-gl10-company-scoping`, commit 8f964ae, is at /home/maverick/work/graphraggle-company-scoping on sigma: the lead will send you the file).
Operator priority: accuracy over speed; no timeouts or fallbacks.

## Questions (answer each as YES/NO + evidence: endpoint, request fields used, response field names)
Q1 Does `/documents/text` (and `/documents/upload`) in 1.5.7 accept per-document metadata, and is it returned per reference/chunk at query time? If yes, which field names?
Q2 Do `/query` and `/query/stream` accept a document-id set or path/file filter? Test in each mode: naive, local, global, mix. Which are honoured, which silently ignored?
Q3 Exact response shape with `only_need_context: true`; does `/query/stream` support `include_chunk_content`? List the reference fields returned (file_path, chunk ids, content).
Q4 If the same bytes are ingested twice under two different file paths, what exactly happens: HTTP code, status of the second, entity/relation counts before/after (store a pair of ~3-paragraph invented files and report counts only)?
   Then with a one-word change in the header line (different bytes): counts before/after, to cost the "company in the chunk header" option.
Q5 Does 1.5.7 support `WORKSPACE` (per-workspace isolation) and what does it cost (extra container? separate store dir?). Do not enable it anywhere but stack2 scratch.
Q6 Measured ingest duration of the 11 live documents (from your logs/state DB timestamps; no new run).
Q7 Confirm the upload-door citation string format (bare `<uuid>--name.ext`?) vs path-door (`/data/.../Company/name.ext`).

## Report
Section "GL-09 LIGHTRAG PROBES 2026-10-31" in ~/rag-project/docs/ops/runs/structure-cutover.md (or a new file if you prefer, say which); STATUS line 1.
Lead cannot be messaged from your seat: write the file and say so in your pane.
