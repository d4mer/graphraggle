# Worker brief: scoped retrieval must over-fetch, because the company filter throws chunks away

Measured on stack2, structure-cutover-9, R2 (retrieval scoping, refusal prompt fix in): 
- hallucinated 43 (R0) -> 28: the prompt discipline works.
- but refused_wrong 4 (R0) -> 21 and scoped_correct 35 -> 26/58: the model now says "I do not have enough information" on questions that ARE answerable. Leaks 0, cross-company named-cited 18/18, one generation per row.
- Scoped pass census (70 rows): references_kept 614, dropped_other_company 246, unattributable 90, ambiguous 100, dropped_by_context_cap 116, context_unused_chars 94,576, context_truncated_blocks 0.

Reading (a hypothesis you must test from code, not assume): LightRAG's retrieval is ranked over ALL companies and we ask for the normal top_k / chunk_top_k, then discard about 40% of what came back (other-company, unattributable, ambiguous). The answer chunk for the named company often is not in the survivors, so an honest model now refuses where before it hallucinated or got lucky. The lever is recall before filtering, not the prompt.

Tasks (code-only, offline, strict fakes with real shapes; no corpus access):
1. In the scoped /query/data retrieval, over-fetch: send a larger top_k and chunk_top_k (a multiplier, flag/config default 3, ceiling configurable) when the question names companies, then filter, rank-preserve and apply the context cap as today. Count and log: requested vs returned vs kept chunks and refs, and the multiplier used. Never exceed the model context budget: the cap stays the limit.
2. If, after filtering, the kept set is below a floor (config, default 3 blocks) and the first retrieval was not already large, do ONE wider retrieval (multiplier x2) before generating. Log scoped_pass_retrieval_widened=true. Bound total retrieval calls at 2 and keep model_calls at 1.
3. Cap ordering stays a rank prefix. Add the new counters to the scorer projection and report them per run.
4. Check the reranker interplay: the scoped retrieval must keep enable_rerank behaviour consistent with the normal path so the over-fetch is reranked, not just longer. Report what the code does today.
5. Production note: the unattributable-90 drop is stack2 store contamination (Solvay-era docs in the shared index with no documents row). Production has no such contamination for Solvay questions. Do not weaken the unattributable rule to hide this; report only.

Flags: default off or at multiplier 1 so the shipped behaviour is unchanged until Hermes measures. Hand back with test counts and exactly what you could not verify offline. Hermes will re-run R2 on the new image; acceptance for that run: scoped_correct >= 35/58, refused_wrong back at R0 level or better, hallucinated not worse than the 28, leaks 0, cross 18/18, latency per row reported.
