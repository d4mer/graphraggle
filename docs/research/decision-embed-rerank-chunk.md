# Decision memo: embedder, reranker, chunking for GraphRAGgle (2026-10-16, revised for "maximise the existing stack")

Scope: web research only; no corpus or store files read. Repo facts come from `docs/05-known-good-config.md`, `docs/09-retrieval-accuracy-plan.md`, `docs/research/*`. Anything unsourced and not marked is my inference; UNVERIFIED marks what I could not confirm.

## 1. Recommendation

### 1A. PRIMARY: keep mxbai-embed-large-v1 and jina-reranker-v3; change chunking and retrieval settings only

Verdict on the models: keeping them does **not** cap accuracy badly once chunks fit the 512-token window. The current loss (about 57% of each chunk invisible) is a chunk-size error, not a model error. mxbai-embed-large-v1 is 1024-d, 512 tokens, MTEB average 64.68 per Mixedbread ([docs](https://mixedbread.ai/docs/embeddings/models)); jina-reranker-v3 reads up to 131K tokens listwise, BEIR 61.94 ([card](https://huggingface.co/jinaai/jina-reranker-v3)). The reranker is not the bottleneck at any chunk size we will use.

**Chunking**
- Strategy: `recursive_character`. Defaults in LightRAG's docs are 1200/100 with separators blank line, newline, CJK punctuation, space, empty ([docs](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)). Override separators (the default has no Latin sentence punctuation):
  `["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""]`
  Transcript turns are `(Speaker) text` lines, so `"\n"` keeps turns whole and packs many tiny turns into one chunk.
- **chunk_token_size = 350, overlap = 50** (LightRAG counts with a tiktoken-style tokenizer; mxbai uses BERT WordPiece, which usually produces more tokens on names, numbers and STT garbles). Budget: 350 content + up to 25-30 header + WordPiece inflation of 15-30% (my estimate, UNVERIFIED) lands at about 440-490 < 512. Hermes can verify in 5 minutes by tokenizing a sample of chunks with the mxbai tokenizer read-only: pass if the 99th percentile is <= 500. If it overshoots, drop to 300.
- **Single-paragraph documents:** the same separator list falls through to sentence boundaries, giving ~350-token sentence-aligned chunks from a 7k-token paragraph (about 20 chunks). STT without punctuation will fall to the `" "` fallback (mid-sentence cut); acceptable, with the 50-token overlap as protection. Do not use `custom` (needs code injection).
- **Header:** yes, but only deterministic and small: `title | date | speakers`, at most 25 tokens, prepended at chunk level (if the pipeline can do this via `transcript_prep`; otherwise skip). Evidence that headers help is contested: the one independent placebo-controlled test found LLM headers no better than shuffled ones and worse at tight budgets ([arXiv 2610.10170](https://arxiv.org/abs/2610.10170), via `docs/research/chunking-adversarial-review.md`; not transcripts). A short metadata line is the cheap part of that idea; the repo review judges it likely captures "which meeting, who was talking". Risk: the header gets extracted as entities. No LLM-written headers.
- Why 350 and not 400-450: the budget is hard, and truncation is silent (Hermes proof in `docs/09-retrieval-accuracy-plan.md`), so margin matters more than the extra 100 tokens. Confidence in the exact number is low; the "fit under 512 with margin" rule is high.

**LightRAG settings** (all are current defaults or small moves; defaults from the [docs](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md))
| Setting | Value | Reason |
|---|---|---|
| mode | `mix` (keep) | graph + vector; rerank on; `naive` loses the graph, `hybrid` loses chunks. |
| TOP_K | 60 (default) | entities/relations; nothing to justify change. |
| CHUNK_TOP_K | 40 (was 20) | chunks are ~3.4x smaller, so 40 x ~400 = 16k tokens, still below today's chunk payload; gives multi-place questions more candidate coverage. Whether LightRAG feeds more than `chunk_top_k` candidates into the reranker I did not verify: UNVERIFIED. |
| rerank | on, `RERANK_BY_DEFAULT=True`, keep jina v3 | 40 short chunks fit easily in a listwise window; latency of that call on M1 is UNVERIFIED. |
| MAX_TOTAL_TOKENS | 30000 (default, keep) | measured prefill ~450 tok/s, ~46 s median TTFT at ~32k prompt (`docs/05-known-good-config.md`): raising it adds latency linearly. |
| MAX_ENTITY / MAX_RELATION tokens | 6000 / 8000 (default) | no evidence to change. |

Hybrid/BM25: LightRAG's documented modes have no lexical retrieval ([docs](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)), so none is available with the current stack. This is the real structural gap for names and STT garbles; the graph and reranker compensate.

**Cheap non-infrastructure levers, ranked by worth**
1. **Gateway-side neighbour expansion (worth it, medium).** Retrieve small chunks (precise), then give the generator the adjacent chunk(s) of each hit. This addresses "chunk lacks context" without spending any embedder window, and the repo review ranks it above LLM headers; independent evidence favours expansion of hits over coarse-to-fine filtering ([arXiv 2610.10170](https://arxiv.org/abs/2610.10170)). Needs chunk order and document id from LightRAG's chunk store or API; availability through the gateway is UNVERIFIED (Hermes/worker check). Biggest value when answers need adjacent turns.
2. **Deterministic header (worth it, small).** As above.
3. **Query rewriting (marginal).** One extra local LLM call (~5-15 s at 58 tok/s decode) and LightRAG already runs its own keyword extraction; no transcript evidence. Skip unless the validation shows failing question types where terms differ from the transcript wording.
4. **LLM per-chunk headers, summaries, RAPTOR-lite (not worth it now).** Expensive (about 130 calls per long transcript in the repo research) and weakest independent evidence.

**Reindex:** necessary because chunking changes; embeddings dimension stays 1024, same model, so no vector-compatibility problem. Estimate: the corpus is ~110 chunks x ~1200 tokens, so ~130k tokens becomes about 380-420 chunks at 350/50. Per chunk the LLM extraction costs (assumed ~1.5-2k prompt tokens at 450 tok/s plus ~700 output tokens at 58 tok/s) about 15-20 s, so about 1.5-2.5 hours sequentially. These LLM figures are from `docs/05-known-good-config.md`; per-chunk token counts are my assumption. Cost scales linearly with corpus growth: the main price of small chunks is extraction call count (about 3.4x today's), and extraction quality for small chunks is untested on transcripts.

### 1B. UPGRADE option: one embedder swap to BAAI/bge-m3 (no reranker swap)

- Spec: 1024-d, 8192-token window, MIT, no query instruction required ([card](https://huggingface.co/BAAI/bge-m3)); MLX build `mlx-community/bge-m3-mlx-fp16`, ~1.1 GB, oMLX-compatible ([card](https://huggingface.co/mlx-community/bge-m3-mlx-fp16)); oMLX README lists BGE-M3 among supported embedding families and `/v1/embeddings` ([README](https://github.com/jundot/omlx)). 8/6/4-bit builds exist ([HF search](https://huggingface.co/models?search=bge-m3%20mlx)).
- What it adds over 1A: the window stops being a constraint, so chunks of 800-1200 can be fully embedded with no header budget, giving fewer chunks (fewer extraction calls) and more context per chunk. It also brings multilingual training (100+ languages) and claims better long-document retrieval ([abstract](https://arxiv.org/abs/2402.03216)).
- **Estimated gain over 1A: small to moderate; I cannot put a defensible number on it.** There is no benchmark on transcripts. Public numbers compare general retrieval: MTEB-multilingual style figures favour newer models (BGE-M3 ~59.56 vs Qwen3-0.6B 64.33 on a secondary blog, [Medium](https://medium.com/@mrAryanKumar/comparative-analysis-of-qwen-3-and-bge-m3-embedding-models-for-multilingual-information-retrieval-72c0e6895413), low rigour), and I found no clean mxbai-vs-BGE-M3 comparison. Most of the recoverable loss (57% invisible text) is already recovered by 1A. The residual advantage is mostly less dilution-vs-context trade-off and lower extraction cost, not higher raw embedding quality.
- **Costs/risks:** full reindex (same as 1A, so it adds no extra ingest if done together, but doubles the number of changed variables); pooling must be right on oMLX (Release 0.6.0 honours checkpoint pooling metadata, [release](https://newreleases.io/project/github/jundot/omlx/release/v0.6.0), not tested by me); oMLX embedding graph recompiles after ~3 s idle ([#266](https://github.com/jundot/omlx/issues/266), status unknown); +1.1 GB RAM; old vectors must never mix with new; rollback needs the backup.
- **Verdict: NOT worth it as the first move.** Do 1A, measure. Take the swap only if the validation below shows `evidence_in_context%` not rising or 350-token chunks hurting multi-fact questions (cross-document score 0.56 baseline), i.e. evidence that chunk size, not model quality, is the limit. If you do swap, BGE-M3 is the only candidate that qualifies (section 2).
- Reranker swap: **not worth it.** jina-reranker-v3.5 (BEIR 63.20 vs 62.10 on the card, MIRACL 74.11 vs 72.20, drop-in, [card](https://huggingface.co/jinaai/jina-reranker-v3.5)) is ~1-2 nDCG points on English/web benchmarks, MLX id unverified; no reason to spend a risk budget on it. Licence note: v3 is CC BY-NC 4.0, fine for personal use.

## 2. Why not the alternatives

**Embedders** (relevant only to the upgrade option)
- **Qwen3-Embedding 0.6B/4B/8B:** best published numbers ([regolo.ai](https://regolo.ai/embeddinggemma-2-vs-qwen3-embedding-benchmarks/)) and MLX builds exist ([HF](https://huggingface.co/mlx-community/Qwen3-Embedding-0.6B-4bit-DWQ)). Loses: not in oMLX's supported list; the last-token-pooling feature request was closed "not planned" and warns mean pooling silently yields bad vectors ([#2350](https://github.com/jundot/omlx/issues/2350)); a memory-growth issue on the 0.6B 4-bit build ([#684](https://github.com/jundot/omlx/issues/684)); needs query instruction prefixes, which LightRAG's openai binding does not apply.
- **EmbeddingGemma 2** (770M multimodal, 768d, 8192 ctx, Apache 2.0): requires prefixes `task: search result | query: ...` / `title: ... | text: ...` ([card](https://huggingface.co/google/embeddinggemma-2)); oMLX supports the architecture and an MLX collection exists ([collection](https://huggingface.co/collections/mlx-community/embeddinggemma-2)). Loses: no way to inject prefixes through LightRAG, newer and less proven.
- **jina-embeddings-v3/v4, nomic-embed-text-v2, modernbert-embed, gte-multilingual/ModernBERT, snowflake-arctic-embed-l-v2:** I did not confirm MLX builds or oMLX support in my searches (only the mlx-embeddings package list XLM-R/BERT/ModernBERT, [repo](https://github.com/Blaizzy/mlx-embeddings)); excluded as unverified, not as proven worse.
- **mxbai-embed-large-v2:** I could not find it; Mixedbread docs show v1 models only and a 512-token large-v1 ([docs](https://mixedbread.ai/docs/embeddings/models)). mxbai-embed-xsmall-v1 has 4.1K context but is 384-d and 22.7M parameters: too weak.
- **bge-m3** is the one upgrade candidate (section 1B).

**Rerankers**
- **bge-reranker-v2-m3** (Apache 2.0, XLM-R, MLX conversions exist): the card's example truncates at 512 and states no bigger limit ([card](https://huggingface.co/BAAI/bge-reranker-v2-m3)); with 350-token chunks that fits, but there is no gain over jina v3 here and pointwise scoring is weaker for multi-fact comparison. Fallback only.
- **Qwen3-Reranker 0.6B** (Apache 2.0, 32K, MTEB-R 65.80): no MLX build on the card, not in oMLX's rerank families (ModernBERT, XLM-RoBERTa per README), needs a yes/no prompt wrapper ([card](https://huggingface.co/Qwen/Qwen3-Reranker-0.6B)).
- **jina-reranker-m0, mxbai-rerank-v2:** no oMLX support evidence found; not researched further.

**Chunkers**
- `fixed_token`: cuts through turns. `semantic_vector`: calls the embedder per sentence at ingest and the REST API cannot set its `sentence_split_regex`, which STT text needs ([docs](https://github.com/HKUDS/LightRAG/blob/main/docs/ProgramingWithCore.md)). `paragraph_semantic`: 2000-token default, depends on paragraph breaks that STT output lacks.

## 3. Evidence on interactions

- **Chunk <= window (hard).** Proven at 512 for the current system (repo Hermes check). Everything in 1A follows from it.
- **Long-context embedders vs smaller chunks.** No study on noisy transcripts. Boundary quality matters when the baseline cuts mid-unit, which ours does ([Chroma](https://research.trychroma.com/evaluating-chunking), via repo research). The only LightRAG chunk-size study (WHO tables) saw naive extraction coverage fall as size grew from 600 to 4800 ([arXiv 2605.21974](https://arxiv.org/html/2605.21974)), a weak pointer toward smaller chunks.
- **Reranker depth vs chunk size.** Listwise jina v3 reads up to 64 docs per window (131K tokens); 40 short chunks are well within limits. Latency UNVERIFIED.
- **Hybrid/BM25.** Not in LightRAG; no workaround inside the stack.
- **Extraction cost.** Calls rise ~3.4x at 350 vs 1200; per-call prompt shrinks but fixed prompt overhead does not, so expect total time to rise roughly 2-3x, not 3.4x (my estimate). Smaller chunks may help or hurt small-model extraction: unmeasured.
- **Thin evidence:** MTEB, BEIR, MIRACL are English/web; the header study is Wikipedia/QASPER; no study covers multi-speaker STT. Treat all rankings as priors.

## 4. Risks and how to cut them

- **Reindex:** 1.5-2.5 h estimated (above); run on stack2 or a fresh store first (the plan's Phase 1 corpus), not production.
- **Dimension handling:** 1A keeps model and 1024-d, so no mismatch. 1B keeps 1024-d, which hides the mismatch: wipe vector stores and reingest completely; never mix old and new vectors.
- **Memory (RAM size not stated in the docs; ASSUMPTION):** oMLX default process limit is system RAM minus 8 GB, with `--memory-guard` tiers and pin/evict controls ([README](https://github.com/jundot/omlx)). 1A adds no memory. 1B adds ~1.1 GB (and removes mxbai, so roughly neutral). Hermes read-only checks: `sysctl hw.memsize`; oMLX version and model list/pinning; resident memory with Qwen3.6-35B + reranker + embedder loaded; mxbai WordPiece token counts of a chunk sample (p99 <= 500).
- **Header leaking into extraction:** check a small sample of extracted entities for header terms; if polluted, drop the header (neighbour expansion substitutes).
- **Rollback:** restore the pre-change backup of `state/ingest.db` and `lightrag_store/` and the old `.env` (the plan's Phase 5 manifest). No model files change in 1A.

## 5. Minimum validation (one decisive check)

On the frozen Phase 1 corpus and dev/held-out question set (60-100 questions, 3 draws), run the single 1A arm (recursive 350/50, 25-token header, CHUNK_TOP_K 40) against the existing C0 baseline with the paired bootstrap, preceded by the 5-minute token-count check (p99 chunk <= 500 mxbai tokens). Metric: `evidence_in_context%` primary, answerable accuracy and attribution secondary. **Pass:** evidence_in_context% up >= 8 absolute points with answerable accuracy not lower than C0 beyond the paired CI. **Fail:** no movement means retrieval is not the bottleneck (per the plan's failure reading); a drop in multi-fact/cross-document accuracy while single-fact rises is the trigger for 1B, not before. Duration: ~2 h ingest + probe runs, same session.

## 6. Ranked fallbacks

1. 1A with 300-token chunks if the token check overshoots.
2. 1A plus gateway neighbour expansion if retrieval hits are right but answers lack context.
3. 1B (BGE-M3 fp16 on oMLX, 800-token chunks, no header budget) if 1A plateaus.
4. EmbeddingGemma 2 only with a prefix shim; Qwen3-Embedding only off oMLX. Not recommended.
If the machine cannot hold the three models (not expected, nothing changes in 1A), swap the reranker to the bge-reranker-v2-m3 MLX build.

## 7. Confidence and unverified

| Decision | Confidence |
|---|---|
| Keep mxbai + jina v3 | High on feasibility; medium that it is sufficient (no measurement) |
| Chunk must fit 512 with margin | High |
| 350/50 exact numbers | Low-medium (tokenizer ratio unverified) |
| recursive_character + Latin separators | Medium |
| deterministic <= 25-token header | Low-medium (contested evidence) |
| CHUNK_TOP_K 40, mix, other settings | Low-medium |
| Neighbour expansion worth it | Medium (feasibility in the gateway unverified) |
| BGE-M3 upgrade "not first move" | Medium; the size of gain is unknown |

Unverified: M1 RAM; WordPiece vs LightRAG token ratio on this corpus; LightRAG 1.5.7 per-request separators override and whether `CHUNK_TOP_K` feeds rerank depth; gateway access to chunk order/adjacent chunks; rerank latency for 40 chunks; oMLX version and fixed status of #266/#684; jina-reranker-v3.5 MLX id; MLX/oMLX support for jina-embeddings, nomic-v2, ModernBERT-embed, arctic-l-v2, jina-m0, mxbai-rerank-v2; any transcript-specific benchmark of these models.
