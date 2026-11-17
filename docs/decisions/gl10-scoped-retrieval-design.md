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

## What would change this ranking

Answers to the stack questions in `docs/decisions/gl10-company-from-question-map.md`:

- If LightRAG 1.5.7 `/query` accepts a document-id or path filter, the filter could move upstream and
  design (b)'s second model call disappears — the guarantee stays, the cost drops. That is the single
  most valuable probe.
- If it supports per-company workspaces with a shared-file exception, (d) becomes worth pricing
  against real numbers rather than the synthetic ones above.
- If `only_need_context` returns per-chunk text with document ids, the filtering step could run
  without `include_chunk_content`, which is the cheaper payload.

None of those is assumed by the shipped code.
