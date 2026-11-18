# Hermes: GL-10 stack2 run R2w on structure-cutover-10 (scoped retrieval over-fetch). Stack2 only, production untouched.

Why: R2 on cutover-9 refused 21 answerable questions (R0: 4) while hallucinations fell 43 -> 28. The company filter discards ~40% of what retrieval returns, so the asked company's answer chunk is often gone before the model sees it. cutover-10 adds an over-fetch (ask wider, then filter) plus one bounded widening round. It is OFF by default (multiplier 1); R2w turns it on. Full design and the settings are in docs/ops/gl10-stack2-runbook.md, section "What changed for the next run (cutover-9 follow-up: the scoped retrieval now over-fetches)", and docs/decisions/gl10-scoped-retrieval-design.md.

Same procedure as the cutover-9 runs.

1. Build local/rag-gateway:structure-cutover-10 from the bundle (sha256 fc05e7e84d6669996fc38771b4dc0843d59994c92f7b77dfa5ffe3368fee0b95, tag structure-cutover-10 = commit 31fec4d). The bundle is NOT on the Thinkpad yet: the lead must deliver it to ~/briefs/structure-cutover-10.bundle first. Verify the sha256 and `git bundle verify`; build from the tag only, tag the image structure-cutover-10 only. Back up the stack2 state DB (state/ingest.db) before starting. Start lightrag-server2 before the orchestrator.
2. Run R2w: R2 exactly as measured on cutover-9 plus the over-fetch lever, one variable against the R2 row:
   COMPANY_FROM_QUESTION=true, COMPANY_SCOPE_ENFORCEMENT=retrieval, COMPANY_SCOPED_CONTEXT_MAX_CHARS=12000, COMPANY_SCOPED_HARD_REFUSAL=false, the same COMPANY_ALIASES as the R2 run, and
   COMPANY_SCOPED_RETRIEVAL_MULTIPLIER=3, COMPANY_SCOPED_RETRIEVAL_FLOOR=3, COMPANY_SCOPED_RETRIEVAL_CEILING=200.
   Score with the tag scorer. Report as soon as R2w is scored.
3. Sanity row first: before the full run, send one scoped question and confirm scoped_pass_retrieval_multiplier=3 and scoped_pass_retrieval_top_k_requested is 3x the door's top_k (capped at 200) in the log. If the multiplier is not applied, stop and report; do not run the full set.
4. Control: run the 12 unscoped rows once on the cutover-10 image and confirm they are unchanged against the cutover-9 R2 row (the lever touches only the scoped pass).
5. Append to structure-cutover.md (dated .bak first, >>) a table comparing R0 / R2 (cutover-9) / R2w: refused_unanswerable, refused_wrong, scoped_correct, hallucinated, leaks, cross-company named-cited, latency median/p95, scoped_pass_model_calls, context_unused_chars, context_first_dropped_index distribution.

Sizing numbers to send back (counts only): per row scoped_pass_retrieval_top_k_requested, ..._chunk_top_k_requested, ..._retrieval_multiplier, ..._retrieval_rounds, ..._retrieval_calls, ..._retrieval_widened, ..._retrieval_widening_kept, ..._retrieval_widen_skipped_reason, ..._references_returned, and scoped_pass_retrieval_rounds_detail. From the scorer: widened_rows, widening_kept_rows, references_returned, retrieval_multipliers, retrieval_top_k_requested, retrieval_chunk_top_k_requested, retrieval_not_scaled_reasons, retrieval_widen_skipped_reasons.

Acceptance (what I will read): scoped_correct >= 35/58; refused_wrong back at the R0 level (4) or better; hallucinated not worse than 28; leaks 0; cross-company named-cited 18/18; per-row latency reported. A widened row must still report scoped_pass_model_calls=1; if any reports 2, say so, do not average it away.

Two readings the run must settle (report both, infer neither):
- Is /query/data's returned reference payload itself token-limited (max_total_tokens)? Signature: scoped_pass_references_returned stays flat while ..._top_k_requested climbs. Report that pair per row, and a one-line yes/no.
- The 90 unattributable drops are stack2 store contamination (Solvay-era documents with no documents row). The rule is deliberately unchanged; do not read that count as a scoping defect.

If R2w does not recover refusals, report the funnel for rows that are still refused_wrong (returned / dropped by reason / blocks kept / unused chars), counts and ids only, so we can tell "answer not retrieved" from "retrieved but the model refused".

Do not read or quote corpus content; counts and ids only. Production untouched, no deploy. Hermes->lead messaging is unreliable: write the result to a summary file and note its path.
