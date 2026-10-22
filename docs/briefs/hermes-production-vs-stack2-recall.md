# Hermes brief: production-vs-stack2 retrieval comparison (same 100 probes, read-only)

**Status: APPROVED by the operator on 2026-10-22 (this action only: the read-only 100-probe run against production
`:9621`). The cutover brief is NOT approved. Production is only READ (queries, no writes). Never `--allow-production`.**
Approval prompts appear in your pane; the operator must be at the terminal and pick the time window.
Report to `~/rag-project/docs/ops/runs/recall-production-vs-stack2.md` (files, not `rig send`); STATUS on line 1.

## Why
Attempt 3 (`stack2-structure-ingest-3.md`) ingested the 10 documents through the structure path: 129 chunks, all
byte-exact, 10/0 processed. On the 100 evidence probes stack2 put the evidence in the returned context for
**91/100**. Nobody has run the same probes against production, so 91/100 has no baseline. This run produces the
number on the same questions, so the operator can decide whether a cutover is worth bringing. It measures whether
the evidence reaches the context, not whether the final answer is right (the judge is not involved).

## What to run
Use the same helper and the same 100 probes as the stack2 run (`probe/run_stack2_recall.py`; 68 solvay-v2 + 32
structure-v2), pointed at **production LightRAG `:9621` directly** (mode=mix, `only_need_context=true`). Do NOT go
through the gateway (`:8020`): the bridge has no context-only mode, and stack2 was queried directly too.

Like-for-like rules (record each as a fact in the report, do not "fix" them):
- Send **no** top_k / chunk_top_k / token budgets in the request, exactly as the stack2 run did, so each side uses
  its own container env. Before the run, print (names and values, no keys) the query-side env of both
  `lightrag-server` and `lightrag-server2` and list **every difference**. Expected differences: the embedding
  binding and the data. Anything else (rerank, top_k, chunk_top_k, max_total_tokens, entity/relation budgets,
  gleaning) that differs is a confound: report it and **stop for the lead** before querying.
- Record which embedding model and dimension production's LightRAG actually runs (`.env`/startup log). If it is not
  BGE-M3 / 1024, say so prominently: production's retrieval then differs in the embedder as well as the chunking, and
  the comparison is "production as-is vs structure+BGE-M3".
- Same judge-free test as before: a probe counts when every normalised evidence quote appears verbatim in the
  returned context. Same normalisation, same ` [...] ` split for cross-doc probes.

## Load safety (production is live)
- Sequential, one query at a time, 5 s pause between queries. Stack2 took 6 to 52 s per query (p50 43 s), so expect
  about 70 to 90 minutes. oMLX (embeddings, rerank, LLM) is shared with live users.
- Before the first query: gateway `/health` 200, LightRAG `/health` OK, documents 10 processed / 0 failed.
- **Stop and report** on: any query over 120 s, any 5xx, a gateway `/health` that is not 200, an oMLX memory-pressure
  signal, or the operator asking you to stop. Report how many probes ran.
- If the operator is about to use the stack, they tell you; pause between probes, do not abandon the run silently.

## Hard rules
- Read only. No writes to production, no `.env` edit, no restart, no document submission, no `--allow-production`.
- At start and end confirm :9621 processed=10/failed=0, container ids unchanged, `.env` mtime unchanged. Any change
  is a stop-and-report.
- No corpus, question, evidence or context text in the report or in any file you write: ids, booleans, sizes and
  timings only. Do not paste LightRAG's context into the report.
- One bare command per approval. Chained or looped shell commands (for/while, `&&`, pipes with several commands)
  show up as "+ N commands" dialogs that time out unattended; use a single script file that was already approved or a
  single command, and run the helper as a background process with a log.

## Output (counts and ids only)
1. Setup: query-side env diff of the two LightRAG servers (list of differing names), production's embedding model and
   dimension, probe file sha256s, helper sha256, the time window used.
2. Production: `evidence_in_context` overall, per probe set (solvay-dev, solvay-heldout, structure-dev,
   structure-heldout) and per type (single-fact, combine, multi-fact, section-disambig, numeric-run,
   section-lookup, follow-up, page-boundary). Query seconds min / p50 / max. Errors.
3. **Paired table, both sides**: for every probe id, production true/false and stack2 true/false (stack2's
   results are in `scratch/stack2-structure8/recall/*.results.jsonl`; reuse them, do not re-run stack2 unless the
   env diff shows it was run under different settings). Totals: both, production only, stack2 only, neither. List the
   ids in "production only" and "stack2 only".
4. Returned-context size per probe (characters and tokens, if the helper has them), median per side. A side that
   wins by returning far more context should be visible.
5. Anything surprising, with exact error text (no document text).
Write the paired per-probe booleans to `~/rag-project/docs/ops/runs/recall-production-vs-stack2.jsonl`
(`{"id":..., "production":bool, "stack2":bool}` per line) so the lead can compute the paired difference and its
interval.

## What happens after
The lead reads the report and the jsonl, computes the paired difference with a bootstrap interval, and brings the
operator the number next to the cutover brief (`hermes-structure-cutover.md`, also a DRAFT). Nothing here authorises a
cutover or any change to production.
