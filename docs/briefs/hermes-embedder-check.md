# Hermes brief: embedder truncation check (read-only, names-only)

**Status: APPROVED by the operator 2026-10-16. READ-ONLY: no restart, no .env edit, no
ingest, no store change. Counts, names and numbers only; no corpus content leaves the host.**
Report to `~/rag-project/docs/ops/runs/embedder-check.md` (file, not rig send). Batch your
approvals up front.

Question: LightRAG chunks are 1200 tokens but mxbai-embed-large-v1 reads 512. Is the
rest of each chunk silently dropped from the vector?

1. Names and values (non-secret) of: EMBEDDING_MODEL, EMBEDDING_DIM, EMBEDDING_BINDING,
   EMBEDDING_BINDING_HOST, EMBEDDING_TOKEN_LIMIT / EMBEDDING_MAX_TOKEN_SIZE (any name),
   CHUNK_SIZE, CHUNK_OVERLAP_SIZE, TOP_K, CHUNK_TOP_K, MAX_TOTAL_TOKENS, MAX_ENTITY_TOKENS,
   MAX_RELATION_TOKENS, MIN_RERANK_SCORE, RERANK_*; from production and from stack2 `.env`
   and the LightRAG container's effective env (`docker exec ... env`, filtered to these names).
2. The embedding server (oMLX at the EMBEDDING host): its max sequence length for this
   model and what it does on longer input: truncate, error, or auto-chunk. Prove it with
   synthetic text only: send `[A*N]` style made-up input of ~300, ~500, ~900 and ~1500
   tokens to the embeddings endpoint (generic words, no corpus text) and report, per length,
   HTTP status, `usage.prompt_tokens` if returned, and the cosine similarity of the
   embedding of the full text against the embedding of its first 400 tokens. A similarity
   of ~1.0 for the 1500-token input means everything after the cut is invisible.
3. Does LightRAG 1.5.7 pass its own embedding token limit (grep the image source/env.example
   read-only for `embedding_token_limit`/`max_token_size`) and what value is in force?
4. One line: conclusion (truncates at N tokens / does not truncate / unknown) and the
   evidence for it.
