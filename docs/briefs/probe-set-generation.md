# Brief: build the probe set from the ingested documents (Gate 1 of docs/ops/release-plan-first-use.md)

To: ops-hermes@thinkpad-ops. Read-only on the stack. No deploys, no config changes, no `pipeline_status` calls.

## Why
The operator cannot write 30-50 test questions with provenance, so you do it. The set will be used to judge whether
the RAG stack gives precise, correct answers (Gate 2). It must be answerable from specific documents so a result
can be checked against the source, not against the system's own output.

## What to produce
`~/rag-project/probe/probe-set-v1.jsonl` (outside git, document text is private) plus a short summary
`~/rag-project/probe/probe-set-v1-summary.md`. One JSON object per line:
`id`, `question`, `source_doc` (file name as in the ingest folder), `location` (page/section/sheet), `evidence`
(short verbatim quote, <=300 chars, that answers it), `expected_answer` (1-3 sentences), `type`, `company` (if scoping applies).

## How
1. List the ingest/source folder on the Thinkpad (`~/rag-project`, source_docs / uploads). Use only docs whose ingest status
   is fully processed (check via gateway document status API, not `pipeline_status`).
2. Read the ORIGINAL files, not LightRAG output, so provenance is independent of the system under test.
3. Target 40 questions (minimum 30), spread across as many docs and file types (PDF/DOCX/PPTX/XLSX) and companies as exist.
   No doc may supply more than 4 questions. Mix by `type`: ~50% single-fact lookup, ~20% multi-fact within one doc,
   ~15% cross-document (list every source_doc), ~10% follow-up pairs (give the preceding question as `history`),
   ~5% unanswerable-from-corpus (expected_answer = "not in the documents"; these test for hallucination).
4. Write questions the way a person would ask them, not by copying the document's wording. Avoid yes/no questions.
   Every answerable question needs a verbatim `evidence` quote you actually located; if you cannot find the quote, drop the question.
5. Do NOT run the questions through the RAG system and do NOT use its answers to write `expected_answer`.
6. Data stays on the Thinkpad. Do not send document text to any outside service or paste it into messages to me.

## Done when
- >=30 lines, each with all fields, every `evidence` verified by grep against the original file text (script it and report counts: checked / passed).
- Summary lists counts by type, file type, company, doc, and any docs skipped and why.
- Reply to me (`rig send orch-lead@graphraggle`) with ONLY: file paths, counts, grep-verification result. No document contents.
