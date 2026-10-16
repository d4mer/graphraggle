# Worker brief: offline chunk x embedder x reranker benchmark (branch `chunking`)

Why: operator wants the best chunking + embedder + reranker combination for the existing
corpus without overnight ingests. The embedder (mxbai, 512 real window) silently truncates
1200-token chunks; BGE-M3 (8192 window) is now served by oMLX. We can measure the VECTOR
retrieval side offline: chunk the documents, embed chunks and questions, rank by cosine,
optionally rerank, and check whether the gold evidence quote is in the top-k chunks. No
LightRAG, no LLM extraction, minutes per configuration. Read `docs/research/decision-embed-
rerank-chunk.md` and `docs/09-retrieval-accuracy-plan.md`.

Rules: work only in /home/maverick/work/graphraggle-chunking. You may NOT read corpus, probes
or any LightRAG data; use synthetic fixtures (made-up text, a fake HTTP embedder). Hermes
will run the script on the Thinkpad where the data is. Bare-env suite must pass; no network
in tests; the socket guard in tests/conftest.py stays.

Build `scripts/chunk_embed_bench.py` (stdlib only, plus optional `tiktoken` if importable,
else a documented whitespace fallback; report which tokenizer was used):
1. Inputs: `--corpus corpus.json` (list of file paths, as the loop uses), `--probes a.jsonl
   [b.jsonl]` (items with `question`, `evidence`; skip items without evidence; accept the
   probe field names the loop uses: read `scripts/overnight_loop.py` `gold_evidence_of` and
   the probe loader and reuse their logic or import it), `--out dir`, `--embed-url`,
   `--embed-model`, `--rerank-url`, `--rerank-model`, `--api-key-env NAME` (read the key from
   the environment, never print it), `--configs configs.json`.
2. A config = {name, chunker: {strategy: "fixed_token"|"recursive", chunk_token_size,
   overlap, separators?}, embedder: {model, doc_prefix?, query_prefix?, max_input_tokens?},
   rerank: null | {model, top_n_in}}. The default configs file ships as
   `docs/ops/loop/chunk-embed-bench-configs.json`: current (fixed_token 1200/100 + mxbai),
   mxbai fixed 350, mxbai recursive 350/50 with separators ["\n\n","\n",". ","? ","! ","; ",
   ", "," ",""], bge-m3 recursive 350/50, 600/80, 800/100, 1200/100, plus a `+rerank` variant
   of the best two. Implement a fixed-token chunker equivalent to LightRAG's default and a
   recursive chunker that splits on the separator list in order, packs pieces up to the token
   size with overlap, and never cuts mid-word. State in the docstring that it approximates
   LightRAG's chunkers (Hermes will compare chunk counts to production: 108 chunks for 1200/100).
3. Embedding client: POST /v1/embeddings (OpenAI shape), batches of <=16 inputs, retries with
   backoff, timeouts; cache embeddings on disk keyed by sha256(model+prefix+text) so reruns
   and added configs are free; log counts and timings only, never text.
4. Metrics per config (probe-level then aggregated; also by probe type if present):
   `recall@k` for k in {5,10,20,40}: gold evidence (whitespace/unicode-normalised substring,
   same normalisation as the loop's recall probe) is contained in one of the top-k chunks; a
   `split` flag when the quote spans two adjacent chunks (count it as hit@k only if both are
   in the top-k, report separately how many quotes are cut by a chunk boundary: that is the
   cost of small chunks); MRR; mean chunk count; chunk token percentiles; with rerank:
   candidates = top `top_n_in` by cosine, reorder by /v1/rerank (Jina shape), same metrics.
   Paired bootstrap CI for the difference between any two named configs (`--compare A B`).
5. Output: `results.json` and a `results.md` table; only counts, ids and numbers, no chunk or
   question text, no file names (positions only).
6. Tests with a fake embedder (deterministic hash-bag vectors) and synthetic documents:
   chunker boundaries, overlap, token limits, no mid-word cuts, the split-quote accounting,
   the cache, bootstrap, and a guard that refuses a URL on port 9621 or 8020.
Hand back: diff summary, suite result, anything unverified. Do not commit context.md; the
lead commits.
