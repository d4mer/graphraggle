# Gateway Quality Review — why OpenWebUI underperforms the LightRAG WebUI

Scope: `app/api.py`, `app/graphrag.py`, `app/graph_native.py`, `app/graph_synthesis.py`,
`app/generation.py`, `app/rerank.py`, `app/lightrag_client.py`, `eval/`.
Branch reviewed: `packet-17-true-graphrag-reset` @ `0c23096`.

---

> **Corrections (post-review).** Four claims in the original draft were overstated or
> wrong and have been fixed inline: (1) A2 and B2 presented the graph-synthesis
> replacement and the `bypass`-mode pseudo-graph expansion as always running. Both are
> gated by flags that default to `false` (`GRAPH_NATIVE_ENABLED`, `GRAPH_EXPANSION_ENABLED`)
> and the live `.env` was never inspected — they are defects *if enabled*. (2) A3 said
> `hybrid` gives summaries only; KG modes also return entity-linked chunks. (3) The
> proposed token-budget pins equal LightRAG's defaults (no-op). (4) The advice to disable
> LightRAG's internal rerank is probably backwards. The flag-independent defects — A1,
> A3 (mode default), A4, A5, A6, A7, B1 — stand as written.

## 0. Verdict

The gap is **not** a model or index problem. The LightRAG WebUI calls
`POST /query` on `lightrag-server` and renders what comes back. The gateway calls the
same endpoint and then applies six transformations, **four of which discard or replace
LightRAG's own grounded answer**. OpenWebUI additionally enters through a bridge that
retrieves a third of the context the rest of the API uses.

Ranked by expected impact on the OpenWebUI path:

| # | Defect | File | Impact |
|---|--------|------|--------|
| A1 | Bridge hardcodes `top_k=4` | `api.py:190,144` | **Critical** |
| A2 | Graph synthesis replaces the answer with a ≤60-word summary of node *counts* (**only if `GRAPH_NATIVE_ENABLED`; live value unverified**) | `api.py:1012`, `graph_synthesis.py` | **Critical** |
| A3 | Default retrieval mode is `hybrid`, not `mix` | `api.py:700` | High |
| A4 | Weak-signal fallback overwrites the answer with `naive` mode | `api.py:855` | High |
| A5 | Conversation history dropped | `api.py:126` | High |
| A6 | Citations never reach OpenWebUI | `api.py:144` | Medium |
| A7 | OpenWebUI title/tag calls run the full pipeline | `api.py:190` | Medium |
| A8 | Streaming is faked; one chunk after full compute | `api.py:190` | Medium |

---

## A. The OpenWebUI gap

### A1 — `top_k=4` on the bridge

```python
# api.py:144
async def answer_ollama_bridge_prompt(prompt: str, top_k: int = 4) -> str:
# api.py:190 (maybe_handle_ollama_bridge)
answer = await answer_ollama_bridge_prompt(prompt, top_k=4)
```

`QueryRequest.top_k` defaults to **12**; the LightRAG WebUI defaults far higher (40–60 in
1.4.x). Every OpenWebUI query therefore runs on roughly a tenth of the evidence the UI
uses. This is the single highest-leverage line in the repo.

```python
# fix
async def answer_ollama_bridge_prompt(prompt: str, top_k: int | None = None) -> str:
    req = QueryRequest(query=prompt, mode="mix", top_k=top_k or settings.bridge_top_k)
```

with `bridge_top_k: int = Field(default=40, alias="BRIDGE_TOP_K")`.

### A2 — Graph synthesis throws away the real answer

```python
# api.py ~1012
if graph_native_applied and combined_evidence:
    synthesized_answer, graph_synthesis_error = await synthesize_graph_aware_answer(...)
    if is_graph_synthesis_answer_usable(synthesized_answer):
        answer = synthesized_answer          # <-- LightRAG's answer is discarded
```

`build_graph_aware_prompt` then constructs the replacement from:

- `MAX_SYNTHESIS_EVIDENCE_ITEMS = 3` evidence items
- `MAX_VECTOR_CONTENT_CHARS = 140` characters of vector content each
- graph evidence rendered as **`seed=X nodes=12 edges=8 labels=[a,b,c,d]`**
- the instruction *"Keep the answer under 60 words"*

`normalize_graph_result` fetches the full `/graphs` payload and stores it under `raw` —
then the prompt uses only the counts. **The relationship content is retrieved and
thrown away.** A relationship question cannot be answered from `nodes=12 edges=8`.

If the flag is enabled, route breadth makes this the common case rather than the edge case; if it is left at its default (`false`) this path never runs. `classify_graph_route`
matches `GRAPH_QUERY_KEYWORDS` = process, shipment, code orange, ecommit, firm horizon,
dashboard, exception, logistics, cmo, consolidation, transport, freight, **policy** —
most real queries in this corpus.

Two fixes, in order of preference:

