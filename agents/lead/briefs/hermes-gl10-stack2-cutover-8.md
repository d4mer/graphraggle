# Hermes: GL-10 stack2 re-run on structure-cutover-8 (refusal prompt fix). Stack2 only, production untouched.

Send order: start this only after R3 on -7 is scored and reported. Same procedure as the -7 runs (docs/ops/gl10-stack2-runbook.md, section "What changed for the next run").

1. Build local/rag-gateway:structure-cutover-8 from ~/briefs/structure-cutover-8.bundle (sha256 55d730612c5c4c0c53ceb3fc49bfbddefbbf01d645679de8afb659542f26aa8b), tag structure-cutover-8 only. Verify the bundle first. Back up the stack2 state DB (state/ingest.db) before starting. Start lightrag-server2 before the orchestrator (it is stopped at teardown).
2. Run R2 (COMPANY_FROM_QUESTION=true, COMPANY_SCOPE_ENFORCEMENT=retrieval) with COMPANY_SCOPED_HARD_REFUSAL=false (the default). Score with the tag scorer.
3. Run R2b: identical, but COMPANY_SCOPED_HARD_REFUSAL=true. Score it. Any drop in scoped_correct versus R2 is the false-refusal rate of the hard rule.
4. Control: run the 12 unscoped rows twice on the same image and report the natural answer_chars drift between the two runs (it tells us whether drift against R0 is just variance).
5. Report in structure-cutover.md (append with >>, dated .bak first) a table: refused_unanswerable, scoped_correct, hallucinated, other-company citations and codes, cross-company named-cited, latency median/p95, scoped_pass_model_calls, scoped_pass_generated, context_first_dropped_index distribution and context_unused_chars on rows that lost an answer, hard-refusal verdict counts (R2b).

Acceptance (what I will read): refused_unanswerable at least 1/12 and ideally 6/12 or better; scoped_correct at least 35/58; leaks 0; cross-company 18/18; model_calls 1 per scoped row.

Do not read or quote corpus content; counts and ids only. Report as soon as R2 is scored, before R2b.
