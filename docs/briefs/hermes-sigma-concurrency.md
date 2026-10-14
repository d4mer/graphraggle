# Brief - sigma Strata concurrency test (no stack changes)

Run AFTER the 6-transcript batch is finished and LightRAG is idle (status_counts processing=0, pending=0). Goal: how much does
sigma's throughput improve when LightRAG sends N requests at once, so we can set MAX_ASYNC. Nothing in ~/rag-project config changes.

Server: http://192.168.1.195:1234/v1/chat/completions, header `Authorization: Bearer 1234`, model `qwen3.8-flash`.
Write a small script ~/rag-project/probe/sigma_concurrency.py (stdlib urllib + threading; no httpx):
- Prompt: take the first ~1200 tokens (~4800 chars) of "Cross-GBU OMP Prioritization Update-transcript.txt" from ~/rag-project/staging/Solvay/
  and ask: "List every named entity (people, systems, organisations, concepts) as lines `entity|type|one-sentence description`,
  then every relationship as `source|target|description`." temperature 0, max_tokens 2000, stream False.
- For N in 1,2,3,4 (N identical requests sent simultaneously), 3 rounds each, 10 s pause between rounds. Record per request:
  wall seconds, usage.prompt_tokens, usage.completion_tokens. Per round: wall seconds for all N, aggregate completion tokens/s
  (sum completion tokens / round wall). Report mean per N, and the speedup vs N=1.
- Also record, before the test and after, the established connections to sigma port 1234 (from sigma: `ssh 100.64.0.3 "ss -tn state established '( sport = :1234 )'"`)
  so we see who else is using it. Do not touch anything on sigma beyond that read-only command.
- Report as heredoc to ~/rag-project/docs/ops/runs/sigma-concurrency.md: table N, mean wall, mean completion tok/s per request, aggregate tok/s, speedup;
  any errors/timeouts/HTTP 429/5xx; the connection lists. Message me the table only.
Constraints: only the script file + report written; 300 s timeout per request; stop on first error pattern and report. No approval for anything else.
