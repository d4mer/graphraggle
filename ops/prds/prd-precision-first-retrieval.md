# PRD — Precision-First Retrieval

Status: proposed
Supersedes the query-time heuristics introduced in Packets 13–17.
Target failure modes: **vague/generic answers** and **facts present in the corpus that never surface**.

---

## Problem Statement

The system is starved of evidence at every stage of the pipeline, and it answers from
summaries rather than from sources.

**The funnel, as built today:**

| Stage | Limit | Where |
|---|---|---|
| Bridge retrieval | `top_k=4` | `api.py:190` |
| Gateway truncation | 5 citations | `RERANK_TOP_K` |
| Synthesis evidence | 3 items | `MAX_SYNTHESIS_EVIDENCE_ITEMS` |
| Vector content per item | 140 characters | `MAX_VECTOR_CONTENT_CHARS` |
| Graph content | node/edge **counts** + 4 labels | `build_graph_aware_prompt` |
| Answer | "under 60 words" | `build_graph_aware_prompt` |

A corpus of thousands of chunks is reduced to roughly 420 characters of text plus a node
count before the answer is written. Every knob in the system is set to narrow. The
observed symptoms — generic answers, missing facts — are the direct and predictable
consequence.

Underneath that, three structural causes:

**1. The graph is built with the wrong schema.** No index-quality variable is set
anywhere in the repo or the installer, so LightRAG runs its defaults:
`ENTITY_TYPES = organization, person, geo, event, category`. The corpus is SAP/OMP
implementation material — *firm horizon, eCommit, Code Orange interface, planned goods
receipt date, CMO scope, S4 transition*. These are not people, places or organisations.
They are force-fit into `category` or dropped entirely. Facts whose entities never enter
the graph are unreachable by every KG retrieval mode, which is exactly the "misses facts
that are in the corpus" symptom.

**2. Answers are synthesised from descriptions, not sources.** `hybrid` mode — the
gateway default — returns entity and relationship *descriptions*, which are LLM-written
summaries produced at index time. The pipeline therefore summarises a summary. That is
the mechanism behind "vague and generic". `mix` mode, which adds the raw chunks back, is
in the `QueryRequest` enum and is never the default.

**3. Documents are ingested whole, with no chunking strategy.** `worker.py` submits
entire files and polls the track. Workshop transcripts and multi-hundred-page specs are
both chunked at LightRAG's flat 1200-token default, with no speaker, topic or section
awareness, and no contextual header. A chunk that straddles a topic boundary embeds as
neither topic and is retrieved for neither.

---

## Solution

**Graph as router, chunks as evidence.**

Use the knowledge graph for *navigation* — deciding which source material is relevant —
and raw chunk text for *content*. The graph answers "where do I look"; the chunks answer
"what does it say". Today the graph is asked to do both, and it only carries summaries.

Concretely, the target retrieval path:

1. Resolve query terms to **real graph entity labels** (`/graph/label/search`)
2. Traverse the neighbourhood of confirmed seeds (`/graphs`)
3. Follow `source_id` provenance on nodes and edges back to **the exact chunks that
   produced those relationships**
4. Fetch those chunks in full
5. Union with standard vector/`mix` retrieval
6. Rerank the union once, with the full candidate set
7. Synthesise from full chunk text plus an explicit edge list, with no word cap

Steps 1–3 already exist in `graph_native.py` and are correct. The payload they fetch is
currently reduced to `nodes=12 edges=8` before it reaches the prompt. The plan is
largely to stop discarding work the system already does.

Given that cross-company questions are a real use case, the index stays unified. Company
scoping moves from a post-retrieval citation filter to a retrieval-time constraint, so a
scoped query spends its whole `top_k` budget on the right corpus instead of retrieving
across all clients and discarding most of the result.

---

## User Stories

1. As an operator, I want answers that quote specifics from the source documents, so that
   I can act on them without opening the document myself.
2. As an operator, I want a fact that exists in an ingested document to surface reliably,
   so that I can trust a "not found" answer.
3. As an operator, I want every answer to carry its sources in OpenWebUI, so that I can
   verify without switching to the LightRAG WebUI.
4. As an operator, I want multi-entity questions to be answered across all named
   entities, not just the one the retriever happened to latch onto.
5. As an operator, I want cross-company questions to work, while a company-scoped
   question spends its full retrieval budget on that company.
6. As an operator, I want follow-up questions in OpenWebUI to understand what I just
   asked.
7. As a maintainer, I want a candidate index I can build and query alongside the live
   one, so that schema changes are proven before a full re-index.
8. As a maintainer, I want every retrieval decision logged, so that a bad answer can be
   diagnosed after the fact rather than reproduced by hand.
9. As a maintainer, I want the domain vocabulary in configuration, not in source, so that
   a new client does not require a code change.
