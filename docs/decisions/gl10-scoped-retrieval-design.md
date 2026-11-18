# GL-10 part 3 — the four company-scoping designs, ranked on measured evidence

Decided 2026-11-17 on `worker/gl10-company-from-question`. The brief listed four candidate designs
and asked for a ranking with measured offline evidence, not plausibility. Two of the four were
measurable here; the numbers are from `scripts/gl10_design_measure.py` over the output of
`scripts/make_synthetic_company.py` (invented documents, seed 7, two companies — Northwind
Chemicals and Contoso Pharma), reported as counts only. Nothing in this file comes from
`source_docs/`, `lightrag_store/`, `state/` or any probe set.

## What was measured

```
companies: 2                                 documents_total: 28
documents_identical_bytes_in_more_than_one_company: 1
documents_identical_bytes_within_a_company:        2
basenames_shared_across_companies:                 3
basenames_ambiguous_within_a_company:              0
text_pieces_total:                                22
text_pieces_carrying_own_company_name:             4   (18.2%)
text_pieces_carrying_another_companys_name:        0   (0.0%)
median_piece_chars:                             1783
pieces_fitting_scoped_context_cap (12000 chars):   9
```

At the shipped chunker window each generated text document came out as one piece, so the piece
percentages below read as document percentages. Two facts matter:

1. **A chunk almost never carries its own company in its own text: 4 of 22 (18.2%).** The structure
   chunker's header is `[Doc: <title> | Date | Section | Part k/n]` (`app/structure_chunker.py:794`)
   — the document title, not the company. The pieces that did carry a company name did so only
   because the generated filename happened to contain it.
2. **No piece mentions the other company (0 of 22).** The synthetic corpus is company-clean; there
   is no in-text cross-company confusion for a retrieval bias to fix.

## Ranking

**1 — (b) retrieve, filter the references through the state join, generate from what is left.
Built.** It is the only design whose guarantee does not depend on upstream behaviour this repo has
not exercised. Every capability it needs is already shipped and measured here: `include_chunk_content`
in the gateway payload (`app/api.py:1377-1378`), `references[].content` as a list of chunk strings
(confirmed on stack2 2026-11-15, read by `app/citation_text.py`), and retrieve→generate with
`mode: bypass` (`app/generation.py:16-45`). The guarantee is then direct and testable: the prompt of
the second call is built from kept references only, so a test can assert that another company's chunk
text is absent from it — which `tests/test_scoped_pass.py` does. Cost: one extra model call, and on
the bridge no token stream. Bounded context: 12000 chars held ~9 median pieces, so the cap is a real
limit on a large cross-company question, not a formality.

