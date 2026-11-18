# Worker brief: GL-10 scoped pass is faster and leak-free but answers unanswerable questions

Context (from Hermes' stack2 run on structure-cutover-7, R2 = COMPANY_FROM_QUESTION + COMPANY_SCOPE_ENFORCEMENT=retrieval):
- PASS: 0 citations from an unnamed company; 0 other-company fact codes in scoped answers (R0/R1 had 5); cross-company named-cited 18/18 (R0 0/18); median latency 15.7 s (R0 42.5 s); model_calls = 1 per scoped row; join now attributes 832/857 citations.
- FAIL: refused_unanswerable 0/12 (R0 1/12, R1 3/12). Scoped correct 31/58 (baseline 35/58, R1 35/58). Judge hallucinated count 42 (R1 37).
- Scoped pass drops: references_kept 614, dropped_other_company 246, unattributable 90, ambiguous 100, dropped_by_context_cap 100.

You may not read corpus or LightRAG content. Work from code, tests and the counts above.

## Tasks (code-only, offline, strict fakes using real shapes)
1. Find why unanswerable questions are not refused in the retrieval-scoped generation. Read how the regeneration call (mode bypass) builds its prompt: does it carry the same "answer only from the supplied context, say it is not in the documents otherwise" instruction that the normal /query path gives? Does it carry the response_type and user_prompt? Does it pass the kept chunks as context in a form the model treats as the only source? Fix so the scoped pass refuses exactly where the normal path would (same refusal wording the existing refusal detector recognises). Add tests with strict fakes that assert the generation request contains the refusal instruction and the kept chunks.
2. dropped_by_context_cap = 100: confirm what cap, and whether it can drop the chunk holding the answer while keeping lower-ranked ones. Chunks should be kept in retrieval rank order after the company filter, cap applied last. Report the cap value and ordering; fix if wrong.
3. second_model_call is true on 70/70 rows while model_calls is 1 per row. Hermes flagged the semantics. Make the flag mean "a second generation ran", rename or document if it means something else, and keep the scorer consistent.
4. Scorer: the unscoped_rows_changed_vs_baseline gate failed on 12 rows where only answer_chars differs (generation is not deterministic). Change the gate to compare structural fields (mode_used, rerank_applied, grounding_verdict, citations_scoped/final, status) and report answer_chars drift separately as informational. Do not hide it.
5. Ambiguous = 100 refs dropped in scoped answers: list which rule produced them (manifest.json and the two inbox-scanning-procedure copies are known). Confirm manifest.json cannot be attributed to a named company's answer by accident and that same-bytes duplicates within ONE company are kept, not dropped.

## Out of scope
No production changes. No new flags unless task 1 needs one (default off). Commit on your branch, hand back with test counts and a short list of what you changed and what you could not verify offline (Hermes will re-run on stack2).

Acceptance for the next stack2 run (Hermes will score): refused_unanswerable at least R0 (1/12) and ideally at least the normal-path rate; scoped_correct at least 35/58; leak and cross-company results unchanged.
