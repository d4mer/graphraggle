# Hermes brief: offline chunk x embedder x reranker benchmark

**Status: DRAFT, NOT APPROVED.** Needs the operator's written go before it is sent.
Offline, read-only for every stack: it only chunks local files and calls oMLX
`/v1/embeddings` (and optionally `/v1/rerank`). No LightRAG ingest, no stack2 change,
no production change, no restart. Never port 9621 or 8020 (the script refuses them).
Report to `~/rag-project/docs/ops/runs/chunk-embed-bench.md` (STATUS line first). Content
never leaves the host: numbers only. Write results to files; no `rig send`.

## Why
The embedder reads 512 tokens; production chunks are 1200. BGE-M3 (8192 window) is now
served by oMLX. This grid measures, in minutes, which chunk size and embedder put the gold
evidence into the top-k chunks, with and without jina-reranker-v3, on the real corpus and
the frozen probes. It decides the configuration; one confirming ingest comes after.

## Steps
1. Tools: `~/rag-project/docs/chunkrun3.tgz`, sha256
   `ef8d3e8573f66b6a042d8e25264acd5bd56e33c0644f7f87cb6b21cb6b77b578`, commit dbd5c13,
   manifest `chunkrun3-MANIFEST.txt`. Verify, extract to `scratch/chunkrun3`. Run the script
   inside a throwaway container of the LightRAG image (or any image with python3 and
   `tiktoken` with the cache you copied for the inventory) so token counts use the same
   tiktoken encoding as the server; report which tokenizer was used. If neither works, say
   so; the whitespace fallback undercounts tokens and must not be used for the decision.
2. Pre-checks (report only): oMLX serves `mxbai-embed-large-v1` and `bge-m3` (exact model ids
   in `/v1/models`), the embedding dimension of each, the M1 RAM if you can read it, and the
   `/v1/rerank` response shape for `jina-reranker-v3-mlx` (keys only; the script expects a
   Jina shape: `results: [{index, relevance_score}]`). Prove BGE-M3's window with synthetic text
   only: embed ~3000-token made-up text and a variant that differs only at token ~2500;
   cosine ~1.0 means it truncates. If any check fails, stop and report; do not improvise.
3. Inputs: `scratch/phase1/corpus.json` (the 10 files), the FROZEN dev probes
   `scratch/phase1/probes/frozen/solvay-dev-v2.jsonl` (sha256 9f47e48d...). Use the dev set
   ONLY; the held-out set is not read. Check the production chunk count for the
   `current-1200-100-mxbai` arm is close to 108; report the number.
4. Run the 9 arms in `docs/ops/loop/chunk-embed-bench-configs.json` (API key via
   `--api-key-env`, never printed). Embedding load is on the shared M1; run serially,
   estimate the time first (about 110-400 chunks per config plus 34 evidence probes), and stop
   with a note if the estimate exceeds 90 minutes. Add, if cheap, a `bge-m3-recursive-600-80
   +rerank` arm.
5. Compare with the script's bootstrap: best bge-m3 arm vs best mxbai arm vs current;
   +rerank vs not. Report per arm: chunk count, token p50/p95, over_embedder_window_pct,
   recall@5/10/20/40, MRR, cut_by_boundary_pct, by probe type; and the paired CIs.
6. Leave nothing running. Keep the embedding cache in `scratch/phase1/bench-cache`.

## Hard rules
- Probes/quotes/chunks are never printed or copied; masked or no file names.
- No change to production or stack2; no ingest; do not stop the worker; no `.env` edits.
- Stop at the first failed check and report it.
