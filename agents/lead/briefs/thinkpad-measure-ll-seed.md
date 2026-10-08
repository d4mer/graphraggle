# Measure the `ll_keywords` seed (GRAG-41 follow-up 1) — instruction for the Thinkpad agent

**For:** the Hermes dev agent on the Thinkpad (production host). **From:** orch-lead, via the operator.
**Code under test:** branch `packet-22-ll-seed` (`d4c31cc`, pushed to origin), off `packet-21-standalone-query`.
Flag `BRIDGE_REWRITE_SEED_LL` (default **false**) appends the user's original wording as the last
`ll_keywords` entry when the query was rewritten. Reason: Part C case c01 lost cited documents
(5/8 down to 4/8) when the rewrite narrowed the query. This job decides whether the flag helps.

## Hard rules

1. Data stays local. Only the local oMLX server and production LightRAG; no outside model, API,
   or judge. No prompt, history, rewritten text, keyword text or answer text in logs (enums, counts,
   booleans only). Raw text may go only in local, gitignored report files.
2. **Do not deploy and do not touch production's gateway, `.env` or running containers.** Use a
   separate test container on port 8021 (as in Part C), built from `packet-22-ll-seed`, reading
   production LightRAG read-only. Production stays on image `31395c52e22e`.
3. Sequential requests only. Stop at the first 5xx, any request over 900 s, or any oMLX memory
   pressure (500s, "Insufficient Memory"), and report.
4. Latency is not a concern. Thinking is not changed server-wide; keep `BRIDGE_REWRITE_NO_THINK=true`
   as production has it. Do not edit `OPENAI_LLM_EXTRA_BODY`.
5. Fresh wording for every request (the answer-cache key includes query, keywords and user_prompt but
   not history). Use your freshness guard against all previously sent strings.

## What to run

Reuse your Part C harness (`~/rag-project/c21/c_run.py`), the same 10 conversations and the same
standalone references (P3) from `~/rag-project/c21/standalone.json`. Two arms, both with
rewrite on, no-think on, history 3, keyword supply on:

- **S0:** `BRIDGE_REWRITE_SEED_LL=false` (today's production behaviour).
- **S1:** `BRIDGE_REWRITE_SEED_LL=true`.

Run **two draws per conversation per arm** with different fresh wordings, alternating arms
(S0, S1, S0, S1 ...) so oMLX load and drift do not favour one. Add the operator's real follow-up
conversations if his file is larger than 10.

Record per request: status, wall time, canned or not, cited file names, answer length,
`keyword_source`, `rewrite_source`, `rewrite_ll_seeded` (must be true only in S1 and only when
`rewrite_source=rewritten`). Compute, per conversation and per arm, overlap = |cited ∩ P3| / |P3|,
the mean per arm, and the per-conversation delta S1 minus S0. Report c01 separately, and report
the spread between the two draws so noise is visible.

## Report

Write `docs/ops/runs/stage0-ll-seed-report.md` (gitignored): commit hashes tested, the table
above, a plain verdict. Say clearly: does S1 recover c01 (5/8) without lowering any other
conversation? Treat a mean difference under the draw-to-draw spread as "no evidence", not as a win.
Also write a local side-by-side file of S0 vs S1 answer openings and Sources for the operator.
Recommend **flip the default / keep off / needs more data**, and say what you did not do.
Do not deploy; the operator decides.
