# Worker brief — URGENT, small: GL-10 scoped retrieval never sends the question (found on stack2 R2)

Base: local master 6f2f6d3 or later; own branch, no push, bare suite must pass.

## What happened
Hermes' stack2 run R2 (COMPANY_SCOPE_ENFORCEMENT=retrieval) stopped at its FIRST scoped probe with HTTP 500. Gateway log: POST lightrag-server2:9621/query/data returned 400 Bad Request, then the fallback POST /query returned
422 Unprocessable Entity, raised as an unhandled httpx.HTTPStatusError (which is why the user sees a 500).

## Root cause (read from the merged code)
The retrieval payload never contains the question. In app/scoped_pass.py, retrieve_context() sends {**payload_base, "include_references": True, "include_chunk_content": True} to /query/data, and _retrieve_via_query() sends
the same to /query. payload_base has no "query" key: the gateway door builds scoped_payload with only mode, top_k, chunk_top_k (and history) (app/api.py around line 1493), and the bridge door removes "query" on purpose
(scoped_base excludes "query", around line 580). The function parameter `question` is never put in the body. LightRAG requires "query", so both calls are rejected. Your tests did not catch it because the fake client accepts any body.

## Do
1. Put "query": question into BOTH retrieval payloads (retrieve_context and _retrieve_via_query).
2. Make the test fakes strict so this class of bug cannot hide: a fake LightRAG client for the scoped-pass tests that REJECTS a body without a non-empty "query", and rejects unknown top-level keys, using the request field list
   Hermes read from the production 1.5.7 source: query, mode, only_need_context, only_need_prompt, response_type, top_k, chunk_top_k, max_entity_tokens, max_relation_tokens, max_total_tokens, hl_keywords, ll_keywords,
   conversation_history, user_prompt, disable_user_prompt_prefix, enable_rerank, include_references, include_chunk_content, include_progress, stream. Add a test that FAILS on the old code. Run the generation call (mode bypass)
   body and the keyword-hint payload through the same strict fake, for both doors (gateway /query and the bridge).
3. A LightRAG HTTP error must never become an unhandled 500 for the user: if both retrieval routes fail, degrade to the citations-only behaviour, log scoped_pass_declined_reason with the status codes, and say so in the answer
   path instead of raising. Add a test.
4. When /query/data or /query returns 4xx, log the response status and the validation message (no chunk text) so the next failure is diagnosable from the gateway log alone.
5. Flag-off stays byte-identical. Nothing else changes.

## Hand-back
rig queue create to orch-lead@graphraggle with SHAs and test counts. I cut structure-cutover-6 immediately for Hermes to re-run R2 and R3; R0 and R1 results stand because they never enter the scoped pass.
