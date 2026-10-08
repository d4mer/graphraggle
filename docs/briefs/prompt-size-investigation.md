# Brief: why do some LLM prompts reach 45k-80k tokens when the context budget is ~32k? (READ-ONLY INVESTIGATION)

Repo: /home/maverick/work/graphraggle, branch packet-22-ll-seed. Do NOT change app code. Do NOT use live services (none run here).
Deliverable is a written report plus, optionally, a throwaway measurement script under scripts/ (not wired into anything).

## Why
oMLX (the LLM server) logs on the production box show: the normal LightRAG request has a ~32k-token prompt (matches
BRIDGE_MAX_TOTAL_TOKENS=32000, app/config.py:75) and decodes at 50-60 tok/s because the shared prefix is cached. A second
population of requests has 45k-80k-token prompts, misses the prefix cache, and decodes at 2.6-5.5 tok/s on a memory-pressured
machine. We need to know what builds the oversized prompts, so we can decide whether to cap them.
Observed fact only: prompt size per request, streaming, 56-1083 completion tokens. We do not know which gateway path sent them.

## Questions to answer (with file:line evidence for every claim)
1. LightRAG's `max_total_tokens` / `max_entity_tokens` / `max_relation_tokens` bound the retrieved CONTEXT. What else is
   added to the final LLM prompt outside that bound? Check in app/api.py (~L200-240 query payload, ~L323 extract_ollama_history,
   ~L476 and ~L634 history use, ~L1223-1250 other query path), app/multi_query.py, app/graph_synthesis.py, app/graphrag.py
   (calls with top_k 1 at L112/L192), app/graph_fusion.py, app/graph_native.py, app/rewrite.py, app/keywords.py, app/generation.py.
   Candidates: conversation_history (BRIDGE_HISTORY_TURNS=3: are assistant turns length-capped? does an earlier long answer
   or the citation block get re-sent?), the system prompt, graph/edge text added by synthesis or fusion, multi-query merging
   several retrievals into one prompt, transcript_like path (`max(req.top_k, 24)`), generate-document path.
2. For each code path that ends in an LLM call (directly or via LightRAG `/query*`), list: path name, what it sends,
   its upper bound in tokens if it has one, and whether any bound is missing.
3. Build a worst-case estimate: with default settings, what is the largest prompt each path can produce? Use a rough
   chars/4 token estimate and say so. Which path(s) can exceed 40k tokens, and under what input (long history, long transcript, ...)?
4. Which paths send the SAME prefix every time (cacheable) and which put variable text early in the prompt (breaks the prefix cache)?
   Only report what the code shows; do not guess about LightRAG internals you cannot read. LightRAG is not in this repo; if the
   answer depends on its code, say "depends on LightRAG v1.4.15 internals" and name the parameter.

## Acceptance
- File docs/ops/runs/prompt-size-investigation.md with sections: Findings (ranked by likelihood of explaining 45k-80k),
  Per-path table (Q2), Worst-case table (Q3), Cache-prefix notes (Q4), Recommendations (caps/changes, each as a one-line
  proposal with the setting or file it touches; do not implement), Unknowns.
- Every claim has file:line. If a question cannot be answered from this repo, say so under Unknowns.
- Do not edit anything except the new report (and an optional new script). Run `git status` at the end and paste it in your reply.
- Tests are not needed for this task; if you add a script, run it once and show its output.