1. **Serialise real edges into the prompt.** Walk `raw["edges"]`, emit
   `source --relation--> target : description` lines, drop the count line, remove the
   60-word cap, raise `MAX_VECTOR_CONTENT_CHARS` to ~1200.
2. **Never replace — only append.** Keep LightRAG's answer as the spine and attach a
   "Related entities" section. Replacement should require an explicit judge comparison,
   not a `is_usable()` non-empty check.

### A3 — `hybrid` instead of `mix`

```python
retrieval_mode = req.mode or "hybrid"
```

In LightRAG, `hybrid` = local + global KG retrieval (context weighted toward entity and
relationship descriptions, plus the chunks linked to them). `mix` = KG **plus** directly
vector-retrieved chunks, and is what the WebUI uses by default. `"mix"` is already in the `QueryRequest` literal
but is never the default. Change the default to `mix` and make it configurable.

### A4 — the `naive` fallback overwrites a good answer

```python
if first_weak_reason and retrieval_mode != "bypass":
    fallback_used = True
    retrieval_mode = "naive"
    answer, all_citations_fallback, _, _, _ = await execute_query_pass(retrieval_mode, req.query)
```

`first_weak_reason` fires when `len(citations_raw) < 2` **or** the answer contains
"not enough information". A precise, correct answer citing one document trips this and
is replaced by `naive` mode — pure vector, no graph, the weakest mode available. There
is no comparison; the replacement is unconditional.

Make it additive: keep both answers, merge citations, and only prefer the fallback when
the primary answer is *empty*, not merely terse.

### A5 — conversation history dropped

`extract_ollama_prompt` returns the last user message only. LightRAG 1.4.x `/query`
accepts `conversation_history` and `history_turns`; neither is passed anywhere in the
codebase. In OpenWebUI, follow-ups ("and the second one?") retrieve on the fragment
alone. Thread the last N turns from `payload["messages"]` into the LightRAG request.

### A6 — citations discarded on the bridge

`answer_ollama_bridge_prompt` returns `data["answer"]` and drops `citations`,
`graph_evidence`, and `query_scope`. The LightRAG UI shows sources; OpenWebUI shows a
bare paragraph. Append a compact `**Sources**` markdown block to the returned content —
OpenWebUI renders markdown, and this restores most of the perceived-quality gap on its
own.

### A7 — OpenWebUI's auxiliary calls run the full pipeline

OpenWebUI fires title-generation, tag-generation, and follow-up-suggestion requests at
the same model. Each currently runs multi-query rewrites, entity extraction, graph
fetch, and synthesis — a dozen-odd LLM round trips to name a chat. Detect the task
prompts (they contain `### Task:`) and answer them with a single `bypass` call.

### A8 — faked streaming

The bridge computes the full answer, then emits one NDJSON chunk. With serial LLM calls
this is a long silence and a timeout risk in front-ends and proxies. Either stream
LightRAG's own stream through, or emit periodic keepalive chunks.

---

## B. Correctness bugs

**B1 — literal `\n` in generation prompts.** `app/generation.py` lines 30, 32, 33, 35,
36, 37 use `"\\n\\n"`, which is backslash-n in source, not a newline. Every
`/generate-document` prompt is one unbroken line with visible escape sequences. Replace
with `"\n\n"`.

**B2 — `expand_graph_neighbors` does not query the graph.** It sends
*"Find graph neighbors and relationships for: …"* to `/query` in **`bypass`** mode —
which by definition skips retrieval — and parses the LLM's parametric reply as JSON.
`merge_graph_expansion` then injects those into the citation list as `graph://`
pseudo-citations. This is hallucination laundered into evidence **when
`GRAPH_EXPANSION_ENABLED` is true** (default `false`; live value unverified). `graph_native.py`
already does this correctly against `/graph/label/search` and `/graphs`; `graphrag.py`'s
expansion path should be deleted or rewritten against those endpoints.

**B3 — `_add_neighbor` budget logic.** The function returns `True` only when the budget
is exhausted and `None` otherwise, so `if _add_neighbor(...)` reads as a sentinel that
is almost always falsy. Make the return type explicit.

**B4 — config values are advertised then ignored.** `min(settings.graph_expansion_max_neighbors, 3)`,
`filtered[:3]`, `RERANK_TOP_K = 5`, `MAX_SYNTHESIS_EVIDENCE_ITEMS = 3`,
`MAX_VECTOR_CONTENT_CHARS = 140`. Six env vars in `config.py` have no effect on
behaviour. Either honour them or remove them — silently-ignored config is how tuning
sessions get lost.

**B5 — client vocabulary hardcoded in source.** `GRAPH_QUERY_KEYWORDS` (api.py:57) and
`operational_terms` in `filter_graph_neighbors_for_query` (graphrag.py) contain
corpus-specific terms — *code orange, ecommit, firm horizon, cmo*. This is overfitting
to the current eval set: it silently mis-routes every other client's documents, and it
means the eval numbers do not generalise. Move to env, or replace the keyword router
with an embedding-similarity router over the graph's own entity labels.

