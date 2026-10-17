# Worker brief: bench fixes, probes-only mode, chunk-350 experiment (branch `chunking`)

Context: the offline benchmark (Hermes report ~/rag-project/docs/ops/runs/chunk-embed-bench.md,
summary in docs/09-retrieval-accuracy-plan.md) found: production's 1200/100 + mxbai is the worst
arm (94.6% of chunks past mxbai's real 512-token window); mxbai at 350 tokens + the existing
jina rerank is best; BGE-M3 does not pay for itself. Next: one confirming ingest on the second
stack with CHUNK_SIZE=350 / CHUNK_OVERLAP_SIZE=50, compared with production on the same probes.
Work only in /home/maverick/work/graphraggle-chunking. You may NOT read corpus, probes or
LightRAG data; synthetic fixtures only. Bare-env suite must pass; no network; socket guard stays.

1. `scripts/chunk_embed_bench.py`: Hermes had to patch two things in its scratch copy. Put them
   in the repo: (a) the rerank request body for oMLX `/v1/rerank` is the Cohere shape
   `{"model":..., "query":..., "documents":[str,...], "top_n":...}` (the Jina shape with
   `"results":[{"text":...}]` returned HTTP 422). Send the Cohere shape; parse the response
   tolerantly (`results[].index` + `relevance_score`, also `data[]`); fail loudly on an
   unknown shape; (b) `docs/ops/loop/chunk-embed-bench-configs.json` model ids = the served ids:
   `mxbai-embed-large-v1`, `bge-m3-mlx-fp16`, rerank `jina-reranker-v3-mlx`. Also make the
   OrderBook-style problem impossible: refuse a corpus entry whose extension is a binary
   office/pdf type (.docx/.pdf/.pptx) with a clear message ("extract to .txt first"). Tests.
2. `scripts/overnight_loop.py`: add `--probes-only`. It runs the probe + judge + recall steps
   against the given `--gateway-url`/`--lightrag-url` for ONE pseudo-arm named by
   `--arm-name` (default `probes-only`), and performs NO write of any kind: no `.env` write or
   backup, no container restart/exec, no store clear, no ingest, no compose command. It
   refuses to combine with an experiments file that has `reingest:true` or `env` keys, and
   it is allowed against production URLs (it is read-only), but prints the target. Add a test
   with the fake Ops that asserts zero calls to any mutating Ops method, and a test that
   `--probes-only` plus a reingest experiment is refused. Document it in the module docstring.
   Purpose: Hermes runs the SAME dev + held-out probes, same judge, against production as the
   comparison arm without changing production.
3. `docs/ops/loop/experiments-chunk350.json`: one arm `C350`: `env` `{"CHUNK_SIZE":"350",
   "CHUNK_OVERLAP_SIZE":"50"}`, `reingest:true`, no transcript_prep, no `chunking` key (fixed
   token, applies to every ingest), `_readme` explaining that the real names must be verified
   by Hermes in the LightRAG env.example, the stack2 flag set (same as the second-stack brief:
   --lightrag-url http://127.0.0.1:9622 --gateway-url http://127.0.0.1:8021 --lightrag-container
   lightrag-server2 --gateway-container gateway-api2 --lightrag-service lightrag-server2
   --gateway-service gateway-api2 --compose-file ~/rag-project/stack2/compose.yml
   --compose-project rag-chunk2 --env-file ~/rag-project/stack2/.env --worker-container none)
   and the probes-only command for production. Check that every key is in ENV_WHITELIST and
   that the loop restarts lightrag-server2 so the new chunk size takes effect before the ingest.
4. Rank by the honest column: in results.md rank arms by answerable-correct% (not
   `ok+0.5*partial`); keep the old column printed for comparability. Test.
Hand back: diff summary, suite result, anything you could not verify. Do not commit context.md.
