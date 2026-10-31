# Hermes brief — retrieval-depth test with the rerank timeout patch, stack2 only (GL-19/20/21, Plane GRAG-74/75/76)

Operator GO 2026-10-31: stack2 ONLY, production untouched, start AFTER your LightRAG probe brief (hermes-gl09-lightrag-probes.md) is reported. Operator priority: ACCURACY over speed.
There is no speed target; the only constraints are 0 rerank fallbacks and 0 timeouts at every hop. No production change comes from this brief; any production change needs a separate operator GO.

## Do
1. Build the staged rerank HTTP timeout patch (your next-GO candidate 2: aiohttp ClientTimeout from an env var, default high, in generic_rerank_api) into a STACK2-ONLY image tag. Record the image id and the diff size. Do not touch the production image tags.
2. Set stack2 timeouts consistently so a slow rerank can finish: RERANK_TIMEOUT above the longest expected batch, the new HTTP timeout above that, EMBEDDING_TIMEOUT per your candidate 1 (120 -> Func 120 / Worker 240 / HC 255), probe-runner guard above the slowest expected answer. List every value you set.
3. Run the held-out probe sets that exist today (solvay-dev/heldout, structure-dev/heldout; the 100-probe set) on stack2 at RELATED_CHUNK_NUMBER = 2, 3 and 5, one arm at a time, same probes, same judge. For each arm report counts per set (correct / refused_ok / partial / halluc), rerank-attempted vs fallback counts, timeouts at any hop (probe runner, LightRAG, reranker, embedding), p50/p95/max (recorded only, no target).
4. Report the mechanism if an arm gains accuracy: which probes flipped (ids only). Also report whether any arm ends with a fallback or timeout even with the patch, and where.
5. Also state the estimated Open WebUI timeout at the user-facing hop and what must change in the chain (Open WebUI -> gateway -> LightRAG -> reranker/LLM) for the best arm to complete there. Read-only: do not change Open WebUI.

## Rules
Stack2 only; production untouched (docker-inspect + .env-mtime snapshot start vs end, diff empty). Counts only, no corpus text. One bare command per approval. Stop and report if sigma is unusable; do not improvise settings.
## Report
Section "DEPTH TEST 2026-10-31" in structure-cutover.md; STATUS line 1. Lead cannot be messaged from your seat: write the file and say so in your pane.
