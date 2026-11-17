# GL-10 part 1 — what exists today, with file:line

Base: `worker/gl10-company-from-question` off local `master@317c077` (`origin/master@2cd2192` + the
commit carrying this brief). Every line number below was re-read at this base; the earlier stage-1 note
(`worker/gl09-gl10-company-scoping@8f964ae`, `docs/decisions/gl09-gl10-company-scoping-design.md`,
unmerged) describes the same machinery at an older base and is still the design history. No corpus,
store, or LightRAG content was read for this note.

## The company path that exists

| step | where | what it does |
|---|---|---|
| ingest attribution | `app/worker.py:173` `infer_company_from_path` (first directory under `SOURCE_DOCS_DIR`), re-checked per scan at `:184` | the filesystem door knows the company from the folder |
| attribution rule | `app/state_store.py:230` `build_company_attribution` | explicit request > path-inferred > `unscoped`; columns `company`, `company_source`, `company_source_detail` at `:26-28` |
| normalisation | `app/state_store.py:223` `normalize_company` | **strip only** — no case folding, no punctuation handling |
| citation → document | `app/api.py:814` `extract_citation_path` (`file_source\|path\|source\|file_path`) → `app/state_store.py:635` `get_documents_by_paths` (`WHERE path IN (...)`, **exact string**) | join key is the stored `documents.path` |
| citation enrichment | `app/api.py:820` `enrich_citation` | copies `company`, `company_source`, `document_id`, `query_ready` onto the citation |
| the scoping itself | `app/api.py:835` `scope_query_citations` | keeps citations whose joined row's company equals the request's; its own metadata says `enforcement: gateway_citation_filter` (`:864`) and attaches the "not hard isolation" warning (`:871`, echoed at `:1660`) |
| where it runs | `app/api.py:1328` `apply_scope_rerank_truncate`, called at `:1579` | **after** retrieval, rerank input; filter → rerank → truncate to `CITATION_TOP_K` |
| request field | `app/models.py:15` `company: str \| None`; read at `app/api.py:1273` | the only way a query gets scoped today |

## The five things that do not work

1. **Nothing reads a company out of the question.** `_query_core` takes scope solely from
   `req.company` (`app/api.py:1273`); `query_scope["mode"]` is `unscoped` unless a caller sends the
   field (`:1584-1585`). There is no helper for the known-company list either — no
   `SELECT DISTINCT company` anywhere in `app/state_store.py`.
2. **The answer is never scoped.** The answer is LightRAG's `response` from the unfiltered pass
   (`app/api.py:1302-1303`); the filter runs afterwards on the citation list only. The one place the
   gateway does rewrite an answer from evidence is graph synthesis
   `app/api.py:1638-1650`, `GRAPH_SYNTHESIS_REPLACE_ANSWER` default off.
3. **The production path has no company at all.** `BRIDGE_BACKEND` defaults to `lightrag_direct`
   (`app/config.py:247`), so Open WebUI goes through `answer_ollama_bridge_direct`
   (`app/api.py:451`) → `build_ollama_bridge_query_payload` (`:216`, no company key) → LightRAG
   `/query/stream`. Its references are parsed from an already-complete stream
   (`extract_stream_references`, `:299`) and reduced to `[{"file_path": …}]` (`:629`), so
   `citation["company"]` is always absent and `build_sources_block` (`:182`, company suffix at `:200`)
   can never print one. `answer_ollama_bridge_pipeline` (`:655`) is the gateway-pipeline alternative and
   also passes no company (`:658-663`).
4. **Upload-door citations cannot be attributed.** The filesystem door submits
   `file_source: str(path)` (`app/worker.py:1165`), which equals the stored `documents.path`; the
   upload door sends only a basename (`app/lightrag_client.py:25-31`), which can never equal it. So an
   upload-ingested document resolves to `company=None` and is dropped by any scoped query.
5. **A shared file belongs to one company at query time.** `COMPANY_AWARE_DEDUPE` (`app/config.py:43`)
   keeps one row per company per path, and a `skipped` duplicate row still resolves its own company
   (`tests/test_skipped_duplicate_rows.py:294`). But upstream indexes identical bytes **once**
   store-wide (`docs/05-known-good-config.md:225-244`), so the citation that comes back carries the one
   path LightRAG stored — a scoped question for the other company filters it out. Path-only attribution
   cannot fix this; content-hash attribution can `app/state_store.py:624` `get_documents_by_sha256`.

## What sits between retrieval and the filter (relevant to any scoping design)

`first pass (:1363)` → multi-query rewrite merge (`:1388-1424`) → weak-signal naive fallback, additive
(`:1436-1463`) → M1 grounding computed on the **pre-scope** reference list (`:1474-1481`) → graph
expansion, merged **before** the scope filter (`:1490-1560`) → scope + rerank + truncate (`:1579`) →
`query_scope` (`:1583`). So any company filter must be applied at each merge point, not once at the end,
or expanded/fallback evidence slips past it. Also `is_transcript_like_query` raises `top_k` to ≥24
(`:1276-1277`), which matters when sizing a retrieval pool that will later be filtered.

## Unanswered upstream questions still open (from the stage-1 note)

Whether LightRAG 1.5.7 `/query` accepts a document-id/path filter (S1), what `only_need_context`
returns per chunk (S3 of the shape question), and whether it supports per-company workspaces — all
three need Hermes probes on stack2, not offline reading. What IS established in this repo:
`references[].content` is a **list of chunk strings** under `include_chunk_content: true`
(`app/citation_text.py`, confirmed on stack2), and `mode: bypass` retrieve→generate is already shipped
in `app/generation.py` and `app/multi_query.py`.