**B6 — `has_weak_answer_signal` defined twice.** Imported from `multi_query` at
api.py:23, then redefined at api.py:408. The local definition wins. Two
implementations, unclear intent.

**B7 — rerank scores are never attached.** `rerank_citations` reorders but does not
write `relevance_score` back onto the citation dicts, so `get_top_retrieval_confidence`
always returns `0.0` and the `rerank_top_score` branch of `compute_adaptive_hops` is
dead code.

---

## C. Robustness

- **C1 — no connection pooling.** `LightRAGClient` opens a new `httpx.AsyncClient` per
  call. A single graph-routed query opens 8–15 connections. Hold one module-level
  client with `httpx.Limits`, closed on lifespan shutdown.
- **C2 — one timeout for everything.** `request_timeout_seconds` (300) applies to a
  bypass rewrite and a full retrieval alike. A hung auxiliary call stalls the user's
  query for five minutes. Split into per-call-class budgets.
- **C3 — no retry or circuit breaker.** `raise_for_status()` turns a transient LightRAG
  blip into a 500 at OpenWebUI.
- **C4 — fail-open into a void.** The broad `except Exception` handlers are the right
  instinct, but they record the failure in `query_scope`, which the bridge discards.
  Degradation is therefore invisible on exactly the path being complained about.
- **C5 — no pipeline logging.** `query_scope` is rich and never written anywhere. Log it
  as one structured line per query (mode, top_k, fallback, graph route, synthesis
  applied, counts, latencies). Without it there is no way to diff a bad OpenWebUI answer
  against a good UI answer after the fact.
- **C6 — sequential awaits.** Multi-query rewrite retrievals, entity extraction, and
  per-seed `/graphs` fetches are all serial `for` loops. `asyncio.gather` with a
  semaphore would cut p95 substantially.
- **C7 — `@app.on_event("startup")`** is deprecated; move to a lifespan context manager.

---

## D. Eval design

The harness is well-structured, but it cannot detect the regression being reported.

- **D1 — `/api/chat` is never evaluated.** The eval drives `/query` only. The complained-
  about path has no coverage. Add a bridge arm that runs the same query set through the
  Ollama bridge.
- **D2 — no LightRAG-direct arm.** The A/B compares gateway-baseline against
  gateway-candidate. "The gateway is worse than raw LightRAG" is structurally invisible.
  Add a third arm calling `lightrag-server:/query` directly with WebUI-equivalent
  parameters (`mode=mix`, matching `top_k`). That arm is the ceiling; the gateway should
  be measured as a percentage of it.
- **D3 — the gate is inside the noise floor.** Three binary fields averaged over a small
  query set, with an 8% lift threshold and no confidence interval or repeat-run variance.
  At temperature > 0 the run-to-run spread likely exceeds 8%. Report n, per-category
  breakdown, and a bootstrap CI; run each config ≥3 times.
- **D4 — judge self-preference.** If the judge is the same `qwen3.6-35b` that generates,
  it will favour its own outputs. Use a different model, or a cheap deterministic metric.
- **D5 — `expected_topics` is unused.** The dataset already carries the ground truth for
  a stable recall metric (fraction of expected topics present in answer + citations).
  That is far less noisy than an LLM judge and costs nothing. Add it as the primary
  gate and keep the rubric as a secondary signal.

---

## E. Suggested sequencing

**Packet 18 — close the bridge gap (hours, no architecture change)**

1. A1 `top_k` 4 → 40, configurable
2. A3 default mode `hybrid` → `mix`
3. A2 disable answer replacement behind `GRAPH_SYNTHESIS_REPLACE_ANSWER=false`
4. A4 fallback becomes additive
5. A6 append a Sources block to the bridge response
6. B1 fix the `\n` escaping

Acceptance: same 10 queries through the LightRAG WebUI and through OpenWebUI; answers
carry comparable citation counts and no answer is shorter than 60 words unless the query
warrants it.

**Packet 19 — make graph evidence real**

1. B2 delete or rewrite `expand_graph_neighbors` against real graph endpoints
2. A2 serialise actual edges into the synthesis prompt
3. B4 honour the config values
4. B5 move client vocabulary out of source

**Packet 20 — observability and eval integrity**

1. C5 structured per-query logging
2. D1/D2 bridge arm + LightRAG-direct arm
3. D5 `expected_topics` recall as the primary gate
4. C1/C2/C6 pooling, timeout classes, `asyncio.gather`

---

*Housekeeping: `test.txt` at the repo root is a stray artifact from a permissions probe
during this review — safe to delete. `.DS_Store` files are tracked in several
directories and should be gitignored.*