10. As a maintainer, I want one reranking layer, not two.

---

## Implementation Decisions

### Stage 0 — Restore fidelity (no re-index)

Undo the starvation. These are parameter and control-flow changes only; the index is
untouched.

1. Default retrieval mode becomes `mix`, not `hybrid`. Configurable via
   `RETRIEVAL_MODE_DEFAULT`.
2. Bridge `top_k` 4 → configurable `BRIDGE_TOP_K`, default 40. Add `chunk_top_k`
   (default 16) as a distinct request parameter.
3. Set the LightRAG context budget explicitly — `MAX_ENTITY_TOKENS`,
   `MAX_RELATION_TOKENS`, `MAX_TOTAL_TOKENS` — so that entity and relation context cannot
   crowd chunk text out of the window. Chunks get the majority share.
4. Graph synthesis no longer replaces the answer. Behind
   `GRAPH_SYNTHESIS_REPLACE_ANSWER=false` initially; in Stage 4 it is rewritten rather
   than re-enabled.
5. Remove the word cap and the 140-character truncation from
   `build_graph_aware_prompt`.
6. The weak-signal `naive` fallback becomes additive — citations merge, and the fallback
   answer is used only when the primary answer is empty, not merely short.
7. Delete the `bypass`-mode expansion path in `graphrag.py`
   (`expand_graph_neighbors`, `extract_entities_via_llm`, `merge_graph_expansion`,
   `filter_graph_neighbors_for_query`). Packet 17's PRD called for this; the code was
   added alongside it rather than replacing it. It injects LLM-invented relationships
   into the citation list as `graph://` entries.
8. Resolve the double rerank. `RERANK_BY_DEFAULT=True` is set in LightRAG's environment
   and the gateway reranks again on the truncated five. Keep exactly one layer — prefer
   the gateway's, applied to the full candidate set before truncation, and set
   `RERANK_BY_DEFAULT=False`.
9. Truncation moves after reranking and rises from 5 to a configurable
   `CITATION_TOP_K`, default 12.
10. The bridge returns citations. Append a compact markdown `**Sources**` block to the
    OpenWebUI response body.
11. The bridge threads `conversation_history` and `history_turns` from
    `payload["messages"]` into the LightRAG request.
12. The bridge short-circuits OpenWebUI's auxiliary calls (title, tag and follow-up
    generation — identifiable by their `### Task:` preamble) to a single `bypass` call
    instead of the full pipeline.
13. Fix the literal `\n` escaping in `app/generation.py` (lines 30, 32, 33, 35–37).

### Stage 1 — Instrumentation (prerequisite for everything after)

Nothing downstream can be proven without this, so it lands before Stage 2.

14. Emit one structured log line per query carrying the full `query_scope` plus
    per-stage latencies and candidate counts. Today `query_scope` is assembled richly and
    then discarded by the bridge.
15. Build a **probe set** of 30–50 questions drawn from real usage, each recording the
    document and passage that contains the answer. This is ground truth for recall, not
    an LLM rubric.
16. Build `scripts/probe_compare.py`: runs the probe set against two LightRAG
    workspaces (or two gateway configurations) and reports, per question — was the
    correct source chunk retrieved at all, at what rank, and does the answer contain the
    expected specifics. Deterministic; no judge model.

### Stage 2 — Domain schema and re-extraction (subset-proven)

17. Define `ENTITY_TYPES` for the implementation domain. Proposed starting set:
    `system, interface, process, process_step, planning_horizon, master_data_object,
    data_field, org_unit, role, decision, requirement, open_question, risk, milestone,
    exception, metric`.
18. Author a custom entity-extraction prompt with few-shot examples drawn from the
    actual corpus, so that *firm horizon* extracts as `planning_horizon` and *Code Orange
    Interface* as `interface`.
19. Raise `ENTITY_EXTRACT_MAX_GLEANING` from 1 to 2. Transcript chunks are
    information-dense and a single extraction pass under-recovers.
20. Build the candidate index into a **separate LightRAG `workspace`**, on a subset of
    one company's documents. This is the mechanism for proving the change without
    touching the live index — `workspace` is used here for config isolation, not tenancy.
21. Promote to a full re-index only after the probe set shows retrieval-rank
    improvement on the subset.

### Stage 3 — Document-class chunking

22. Extend `classify_file` in `validation.py` to classify by *content class* —
    transcript, specification, minutes, correspondence, tabular — not just extension.
23. The worker pre-chunks before submission rather than posting whole files:
    - **Transcripts**: segment on speaker turns, group to ~700 tokens with ~200 overlap,
      never splitting mid-turn.
    - **Specifications**: segment on heading hierarchy, preserving section boundaries.
    - **Tabular**: one chunk per logical table with its caption and column headers.
