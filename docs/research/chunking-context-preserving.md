# Context-preserving chunking for a transcript-heavy LightRAG corpus

Date: 2026-10-16. Scope: research only. No corpus, LightRAG store or probe files were read.
Method: web searches and fetches by a research agent. Items marked **[UNVERIFIED]** were seen only in secondary summaries or snippets. Items marked **[NOT FOUND]** are gaps in what I could locate.

## 1. Executive summary

**Recommendation.** Do not look for a smarter boundary detector as the main fix. The evidence says boundary choice is a weak lever, and restoring document context to each chunk is a stronger one.

- Boundary-choice evidence: Vectara/NAACL found fixed-size chunking at least as good as semantic chunking on realistic data. Chroma found simple recursive splitting competitive with LLM chunkers.
- Context-restoration evidence: Anthropic reports a large reduction in top-20 retrieval failures from prepending document-situating context to each chunk. This is a vendor claim on its own mixed benchmark.

For this stack the best plan has three parts:

1. Split transcripts on speaker-turn boundaries into small chunks of about 250 to 350 tokens. The 512-token embedder forces this. Do not split inside a turn unless the turn is huge.
2. Prepend a short LLM-generated, document-aware context header to every chunk (contextual retrieval). Add deterministic metadata: file, meeting date, speaker roster, section label.
3. Keep a document-level summary and a topic-segment layer as a second index tier, linked to chunks (small-to-big).

**Ranked shortlist, top 3 to test:**

| Rank | Method | Why |
|---|---|---|
| 1 | Turn-aware chunking plus per-chunk LLM context header plus deterministic metadata header (contextual retrieval adapted to transcripts) | Strongest reported gains. Works with a 512-token embedder if chunks are about 300 tokens. Fits a `custom` chunker or an external preprocessor. Cost is one local-LLM call per chunk, and the document prefix can be cached. |
| 2 | LLM or embedding topic segmentation into coherent segments (SeCom-style, TextTiling-like fallback), then chunk within segments, with segment title and summary as header | The only approach with direct conversational-memory evidence (SeCom, ICLR 2025). Answers the operator's "decide where to chunk" request. Segments must be split down to 512 tokens or fewer for the embedder. |
| 3 | Document-level and segment-level summaries as extra indexed units (RAPTOR-lite or "summary-first") | Helps whole-meeting questions that no 300-token chunk can answer. Cheap: a handful of calls per document. Output must itself fit 512 tokens. |

**Not recommended now:**
- Late chunking. It needs a long-context embedder, and mxbai at about 512 tokens cannot do it. Swapping the embedder means a full re-embed and a different model, and the measured gain is modest (about 2 to 4% relative nDCG).
- Proposition chunking. High cost, and it is awkward on conversational text.
- Fine-tuned chunkers (MoC, Meta-Chunking). They were trained for written prose.

**Caveat on the whole report.** I found no peer-reviewed study of chunking for noisy meeting transcripts in RAG. The transcript-specific advice below is extrapolated from conversational-memory work and from general results. Test it before adopting it.

## 2. Method-by-method

### 2.1 Contextual Retrieval (Anthropic)
- **Source:** "Introducing Contextual Retrieval", Anthropic, Sept 2024. https://www.anthropic.com/news/contextual-retrieval
- **Mechanism:** For each chunk, an LLM sees the whole document and the chunk. It writes a 50 to 100 token "situating" context. That text is prepended before embedding and before BM25 indexing.
- **Reported gains (top-20 retrieval failure rate, 1 minus recall@20):**

| Configuration | Failure rate | Reduction |
|---|---|---|
| Baseline | 5.7% | none |
| Contextual embeddings | 3.7% | 35% |
| Plus contextual BM25 | 2.9% | 49% |
| Plus reranking (150 candidates down to 20) | 1.9% | 67% |

  Anthropic recommends passing 20 chunks to the generator rather than 5 or 10.
