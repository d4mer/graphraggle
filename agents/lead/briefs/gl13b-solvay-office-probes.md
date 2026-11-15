# Worker brief — GL-13b: Solvay held-out probes for the PDF, DOCX and PPTX (Plane GRAG-68)

Operator decision 2026-11-15: option 1. Your existing source-reading exception is EXTENDED to exactly these three office documents, now staged (sha256-verified against the thinkpad staging copies) at
`/home/maverick/work/solvay-src/office/`: `SAP_S4_IntegrationAnalysis_Solvay_V1_22052026.pdf` (3,795,507 B), `OrderBook.docx` (3,534,132 B), `Solvay_OPR_ECCFlatfiles_v2.pptx` (3,180,819 B).
Same rules as GL-13: you may read these SOURCE files to author probes; you still never read corpus/LightRAG content or any store; probe files are NEVER committed and live under /home/maverick/work/solvay-probes/;
reports are counts only (no question text, no quotes, no answers); the files stay mode 600 and are not copied anywhere else. Do not paste their content into the queue row, a ticket or a commit.
Base: `git fetch && git rev-list --left-right --count HEAD...origin/master` must read `0 0` (origin/master 406cf0d or later); own branch/worktree if you change code; no push.
Not to be confused with the running gate-evidence investigation: finish or park that first if you are mid-way; hand each back as its own queue row.

## Why
GL-13 delivered a transcript-only fresh held-out set (51 probes, 21% of the pool shapes were pdf/docx/pptx in the brief but none could be authored, so 0 office rows). The go-live gate needs office-document
questions measured on the real documents: ok% >= 88 on a NEW held-out set, 0 hallucinations, refusal correctness >= 90%.

## Do
1. Author at least 30 NEW probes from these three documents (suggest PDF 14, DOCX 8, PPTX 8, balanced by what each holds), none duplicating a question in any consumed set (use your GL-13 overlap check and the `--forbid-overlap-with` option
   of `scripts/probe_set_check.py`; report the max overlap as a number). Shapes: fact lookup, table/numeric lookup (the PDF and DOCX tables), multi-fact synthesis, figures/captions or slide notes where they carry facts (PPTX speaker notes,
   grouped shapes), plus at least 6 unanswerables (plausible, absent from all sources) and 3 near-miss unanswerables that borrow a neighbouring fact's wording. Id prefixes new and distinct from every consumed set (GL-13 used hq, hr, cx, sa, rg, pf, ua, nm).
2. Same file format and field names as the GL-13 set (questions.jsonl, answer-key.jsonl, run-set.jsonl) plus `shape` in `pdf|docx|pptx|table|synthesis|unanswerable|near-miss`; keep the question file free of answers.
3. Evidence must be verbatim in the document AS THE REPO EXTRACTS IT (not as a viewer shows it): extract with the same extractors the worker uses in production (`app/pdf_extract.py` / DOCX / PPTX paths; poppler `pdftotext -layout` where available, pypdf fallback)
   and have `scripts/probe_set_check.py` validate evidence quotes against that extracted text (extend the script to take a pre-extracted text file per office source if it does not already; add tests). A quote that appears in the viewer but not in the extracted text is a trap: the
   system can never retrieve it. Report, counts only, how many candidate probes you dropped for that reason. Note warnings (running headers/footers dropped, table cell order) so scoring is not blamed for extraction loss.
4. Validate with the checker: failures 0; write the validation report counts-only; ids unique; no answers in the question file.
5. Do NOT run anything against production or stack2. Output to /home/maverick/work/solvay-probes/gl13b/ and report sha256s of the three files so a run can be pinned.

## Hand-back
`rig queue create --destination orch-lead@graphraggle --summary ...` with paths, counts per `shape`, overlap number, validation pass counts, dropped-for-extraction count, any code/test change (branch + SHA, suite result bare). Hermes copies and runs the set; the lead briefs that.
