# Hermes: how much faster is it with fewer chunks reaching the reranker (stack2, queries only)

Operator-approved 2026-11-19. Start AFTER the single-prompt comparison is reported (done) and only while the operator is not asking questions
(check the production query log first; if a user query ran in the last 10 min, wait). The 117-row dvgw run stays on hold.

## Why
Time to first token is minutes. In the production LightRAG log the gap between "Query nodes" and "Successfully reranked" is 130-240 s: 76 chunks took
about 172 s, 98 took about 239 s (about 2.3-2.4 s per chunk), and the reranker keeps only 20. The operator chose accuracy over speed in October, but
4-5 min per answer is too slow to use daily. Question: how much time do we win by sending fewer candidates to the reranker, and what does it cost in answer quality?

## What to do (stack2 only, config/request parameters only, no code change, no deploy)
1. Pick 10 real operator-style questions from the existing Solvay pool v2 (mix of single-fact, multi-fact, one unanswerable) plus the RAC question
   ("give me the steps to diagnose the RAC issue in Solvay"). Same 11 questions for every arm.
2. Arms, one variable at a time against the current setting (A = top_k 40, chunk_top_k 20): B = top_k 20, chunk_top_k 20; C = top_k 20, chunk_top_k 10.
   (If a different knob is the real cause of the candidate count, e.g. related-chunk selection, say so and add that arm.) Sequential, no other load on oMLX.
3. Per row record: candidates reaching the reranker, rerank seconds, first-token seconds, total seconds, references returned. Judge answer quality with the
   same judge and rubric as the earlier scoring; run the judge AFTER all timing runs so it does not disturb them.
4. Report counts and timings to a summary file (path to me): per-arm median/p95 first-token, ok%, rows where B/C lost an answer A got right (ids), and whether
   the key facts of the RAC answer survived (read it yourself; you may read content, the lead may not).

## Decision this feeds
Operator chooses: ship the faster setting, keep A, or try a smaller reranker. I will not propose a production change without the numbers.

Local models only. No production queries beyond reading its log.