- **Aggregation unclear:** Secondary sources disagree on whether 67% is an average over domains or the best embedding configuration. **[UNVERIFIED]** Per-domain variation was reported as high as 84% for fiction and as low as 41% for vague codebase queries. **[UNVERIFIED, Medium analysis]** https://medium.com/almond-ai/responding-to-anthropics-contextual-retrieval-for-rag-apps-why-context-is-not-all-you-need-af503985aa55
- **Cost:** Anthropic estimates about $1.02 per million document tokens with prompt caching. The assumptions are 800-token chunks and 8k-token documents. With a local LLM the cost is time, not dollars. Estimate: a 40k-token transcript at 300-token chunks is about 130 calls, each reading a cached 40k prefix. This is feasible if the serving stack supports prefix caching. Otherwise each call re-reads the whole document. **[Estimate, not sourced]**
- **Independent comparison:** Merola and Singh, "Reconstructing Context" (2025), ran contextual retrieval and late chunking head to head with Phi-3.5-mini as the context generator. Contextual retrieval kept semantic coherence better at higher compute. Late chunking was cheaper but lost relevance and completeness. https://arxiv.org/abs/2504.19754 I only saw the abstract and excerpts, not the tables.
- **Failure modes:**
  - It can blur the distinction between chunks of the same document. A taxonomy study found contextualized chunking helps in-corpus retrieval but consistently hurts in-document retrieval on GutenQA. https://arxiv.org/pdf/2602.16974 (Beyond Chunk-Then-Embed, 2026.)
  - The context text eats embedder budget. With a 512-token limit, 100 context tokens leave about 400 for content.
  - Small local LLMs may hallucinate context. Validate on a sample.
  - Prefix caching must be present, or indexing is slow.
- **Fit to transcripts:** Transcripts are the high-implicit-context case. A line like "yes, let's go with the second option" carries no topic. Expect larger gains than on encyclopedic text, but this is a hypothesis.

### 2.2 Late chunking (Jina)
- **Source:** Günther et al., "Late Chunking: Contextual Chunk Embeddings Using Long-Context Embedding Models", Jina AI, 2024. https://arxiv.org/abs/2409.04701
- **Mechanism:** Embed the whole document at token level with a long-context model. Apply chunk boundaries after the transformer, then mean-pool per chunk. Each chunk vector therefore carries document context. No LLM calls.
- **Gains:** About 2.7 to 3.6% nDCG@10 improvement on BEIR. **[UNVERIFIED against paper tables; alphaXiv summary]** https://alphaxiv.org/overview/2409.04701v3 Jina says the gain grows with document length.
- **Limits:**
  - It needs a long-context embedder (Jina examples use 8192 tokens). Longer documents use overlapping macro-chunks.
  - **Conflicts with the 512-token embedder.** mxbai-embed-large-v1 cannot do this.
  - Jina-embeddings-v3 is reported as CC BY-NC 4.0. **[UNVERIFIED]**
  - Results vary by embedding model and dataset (Merola and Singh). With BGE-M3 on NFCorpus and Stella-V5 on MS MARCO, conventional chunking won in some cases.
  - It only improves the vector side. BM25 and the LLM that reads the chunk get no context.
- **Related:** ConTEB and InSeNT (Conti et al., EMNLP 2025) show standard embedders do badly when document-wide context is needed. They train a contextual embedder with late-chunking pooling. https://arxiv.org/abs/2505.24782 The released ModernBERT model is flagged experimental with regressions on standard tasks. Not production-ready.
- **Verdict:** Park it. Revisit only if you change the embedder for other reasons.