**2 — (c) larger pool, post-filter, regenerate.** Same guarantee as (b) for whatever the pool
returns, at strictly higher cost: more retrieval tokens, more rerank work, and it still regenerates.
Its only advantage is recall when the named company's documents rank below the pool cut — which the
2026-11-12 baseline says is not the observed failure (unscoped queries returned *other* companies'
documents, not too few of the asked one's). Folded into (b) as a knob instead of a separate design:
`top_k`/`chunk_top_k` and `COMPANY_SCOPED_CONTEXT_MAX_CHARS` are all settable per run, so a stack
run can raise the pool without a second code path.

**3 — (a) company in the chunk header so the embedding carries it.** The measurement above says what
it would buy: today only 18.2% of pieces carry their company, so an embedding-level bias is nearly
absent, and adding it needs a full re-ingest of every document. It is also soft by construction — a
bias cannot exclude anything — and on a corpus where 0 pieces mention another company, there is
nothing in the text for the bias to separate. It changes retrieval *probability*, not the answer's
boundary, so it cannot be the mechanism that satisfies "the answer must come only from that
company's documents". It may still be worth doing later as a recall improvement for scoped questions;
it must not be presented as scoping.

**4 — (d) per-company LightRAG workspaces.** Strongest isolation, and the measurement prices it: the
corpus already contains documents that are identical bytes under two companies (1 of 28) and 3
basenames shared across companies, and `COMPANY_AWARE_DEDUPE` is on in production precisely because
one company's copy of a shared file must not be silently lost. Per-company workspaces index a shared
file once per workspace, so a shared document either costs duplicate storage and duplicate
extraction, or it is invisible to one of its owners. Cross-company questions then need one query per
company and a merge the gateway would have to invent. It also breaks the measured baseline numbers,
which come from one shared index. Not built.

## How (b) keeps the answer's style and citations

The regeneration prompt (`app/scoped_pass.py:scoped_generation_prompt`) asks for an answer from the
listed material only, forbids naming a document that is not listed, and requires an explicit
"could not find it" when the material does not answer — the refusal behaviour the GL-08/GM1 work
measured as missing (8 of 12 unanswerable probes hallucinated). Citations do not come from the
generation call at all: they come from the kept references, which go through the same
`scope_query_citations` → rerank → truncate path as today, so the Sources block and its company
suffix are unchanged. LightRAG's own answer prompt is not reused; that is the honest cost of the
guarantee, and it is why `COMPANY_SCOPE_ENFORCEMENT` defaults to `citations` and the operator decides.

## Cost correction: the retrieval leg must not pay for an answer (2026-11-17)

The first implementation retrieved the scoped material through `POST /query` with
`include_chunk_content`. That was a retrieval **and a generation**: LightRAG wrote a complete answer
that was thrown away, so a scoped question paid for two model calls while an unscoped one paid for
one.

LightRAG 1.5.7 has a retrieval-only endpoint, `POST /query/data` (same auth and body model), returning
`status`, `message`, `data{entities, relationships, chunks, references}` and `metadata` with **no
generation**; `include_chunk_content` puts chunk text into the returned items. Reading its source also
confirmed the negative: there is no per-request document, file-path or workspace filter in `/query`, so
post-filtering remains the only route. Only where the material comes from changes, not how it is
scoped. A scoped question is now one retrieval plus one generation - the same cost as an unscoped one.

The parsing is deliberately not a bet on an unobserved shape. Which list carries the chunk text
(`references`, or `chunks`) and under which key has not been seen on this stack, so both are read;
chunk items are grouped by document path, because the scoping decision is per document rather than per
chunk; the accepted path keys are exactly the four `app/api.py:extract_citation_path`
already tolerates (`file_source`, `path`, `source`, `file_path`) — no fifth name is invented, because a
wrong guess there would surface as a wrong owner rather than an unknown shape — and the text is read
through the shared citation accessor `app/citation_text.py`. Every run logs a census: references found, chunks found, items carrying text,
text-carrying items with no path, chunk-only paths, which content keys appeared. A chunk is never
invented a path - an item with text and no path stays unattributable and is counted as such. If
`/query/data` fails or returns nothing usable the pass falls back to the `/query` retrieval it used
before and records `retrieval_route` plus `retrieval_fallback_reason`, with `model_calls: 2` marking
the discarded generation that route forces.

`COMPANY_KEYWORD_HINT` (off by default) additionally names the scoped company in the
`hl_keywords`/`ll_keywords` of the retrieval payload - the same keys the bridge already sends. That is
a bias toward the right chunks in a bounded pool, never a boundary: the exclusion comes entirely from
the state-store join, and a test asserts the hint does not stand in for the filter.

## What would change this ranking

Answers to the stack questions in `docs/decisions/gl10-company-from-question-map.md`:

- ~~If LightRAG 1.5.7 `/query` accepts a document-id or path filter…~~ Answered: it does not, read
  from the 1.5.7 source. What it does have is the retrieval-only `/query/data`, now used for the
  retrieval leg, so the wasted generation is gone. A native path filter would still be worth having -
  it would let the named company's material be retrieved rather than filtered after the fact, which
  matters for recall on a large store - but it no longer buys a model call.
- If it supports per-company workspaces with a shared-file exception, (d) becomes worth pricing
  against real numbers rather than the synthetic ones above.
- If `only_need_context` returns per-chunk text with document ids, the filtering step could run
  without `include_chunk_content`, which is the cheaper payload.

None of those is assumed by the shipped code.

## Refusal discipline is ours to write (2026-11-18)

R2 on cutover-7 refused 0/12 unanswerable questions (R0 1/12, R1 3/12) while leaking nothing. The scoped
pass generates with `mode: bypass`, which sends the LLM only the text we hand it: LightRAG's retrieval
prompt - the thing that carries its "if you do not have enough information, say so" instruction and its
reference discipline - never runs. Choosing (b) bought the boundary and quietly removed the instruction
that makes a RAG system admit it has nothing. So the boundary and the honesty are now both our
responsibility in one prompt: bound the evidence, ask for the coverage check before the answer, require
attribution to a Source line, forbid inventing identifiers, and name the refusal sentence. That sentence
is one constant, and it contains the phrase `app/api.py:WEAK_ANSWER_MARKERS` already detects, so a
refusal is a countable event instead of a phrasing.

The deterministic half (`COMPANY_SCOPED_HARD_REFUSAL`, off) is kept separate and off because it is a
different kind of claim: the gateway deciding a question is unanswerable from the identifier it asked
about. The matcher is the shipped grounding classifier, whose false-refusal rate was measured only on
synthetic probes. Measured on a stack run before it is trusted on answers; when it fires and the
identifier was lost to the context cap, the reason string says so, because that is a budget decision
rather than a fact about the corpus.

Two consequences worth recording for anyone who changes this again: the cap is a claim about which
evidence the model may see, so the kept set is a prefix of the retrieval order and only the top block may
be cut (marked); and `data.references[]` and `data.chunks[]` are never merged into one ordering, because
they are two rankings and only one of them can be trusted when the other carries no text.

## The scoped pass is inside a conversation (2026-11-18)

Retrieval was given the conversation and generation was not, so a follow-up resolved for retrieval and not
for the model. Two decisions, one of them a safety call rather than a convenience one:

- The generation payload sends the same `conversation_history` the normal path sends, and the prompt also
  carries a bounded block of the **earlier user turns**, because whether `mode: bypass` honours that field
  upstream is unverified here. Sending the field is what upstream would honour; embedding the questions is
  what makes the instruction work either way.
- **Assistant turns are never embedded.** An earlier answer may have been written before any company
  boundary existed and may quote another company's document; pasting it into a scoped generation puts the
  excluded text back in front of the model. Earlier questions carry the referent without carrying facts.
  The residual is stated, not hidden: if bypass does honour `conversation_history`, upstream may still feed
  assistant turns back, and that is a source question for the stack, not something this repo can decide.

Failure is a degradation, not an error: a rejected or timed-out scoped generation makes the pass
`used=False` with `generation_failed_<status>`, hands over no text and no citations, lets the caller serve
its citation-filtered path, and if that fails too the request is served with the honest decline. The
decline sets no weak-signal reason, so the evidence-adding stages cannot re-open an unscoped search — that
would trade a 500 for a leak, which is the one swap this feature refuses to make.

## Retrieval must over-fetch, because the filter is the point (2026-11-18)

The guarantee is a subtraction: retrieve over the whole index, then drop everything that is not the named
company's. Cutover-9 measured the price of doing that at the ordinary request size — 1056 references, 614
kept, and the answer chunk for the company that was asked about frequently sitting in the discarded 442.
Once the prompt was honest enough to refuse what it cannot support, that loss stopped looking like a
hallucination and started looking like a refusal: `refused_wrong` 4 → 21 on questions the corpus answers.

The decision, and its three bounds:

- **Ask wider than we can keep** (`COMPANY_SCOPED_RETRIEVAL_MULTIPLIER`, ceiling
  `COMPANY_SCOPED_RETRIEVAL_CEILING`), and when fewer than `COMPANY_SCOPED_RETRIEVAL_FLOOR` blocks reached
  the prompt, retrieve once more at double the multiplier. Default multiplier 1: the lever is off until a
  stack run measures it, and it stays one retrieval and one generation per row while off.
- **Widening may not buy a generation.** A second retrieval is allowed only when the first cost none, and the
  wider round itself never uses the answering endpoint. A row may be retrieved twice; it may not be answered
  twice for a reason the caller never agreed to.
- **Widening may not move the evidence boundary.** The wider round wins only if strictly more blocks reach
  the prompt; on a tie the first round stands. Only `top_k`/`chunk_top_k` are rewritten — rerank and token
  budgets come from the door unchanged, so the pool is bigger and better ordered, never a different kind of
  retrieval. The context cap is untouched and remains the only limit on what the model is shown.

What the run has to decide that this repo cannot: whether `/query/data`'s returned reference payload is
itself bounded by `max_total_tokens`. If it is, the multiplier enlarges the pool without enlarging the text
we can read, and the tell is `scoped_pass_references_returned` refusing to move while
`scoped_pass_retrieval_top_k_requested` climbs. That is why the requested numbers are logged per round:
without them, "we asked for 20 chunks out of a five-company index" and "this company has no such document"
are the same row.