24. Every chunk carries a **contextual header** — document title, section or timestamp,
    participants where known, company — prepended to the chunk text before embedding.
    This is the cheapest available recall win: it makes each chunk self-describing to
    both the embedder and the reranker.
25. Chunk-level metadata is submitted alongside the text so that company and document
    class are available as retrieval-time filters.

### Stage 4 — Entity-anchored retrieval

26. Replace the keyword router (`GRAPH_QUERY_KEYWORDS`, `classify_graph_route`) with an
    empirical one: a query routes to the graph channel when its terms **resolve to actual
    entity labels** in the graph. No hardcoded vocabulary, and it generalises to any
    client's corpus.
27. Move client vocabulary out of source entirely. `operational_terms` and
    `TRANSCRIPT_QUERY_KEYWORDS` become configuration, or disappear with the router.
28. Implement **provenance-to-chunk hydration**: walk `source_id` on traversed nodes and
    edges, resolve to chunk IDs, and fetch those chunks in full. This is the step that
    turns graph traversal into evidence rather than metadata.
29. Fuse hydrated graph chunks with `mix`-mode vector chunks into one candidate pool,
    deduplicated by chunk ID, and rerank the pool as a whole.
30. Company scoping applies as a filter over that pool before reranking, replacing the
    post-hoc citation filter in `scope_query_citations`.

### Stage 5 — Query decomposition

31. Replace paraphrase-style `multi_query` rewrites with **entity decomposition**. Real
    questions name three or four entities — *"how are empty shipments, freight orders,
    and the split between optimization horizon and firm horizon connected?"* — and a
    single retrieval pass systematically under-covers them.
32. Decompose into per-entity retrievals plus a pairwise-relationship pass over the
    named entities, then synthesise once over the union. Bounded by a configurable
    entity ceiling.
33. Decomposition triggers on entity count resolved from the graph, not on word count or
    keyword match.

### Stage 6 — Synthesis

34. The synthesis prompt receives full chunk text and an explicit edge list rendered as
    `source --relation--> target : description`, not counts and labels.
35. No word or length caps. Response length is governed by `response_type`, passed
    through from the request.
36. Require inline citation markers tied to chunk IDs, so that grounding is checkable
    per claim rather than per answer.
37. Add a verification pass: the draft answer is checked claim-by-claim against the
    evidence pool, and unsupported claims are dropped or marked. This is the direct
    counter to vagueness — a verifier can require that each claim name a specific
    entity, field or value.

---

## Sequencing and Expected Effect

| Stage | Re-index | Effort | Addresses |
|---|---|---|---|
| 0 — Restore fidelity | no | hours | Vagueness (primary), recall (partial) |
| 1 — Instrumentation | no | 1 day | Prerequisite for proof |
| 2 — Domain schema | subset | 2–3 days + build | Recall (primary) |
| 3 — Chunking | subset | 2–3 days + build | Recall, precision of retrieved span |
| 4 — Entity-anchored retrieval | no | 3–4 days | Recall (primary), vagueness |
| 5 — Decomposition | no | 2 days | Multi-entity coverage |
| 6 — Synthesis | no | 2 days | Vagueness (primary) |

Stage 0 alone should produce a visible step change, because it stops the pipeline
discarding LightRAG's own grounded answer and restores roughly an order of magnitude more
evidence to the context. It is also the cheapest thing to reverse if it does not.

Stages 2 and 3 are where the durable gains live, and both require the candidate-workspace
mechanism from Stage 1 to be proven on a subset first.

---

## Testing Decisions

1. **Probe-set recall** is the primary metric throughout: for each probe question, was
   the known-correct chunk retrieved, and at what rank. Deterministic, no judge model,
   stable across runs.
2. **Answer specificity** as a secondary deterministic metric: presence of expected
   entities, field names and values in the answer text. The existing
   `expected_topics` field in `eval/datasets/` already carries this and is currently
   unused.
3. Each stage is gated on probe-set recall not regressing, and on the stage's own
   target metric improving.
4. Candidate and live workspaces are queried through the same gateway build, so that
   only the index differs.
5. `/api/chat` is exercised directly, not only `/query` — the bridge is the path in
   daily use and currently has no coverage.
6. A LightRAG-direct arm (`mode=mix`, matching `top_k`) establishes the ceiling. The
   gateway is measured as a percentage of it; the gateway should never score below it.
7. Unit coverage for the new modules: provenance hydration, entity decomposition,
   chunk-class segmentation, contextual header construction.

---

## Out of Scope

- Replacing LightRAG or introducing a second graph database.
- Per-company workspace isolation — ruled out because cross-company questions are a
  required capability.
- Fine-tuning the embedding or rerank models.
- Changing the LLM. `qwen3.6-35b` is not the constraint; the context it receives is.