### 2.3 Fixed, recursive and semantic (embedding-breakpoint) chunking
- Qu, Tu and Bao, "Is Semantic Chunking Worth the Computational Cost?" (Findings of NAACL 2025). Semantic chunking has a slight edge only on artificially stitched datasets. On realistic data, fixed-size chunking did better. With GPT-4o, answer differences were negligible. https://arxiv.org/abs/2410.13070
- Chroma, "Evaluating Chunking" (2024). Token-level precision, recall and IoU metrics. Well-parameterized RecursiveCharacterTextSplitter performs well. Their ClusterSemanticChunker and LLMChunker are competitive. https://research.trychroma.com/evaluating-chunking The numbers quoted in blog posts (recursive 7.0% IoU, LLM chunker 3.9%, at 200 tokens, k=5) are **[UNVERIFIED]**.
- **Implication:** The 5 percentile/stddev/IQR/gradient variants in LightRAG are unlikely to be the big win. They are cheap to include as arms, but expect small differences.
- **Transcript-specific risk:** Embedding-breakpoint methods work on sentence similarity. With 40 to 51% of turns under 10 tokens ("yeah", "right"), per-turn embeddings are noisy and breakpoints will be spurious. Group turns into windows before measuring similarity. **[Inference]**

### 2.4 LLM-decided boundaries (LumberChunker, Meta-Chunking, MoC, LLMChunker)
- LumberChunker feeds consecutive paragraphs to an LLM and asks where content diverges. Cost and instruction-following demands are its main drawbacks. Meta-Chunking uses margin sampling or perplexity to find logical boundaries. MoC (Zhao et al., 2025) trains a small router and chunker (Qwen2.5-1.5B fine-tuned on 20k chunked QA pairs) and proposes boundary clarity and chunk stickiness metrics. https://arxiv.org/abs/2503.09600 https://arxiv.org/abs/2410.12788
- All the comparisons come from one group (IAAR-Shanghai), on QA datasets such as CRUD. **No independent benchmark found.** **[NOT FOUND]**
- **Failure modes:** Output must be validated, because LLMs drop or alter text when asked to reproduce it. Use the LLM to return turn indices of boundaries, never to rewrite text. Cost scales with document length. Segment sizes are unconstrained, so a 512-token cap needs a second pass.
- **Verdict:** An LLM that returns boundary indices over speaker-turn windows is a reasonable engine for the topic-segmentation arm (section 3). The fine-tuned variants are not worth the effort for prose-trained models on STT text.

### 2.5 Propositions / Dense X
- Chen et al., "Dense X Retrieval" (EMNLP 2024). Proposition-level indexing beat passage-level retrieval, especially for unsupervised retrievers, and improves QA under a fixed token budget. https://arxiv.org/abs/2312.06648
- **Cost:** One LLM rewrite per passage, producing many small units. Index size grows several times over.
- **Transcripts:** Atomic, self-contained propositions need coreference resolution ("he said we should switch" becomes a named person). That is exactly the lossy step with noisy STT and anonymous speaker labels. The paper's gains are on Wikipedia. **[NOT FOUND]** for dialogue.
- Fine-grained units also multiply the graph extraction work in LightRAG (each unit becomes an extraction call).
- **Verdict:** Low priority.

### 2.6 Hierarchical / parent-document / small-to-big
- **Mechanism:** Embed small units for precision, then return the parent (larger window or whole segment) to the LLM for context. LlamaIndex and LangChain have implementations. I did not find a primary-source benchmark for this pattern. **[NOT FOUND]**
- **Fit:** It resolves the 512-token embedder constraint cleanly. Embed 300-token chunks, but hand the generator the parent segment or neighbouring turns. In LightRAG, the stored chunk text is what gets returned, so this needs either a custom retrieval step or storing the expanded text. Check LightRAG 1.5.7 behavior before relying on it. **[Not verified against LightRAG source]**

### 2.7 RAPTOR and tree summarization
- Sarthi et al., ICLR 2024. Recursively cluster chunks, summarize clusters, build a tree, retrieve across levels. With GPT-4, 20 points absolute improvement on QuALITY (82.6% accuracy per a leaderboard snapshot, **[UNVERIFIED]**). Documents average about 5,000 tokens. https://arxiv.org/abs/2401.18059
- **Gains are for questions needing multi-part synthesis.** For narrow factual lookups, leaf retrieval already works.
- **Cost:** Summaries at every level. Cluster-based summarization requires re-clustering as the corpus grows. For a growing corpus, build the tree per document only (no cross-document clusters), which keeps incremental ingest cheap.
- **Verdict:** Use a shallow per-document version: segment summaries plus one document summary. See rank 3.

