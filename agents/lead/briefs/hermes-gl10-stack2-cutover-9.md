# Hermes: GL-10 stack2 re-run on structure-cutover-9 (refusal prompt fix). Stack2 only, production untouched.

Send order: start this only after R3 on -7 is scored and reported. Same procedure as the -7 runs (docs/ops/gl10-stack2-runbook.md, section "What changed for the next run").

1. Build local/rag-gateway:structure-cutover-9 from ~/briefs/structure-cutover-9.bundle (sha256 910cd26d274712cf331051af0ee9b3fa9c79ebe62d78f06e19db6cbbd202d788), tag structure-cutover-9 only. Verify the bundle first. Back up the stack2 state DB (state/ingest.db) before starting. Start lightrag-server2 before the orchestrator (it is stopped at teardown).
2. Run R2 (COMPANY_FROM_QUESTION=true, COMPANY_SCOPE_ENFORCEMENT=retrieval) with COMPANY_SCOPED_HARD_REFUSAL=false (the default). Score with the tag scorer.
3. Run R2b: identical, but COMPANY_SCOPED_HARD_REFUSAL=true. Score it. Any drop in scoped_correct versus R2 is the false-refusal rate of the hard rule.
4. Control: run the 12 unscoped rows twice on the same image and report the natural answer_chars drift between the two runs (it tells us whether drift against R0 is just variance).
5. Report in structure-cutover.md (append with >>, dated .bak first) a table: refused_unanswerable, scoped_correct, hallucinated, other-company citations and codes, cross-company named-cited, latency median/p95, scoped_pass_model_calls, scoped_pass_generated, context_first_dropped_index distribution and context_unused_chars on rows that lost an answer, hard-refusal verdict counts (R2b).

Acceptance (what I will read): refused_unanswerable at least 1/12 and ideally 6/12 or better; scoped_correct at least 35/58; leaks 0; cross-company 18/18; model_calls 1 per scoped row.

Do not read or quote corpus content; counts and ids only. Report as soon as R2 is scored, before R2b.

Addendum (cutover-9 includes the follow-ups): one more check, after R2 and before R2b. Send ONE two-turn conversation through Open WebUI's /api/chat bridge (turn 1: a question naming a company; turn 2: a follow-up with no company, e.g. "and the second one?") and report whether turn 2 stays scoped to that company. Also report whether LightRAG mode bypass honours conversation_history (a one-line check in the LightRAG source or a probe; counts and yes/no only).