### 2.8 Document summaries prepended / sliding-window summaries
- The cheapest context device: one summary per document (or per segment) is prepended to each chunk or indexed as its own unit. This is the manual form of contextual retrieval. Anthropic's version is chunk-specific, which is the more targeted variant.
- Risks: the shared summary text makes all chunks of a document look alike, which hurts in-document discrimination (the same effect reported for contextualized chunking above). Prefer a short, chunk-specific line over a long shared summary.
- A rolling summary ("what has been discussed so far") is a reasonable fit for transcripts, because later turns refer back to earlier ones. No benchmark found. **[NOT FOUND]**

### 2.9 Topic segmentation (TextTiling, embedding-based, LLM-based)
- Classic TextTiling and C99 detect lexical cohesion shifts. Embedding variants threshold similarity between adjacent windows (the same mechanism as LightRAG's semantic_vector).
- Dialogue segmentation benchmarks: DialSeg711, TIAGE, SuperDialSeg (named in the SeCom paper).
- A 2026 paper evaluates LoRA-fine-tuned LLMs for multi-level table-of-contents generation on meeting and lecture transcripts. Fine-tuned models beat baselines, but zero-shot LLMs show substantial gaps. https://arxiv.org/abs/2601.02128 **[abstract-level only]**
- "When F1 Fails" (2025) argues standard boundary F1 is a poor metric for dialogue segmentation and proposes granularity-aware evaluation. https://arxiv.org/abs/2512.17083 **[abstract not read]**
- **Implication:** Zero-shot local LLMs may segment unreliably. Validate boundaries against a hand-labelled sample of 5 to 10 meetings before relying on them.

## 3. Transcript and meeting specifics

- **SeCom** (Pan et al., Microsoft/Tsinghua, ICLR 2025). Memory units are topical segments from a conversation segmentation model, then denoised with LLMLingua-2 compression. Segment-level memory beat turn-level, session-level and summary-based memory on LOCOMO and Long-MT-Bench+. https://arxiv.org/abs/2502.05589 I did not obtain the numbers. **[UNVERIFIED]** Note it targets two-party chatbot dialogue, not multi-speaker STT meetings.
- **Meeting recap work** (Asthana et al., 2023, predates the window): segmenting transcripts into topical chunks before summarization reduced hallucination and improved coherence. https://arxiv.org/abs/2307.15793
- **Speaker-turn aware chunking:** I found no benchmark. It is a sensible design constraint: never cut inside a turn, keep speaker labels on every line, repeat the label if a long turn is split. **[Design inference]**
- **Metadata:** Putting date, meeting title, and speaker roster into the embedded text (not only into stored fields) is a cheap form of contextualization. The 1-to-2-speaker files should be flagged (e.g. "monologue") since label-based reasoning does not apply.
- **Noisy STT:** The one relevant study I found concerns spoken queries, not noisy corpora. ASR-corrupted query entities were the main failure in multi-hop RAG (arXiv 2608.22872). **[NOT FOUND]** for corpus-side STT noise with chunking. Practical consequences: keep BM25/keyword search in the mix for names that STT mangles, and consider a speaker/name normalization pass.
- **Short turns:** With 40 to 51% of turns under 10 tokens, chunk by token budget while respecting turn edges. Short backchannel turns ("mm-hmm") carry no retrieval value and can be dropped from the embedded text but kept in the stored text. **[Design inference; test it]**
- **Summarization-first:** For whole-meeting questions ("what was decided about X across the meeting"), a per-meeting summary plus decision/action list as extra indexed units is the practical answer. Action-item-driven summarization of long meetings is studied in arXiv 2312.17581 **[not read]**.

## 4. Graph RAG specifics

- **Chunk size vs extraction recall:** The GraphRAG paper says longer chunks need fewer LLM calls but suffer degraded recall for information early in the chunk. https://arxiv.org/abs/2404.16130 Blog posts relay a figure that 600-token chunks yielded about twice as many entity references as 2,400-token chunks on HotPotQA. **[UNVERIFIED, secondary]** Microsoft's guidance cites a default near 300 or 1200 tokens depending on version. **[conflicting sources; check the version you run]**
- **LightRAG-specific:** A 2026 study varied chunk_token_size across 600, 1200, 2400 and 4800 on tabular WHO data and found schema-constrained extraction stable, schema-free extraction declining with size. https://arxiv.org/abs/2605.21974 One domain, tables, so weak evidence for transcripts.
- **Implication for this project:** The embedding limit (about 512) pushes chunks smaller than the current 1200. Smaller chunks likely raise entity recall per token but cost more extraction calls and lose cross-chunk relations. **[Inference]** Whole-document extraction by a long-context local LLM is not supported by the available evidence: no source shows it beats chunked extraction, and the recall-degradation result argues against it.
- **Separate the two roles.** In LightRAG one chunk serves as (a) the extraction unit, (b) the vector-retrieval unit, (c) the text shown to the answering LLM. These can want different sizes. If the `custom` chunker allows it, extraction could use larger windows and retrieval smaller ones. **Check whether 1.5.7 allows this; I did not verify.**
- **Context headers help extraction too:** A chunk that starts with "Meeting: weekly infra sync, 2026-03-02, speakers: A, B, C. Context: discussion of the database migration" lets the extractor resolve "it" and "the second option". This is a hypothesis; no direct evidence found. **[NOT FOUND]**
- **Document-level entity resolution and chunk-to-document links:** LightRAG already merges same-name entities across chunks by name. Speaker labels like "(Speaker 2)" are not stable across files and will pollute the graph as a shared entity. Strongly consider stripping or namespacing speaker labels per document in extraction input. **[Inference, high-value, cheap]** HippoRAG 2 keeps passage nodes alongside phrase nodes and reports gains from combining them (average passage recall@5 78.2 vs 63.8 for HippoRAG 1, Llama-3.3-70B). https://arxiv.org/abs/2502.14802 **[numbers via excerpt]**
- **LazyGraphRAG:** Microsoft reports indexing cost equal to vector RAG and 0.1% of full GraphRAG, deferring LLM work to query time. https://www.microsoft.com/en-us/research/blog/lazygraphrag/ Vendor claim. Relevant only if extraction cost becomes the limiting factor.

## 5. What the evidence shows vs vendor claims

| Claim | Source type | Status |
|---|---|---|
| Contextual retrieval cuts failures 35/49/67% | Anthropic's own benchmark, aggregation unclear | Directionally supported by an independent paper (Merola and Singh), but I did not see its numbers. Expect smaller gains on your data. |
| Late chunking improves retrieval | Jina authors, BEIR | Modest (about 3% nDCG), model-dependent, and sometimes loses to conventional chunking in independent testing. |
| Semantic chunking beats fixed | Many blogs | Not supported on realistic data (Qu et al.). |
| LLM chunkers beat rule-based | IAAR-Shanghai group only | No independent replication found. |
| Propositions beat passages | Chen et al., Wikipedia | Peer-reviewed, but not tested on dialogue. |
| RAPTOR large gains | Sarthi et al., QuALITY | Gains concentrate on synthesis questions over single long documents. |
| Segment-level memory beats turn and session level | SeCom, LOCOMO | Peer-reviewed, chatbot dialogue, numbers not verified by me. |

Cross-cutting points:
- Benchmarks for chunking are often built on short, self-contained passages, which hides the context-loss problem (ConTEB authors make this point).
- With a strong answering LLM, chunking differences shrink (Qu et al.), because the generator compensates. For a local LLM this compensation is weaker, so chunk quality may matter more for you. **[Inference]**
- Fixed-size chunking remains a hard baseline to beat. The honest expectation is that context headers matter more than boundary placement.

## 6. Ranking against this stack

Score = expected gain on transcripts times feasibility (LightRAG `custom` chunker or external preprocessing before POST /documents/text, local LLM, 512-token embedder).

| Method | Expected gain | Feasibility | 512-token conflict | Overall |
|---|---|---|---|---|
| Turn-aware chunks plus context header plus metadata | High | High | Limits chunk to about 300 tokens + header | 1 |
| LLM or embedding topic segmentation, then sub-chunking | Medium-high | Medium (needs validation) | Segments must be split to 512 or fewer | 2 |
| Document and segment summaries as extra units | Medium (high for global questions) | High | Summaries must be 512 tokens or fewer | 3 |
| Small-to-big expansion at retrieval time | Medium | Medium (depends on LightRAG internals) | Solves it | 4 |
| Per-document speaker-label namespacing for extraction | Medium for graph | High | None | do alongside 1 |
| Semantic-vector or percentile breakpoints | Low-medium | High (built in) | Fine | cheap control arm |
| Propositions | Low-medium | Low | Fine | 5 |
| Late chunking | Low-medium | Low | Blocked by embedder | 6 |
| RAPTOR full cross-document tree | Medium | Low (incremental corpus) | Fine | 7 |
| Fine-tuned chunkers (MoC) | Unknown | Low | Fine | 8 |

Integration notes:
- External preprocessing (build chunk text with headers, POST it) is lower risk than relying on LightRAG internals. Verify how `custom` returns chunks and whether per-chunk metadata survives. **[Not verified against 1.5.7 source]**
- Preprocessing must be deterministic and cached per document hash so re-ingest does not repeat LLM work.
- Use the LLM only to emit structured outputs (boundary turn indices, short header strings), never to rewrite transcript text.

## 7. Minimal fair evaluation design

**Pitfalls to avoid:**
- A small corpus makes chunking irrelevant. If the whole index fits in top-k, every method retrieves the right text. Use enough documents that retrieval is selective, and include distractor meetings that discuss similar topics.
- Chunk-boundary metrics rewarding boundaries instead of answers (MoC's boundary clarity is an intrinsic proxy, not an outcome).
- LLM-generated questions that copy the chunk's own wording favor small chunks. Chroma generates queries with excerpts and scores token-level overlap, which avoids dependence on chunk identity. https://research.trychroma.com/evaluating-chunking
- Document-level recall hides chunking differences because any chunk of the right document counts.
- Changing several things at once (chunker, embedder, reranker).

**Design:**
1. **Corpus:** All current transcripts (not 2 documents), with several similar-topic meetings. Hold embedder, reranker, LLM, k and prompts constant across arms.
2. **Questions (about 60 to 100):** Mix of
   - (a) single-fact lookups,
   - (b) questions whose chunk contains pronouns or ellipsis (context-dependent),
   - (c) whole-meeting questions (decisions, who owns what),
   - (d) cross-meeting questions.
   Write or hand-check them yourself, and record the gold evidence span (turn range) and gold document. Draft candidates with an LLM, but have the operator or a trusted reviewer verify. Questions must not be answerable without the corpus.
3. **Metrics:**
   - Evidence recall@k at turn-span level: does any retrieved chunk overlap the gold span? Use k = 5, 10, 20, and measure before reranking and after.
   - Document-level recall@k as a secondary figure.
   - Answer correctness by a fixed rubric or LLM judge, with a manual check on a 20-question sample.
   - Citation attribution rate.
   - Indexing cost: LLM calls, wall time, index size.
4. **Arms:**
   - C0: current fixed 1200/100.
   - A: fixed about 300 tokens, turn-aligned (isolates size).
   - B: A plus context header (rank 1).
   - C: topic segments plus header (rank 2).
   - D: B plus summary units (rank 3).
   - Optional control: LightRAG semantic_vector.
5. **Retrieval modes:** Evaluate naive (vector only) mode separately from the graph modes, so chunking effects are not confounded with extraction effects. Then evaluate graph modes for the arms that win.
6. **Statistics:** With 60 to 100 questions, differences under about 8 points are noise. Report per-category results and paired comparisons, not just averages.

## 8. Open uncertainties

- No study found on chunking strategy for noisy multi-speaker meeting transcripts in RAG. All transcript advice is extrapolation.
- Whether small local LLMs (not Claude Haiku-class) produce good enough context headers and boundaries. Merola and Singh used Phi-3.5-mini, which is mildly encouraging, but I did not see the numbers.
- LightRAG 1.5.7 internals: how `custom` chunkers return data, whether chunk text and the text embedded can differ, and whether any parent-expansion exists. Not verified. Read the source or ask the worker to.
- Whether context headers help or hurt graph extraction (no direct evidence). Possible harm: headers get extracted as entities. Mitigate by testing on a few documents.
- Effect of the reranker (jina-reranker-v3) in reducing chunk-quality differences. Anthropic's 67% figure includes reranking, but the baseline-vs-reranked split for your corpus is unknown.
- Numbers I could not confirm from primary sources: Jina's BEIR gain, Chroma's IoU table, RAPTOR's baseline, SeCom scores, GraphRAG's 600 vs 2400 figure, HippoRAG 2 table.
- Recent items found only as titles or abstracts (not read in full): arXiv 2602.16974, 2601.02128, 2512.17083, 2607.24767, 2608.22872. Treat their findings as leads.

## References

- Günther et al., Late Chunking, Jina AI, 2024. https://arxiv.org/abs/2409.04701
- Anthropic, Introducing Contextual Retrieval, 2024. https://www.anthropic.com/news/contextual-retrieval
- Merola and Singh, Reconstructing Context, 2025. https://arxiv.org/abs/2504.19754
- Qu, Tu, Bao, Is Semantic Chunking Worth the Computational Cost?, Findings of NAACL 2025. https://arxiv.org/abs/2410.13070
- Chroma Research, Evaluating Chunking, 2024. https://research.trychroma.com/evaluating-chunking
- Zhao et al. (IAAR-Shanghai), MoC, 2025. https://arxiv.org/abs/2503.09600 ; Meta-Chunking, https://arxiv.org/abs/2410.12788
- Chen et al., Dense X Retrieval, EMNLP 2024. https://arxiv.org/abs/2312.06648
- Sarthi et al., RAPTOR, ICLR 2024. https://arxiv.org/abs/2401.18059
- Pan et al., SeCom, ICLR 2025. https://arxiv.org/abs/2502.05589
- Conti et al., ConTEB / InSeNT, EMNLP 2025. https://arxiv.org/abs/2505.24782
- Beyond Chunk-Then-Embed (chunking taxonomy), 2026. https://arxiv.org/abs/2602.16974
- Edge et al., From Local to Global: GraphRAG, Microsoft, 2024. https://arxiv.org/abs/2404.16130
- LazyGraphRAG, Microsoft Research blog. https://www.microsoft.com/en-us/research/blog/lazygraphrag/
- Gutierrez et al., HippoRAG 2 (From RAG to Memory), 2025. https://arxiv.org/abs/2502.14802
- Multi-level transcript segmentation (LoRA, ToC), 2026. https://arxiv.org/abs/2601.02128
- When F1 Fails: dialogue topic segmentation evaluation, 2025. https://arxiv.org/abs/2512.17083
- Format-constraint coupling in KG construction (LightRAG chunk size on tables), 2026. https://arxiv.org/abs/2605.21974
- Meeting recap system, 2023. https://arxiv.org/abs/2307.15793
