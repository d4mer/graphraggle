# Prompt-size investigation — what builds 45k–80k token prompts

Task: `qitem-20261008185225-b0661b3d` (read-only). Repo `/home/maverick/work/graphraggle`,
branch `packet-22-ll-seed` @ `4cd7e09`, 2026-10-08. No app code changed.
Measurement helper (throwaway, wired into nothing): `scripts/prompt_size_estimate.py`,
output in the Appendix.

Method. Token figures are **chars/4** (the brief's instruction); the real tokenizer is
oMLX's and is not available offline. "Gateway-side" = bytes *this repo* puts into the
prompt. Anything LightRAG adds (its own templates, its truncation of retrieved context)
is marked **LIGHTRAG-INTERNAL** and is not guessed at. LightRAG v1.4.15 runs in Docker
(`compose.yml:2`) and is not installed in `.venv`, so its source was not read.

---

## Findings (ranked by likelihood of explaining the 45k–80k population)

### F1 — OpenWebUI auxiliary ("`### Task:`") requests are forwarded to the LLM with an uncapped prompt

- Gate: `app/api.py:437` (`is_openwebui_task_prompt`, `app/api.py:317-319`, prefix constant
  `app/api.py:89`). Request body: `app/api.py:446-455` —
  `{"query": prompt, "mode": "bypass", "stream": True, "include_references": False}`.
  **No `max_total_tokens`, no `max_entity_tokens`, no `max_relation_tokens`, no `top_k`** —
  contrast the normal bridge payload which sends all of them (`app/api.py:218-222`).
- `prompt` is the raw OpenWebUI task prompt, taken verbatim and **length-uncapped** by
  `extract_ollama_prompt` (`app/api.py:162-177`). The repo's own comment states what it
  contains: "A task prompt embeds the chat history OpenWebUI wants summarised"
  `app/api.py:434-435`), and the quality review names the callers: "OpenWebUI fires
  title-generation, tag-generation, and follow-up-suggestion requests at the same model"
  (`docs/ops/gateway-quality-review.md:143-148`, item A7).
- `mode: "bypass"` means LightRAG retrieves nothing, so there is no truncation pass to
  shrink the text — the query text *is* the prompt. (Whether bypass honours
  `max_total_tokens` at all: depends on LightRAG v1.4.15 internals, `operate.py` bypass branch.)
- Fits every observed property: streaming (`app/api.py:452`), small completions
  (title/tag/suggestion — 56–1083 tokens is consistent once the inline chain-of-thought
  qwen3.6-35b emits is counted, `app/config.py:83-85`, strip pairs `app/api.py:106`), size grows with conversation
  length, and the shared prefix is only the task header (see Cache-prefix notes).
- Gateway-side contribution is the whole task prompt: 20k chars ≈ 5k tokens,
  80k chars ≈ 20k tokens (Appendix). Reaching 45k–80k **tokens** needs a ~180k–320k-char
  task prompt — a long multi-turn chat, which is exactly what title/tag generation
  summarises. Whether OpenWebUI truncates its own task prompt is **not determinable from
  this repo** (Unknowns U1); the point is the gateway imposes no cap of its own.

### F2 — `/api/chat` and `/api/generate` forward the raw client body to LightRAG for any other model name

- The bridge only claims requests whose model is exactly `lightrag:latest`
  (`app/api.py:681`, constant `app/api.py:84`). Otherwise the untouched body is proxied to
  LightRAG's Ollama-compat endpoints: `app/api.py:1713` (`/api/generate`) and
  `app/api.py:1728` (`/api/chat`).
- On that path **nothing in this repo caps anything**: the history trimming
  (`app/api.py:354` 2000 chars/message, `app/api.py:358` last `turns*2` messages) and the
  retrieval caps (`app/api.py:218-222`) never run. The full `messages` array goes upstream.
- LightRAG's Ollama layer then builds its own prompt with container defaults — depends on
  LightRAG v1.4.15 internals (its `/api/chat` handler and its `MAX_TOTAL_TOKENS`/`TOP_K` env).
- Trigger condition is a model-name mismatch, so it fires for any OpenWebUI model picker
  value other than `lightrag:latest`, and for any client that posts to those routes directly.

### F3 — the gateway `/query` pipeline sends no token caps at all

- Payload: `app/api.py:1234-1247` — `query`, `mode`, `top_k`, `chunk_top_k`,
  `include_references`, `include_chunk_content`. No `max_*_tokens` keys, unlike the bridge
  (`app/api.py:218-222`). So `BRIDGE_MAX_TOTAL_TOKENS=32000` (`app/config.py:75`) does **not**
  bound this path; the container's LightRAG defaults do (depends on LightRAG v1.4.15
  `QueryRequest.max_total_tokens` / `MAX_TOTAL_TOKENS` env).
- Request multiplier, not prompt multiplier: one gateway `/query` can issue up to five
  LightRAG query passes — first pass `app/api.py:1308`, rewrite passes `app/api.py:1351`
  and `app/api.py:1397`, naive fallback pass `app/api.py:1388-1390` — each with its own full
  prompt. `MULTI_QUERY_ENABLED` defaults off (`app/config.py:29`).
- `top_k` here is 12 by default (`app/models.py:16`) or 24 for transcript-like queries
  (`app/api.py:1223`), i.e. *smaller* than the bridge's 40 (`app/config.py:53`). This path
  only exceeds the bridge if the container's token defaults are larger than 32000.

### F4 — `POST /generate-document` builds an uncapped prompt out of every returned chunk

- Retrieval: `app/generation.py:16-24` — `mode: "hybrid"`, `include_chunk_content: True`,
  **no `top_k`, no `chunk_top_k`, no token caps**.
- Context assembly: `app/generation.py:26-33` concatenates every reference's full chunk
  content with **no cap and no count limit**.
- LLM call: `app/generation.py:40-46` — `mode: "bypass"`, so no truncation pass.
- Illustrative size: 40 references × 1200-token chunks ≈ **48k tokens** (Appendix row 10).
  Reference count and chunk size are LIGHTRAG-INTERNAL.
- Reachability: gateway API only, not OpenWebUI; operators are told to use it
  (`docs/07-openwebui-guide.md:125`, `docs/03-admin-operations.md:186`). It explains a
  population only if something actually calls it.

### F5 — the main bridge path is capped, but only on retrieved context

- Caps sent: `app/api.py:218-222` → `max_entity_tokens=10000`, `max_relation_tokens=10000`,
  `max_total_tokens=32000` (`app/config.py:73-75`), `top_k=40` (`app/config.py:53`),
  `chunk_top_k=20` (`app/config.py:72`). Production logs confirm those values are in force:
  `top_k:40`, `chunk_top_k:20`, `Final context: 55 entities, 223 relations, 11 chunks`
  (`scripts/lightrag_log_stages.py:12-23`).
- Added **outside** that budget: `conversation_history` (`app/api.py:229`) up to
  `bridge_history_turns*2 = 6` messages × 2000 chars = 12k chars ≈ 3k tokens
  (`app/api.py:354,358`; `app/config.py:62`; `MAX_HISTORY_MESSAGE_CHARS` `app/api.py:109`),
  the query itself, and `user_prompt` when a rewrite happened (`app/api.py:505`), plus
  LightRAG's answer template (LIGHTRAG-INTERNAL).
- Measured gateway-side contribution with 3 full turns: 8.4k chars ≈ 2.1k tokens
  (Appendix row 6). So this path explains the observed **~32k** line (32k context + ~2–3k
  history + template), not 45k–80k. Assistant turns are length-capped and prior Sources
  blocks are stripped before re-sending (`app/api.py:352-354`), so the citation block is
  *not* re-sent.

### F6 — ingest-time prompts are entirely LightRAG-decided (cannot be excluded)

- The worker submits a whole document in one call: `app/worker.py:464-469`
  (`POST /documents/text`, full text) or `app/worker.py:472` (`POST /documents/upload`),
  with **no chunking parameters**. Chunk size, the extraction prompt, and any
  entity/relation summary calls are LightRAG v1.4.15 internals. `CHUNK_TOP_K` is LightRAG's
  own variable and the stack shares one `.env` (`app/config.py:55-56`, `compose.yml:5-7`).
- The brief's evidence (prompt size, streaming, 56–1083 completion tokens) does not identify
  the caller, so ingest cannot be ruled out from this repo.

### Not LLM paths (checked, excluded)

- `app/graph_native.py:74-99` — LightRAG `/graph/label/*` GETs only; no LLM call.
- `app/rerank.py:24-30,78-80` — posts to a `/rerank` endpoint, not the chat model.
- `app/graph_fusion.py` — pure list merging, no I/O.

---

## Q2 — per-path table

| # | Path | file:line | What it sends to the LLM | Bound in this repo | How the LLM is reached |
|---|---|---|---|---|---|
| 1 | Bridge direct answer (production OpenWebUI path) | payload `app/api.py:214-235`, POST `app/api.py:549-552` | rewritten query + history + retrieval params | **yes**: `max_total_tokens=32000`, entity/relation 10000 each, `top_k=40`, `chunk_top_k=20` (`app/config.py:53,72-75`); history ≤6×2000 chars (`app/api.py:354,358`) | LightRAG `/query/stream` |
| 2 | Bridge task short-circuit | `app/api.py:446-455` | entire OpenWebUI task prompt (embeds the chat) | **none** | LightRAG `/query/stream`, `mode=bypass` |
| 3 | Bridge keyword step | body `app/keywords.py:410-418`, prompt `app/keywords.py:115-122` | fixed 2779-char template + query | yes (template fixed; `max_tokens=2000` out) | direct to `LLM_BINDING_HOST` (`app/keywords.py:388`) |
| 4 | Bridge rewrite step | body `app/rewrite.py:331-337`, prompt `app/rewrite.py:187-191` | 1076-char template + ≤3 pairs capped 600/1200 chars (`app/rewrite.py:144-145,173`) + **latest message uncapped** (`app/rewrite.py:190`) | partial | direct to the same endpoint (`app/keywords.py:388` shared helper) |
| 5 | Gateway `/query` first pass | `app/api.py:1234-1248` | query, `top_k` 12/24 (`app/models.py:16`, `app/api.py:1223`), `chunk_top_k=16` (`app/config.py:57`) | **no token caps sent** | LightRAG `/query` |
| 6 | Gateway naive fallback pass | `app/api.py:1388-1390` | same payload, `mode=naive` | **no token caps sent** | LightRAG `/query` |
| 7 | Gateway multi-query rewrite | `app/multi_query.py:203-220` | instruction + original query | effectively tiny (`mode=bypass`, `top_k=1`) | LightRAG `/query` |
| 8 | Gateway multi-query retrieval passes | `app/api.py:1351`, `app/api.py:1397` | as row 5/6, up to `MULTI_QUERY_REWRITE_COUNT=2` times (`app/config.py:31`) | **no token caps sent** | LightRAG `/query` |
| 9 | Graph expansion entity extraction | `app/graphrag.py:107-116` | template + ≤3 seed citations × 2000 chars (`app/graphrag.py:52`) + query | yes (~6.5k chars); `GRAPH_EXPANSION_ENABLED` default off (`app/config.py:37`) | LightRAG `/query`, `mode=bypass` |
| 10 | Graph neighbour expansion | `app/graphrag.py:187-196` | entity name list | yes (≤3 neighbours kept, `app/graphrag.py:374`) | LightRAG `/query`, `mode=bypass` |
| 11 | Graph-aware synthesis | `app/graph_synthesis.py:58-67` | ≤3 evidence items, vector content 140 chars (`app/graph_synthesis.py:12-13`) | yes (~1k chars); `GRAPH_SYNTHESIS_REPLACE_ANSWER` default off (`app/config.py:90-92`) | LightRAG `/query`, `mode=bypass` |
| 12 | `POST /generate-document` | retrieval `app/generation.py:16-24`, LLM `app/generation.py:40-46` | instruction + user request + **all** returned chunk contents (`app/generation.py:26-33`) | **none** | LightRAG `/query`, `mode=bypass` |
| 13 | `/api/chat`, `/api/generate` passthrough | `app/api.py:1713`, `app/api.py:1728` (gate `app/api.py:681`) | raw client body, full message list | **none** | LightRAG Ollama-compat endpoints |
| 14 | Ingest | `app/worker.py:464-472` | whole document text (chunking upstream) | **none** | LightRAG `/documents/text`, `/documents/upload` |

---

## Q3 — worst-case estimate (chars/4, gateway-side contribution only)

Settings used are the code defaults (`app/config.py`), printed by the script:
`BRIDGE_TOP_K=40 BRIDGE_CHUNK_TOP_K=20 BRIDGE_MAX_ENTITY_TOKENS=10000
BRIDGE_MAX_RELATION_TOKENS=10000 BRIDGE_MAX_TOTAL_TOKENS=32000 BRIDGE_HISTORY_TURNS=3
GATEWAY_CHUNK_TOP_K=16 RETRIEVAL_MODE_DEFAULT=mix`.

| Path | Gateway-side chars | ~tokens | Retrieved-context cap sent | Can it exceed 40k tokens? |
|---|---|---|---|---|
| 3 keyword step | 3,458 | 864 | n/a (no retrieval) | no |
| 4 rewrite step | 7,110 (3 capped turns) | 1,778 | n/a | only if the user pastes a ~150k-char message (`latest` uncapped) |
| 11 synthesis | 1,010 | 252 | n/a | no |
| 7 multi-query rewrite | 400 | 100 | n/a | no |
| 9 graph entity extraction | 6,513 | 1,628 | n/a | no |
| 1 bridge direct | 8,400 (query + 3 turns) | 2,100 | 32,000 tokens | no — ceiling ≈ 32k + ~2.1k + LightRAG template |
| 5/6/8 gateway `/query` pass | 600 (query) | 150 | **none sent** | **yes, if the container's LightRAG defaults exceed 32k** — LIGHTRAG-INTERNAL |
| 2 task short-circuit | 20,000 → 80,000+ (scales with chat length) | 5,000 → 20,000+ | **none sent** | **yes** — unbounded; 45k–80k tokens needs a ~180k–320k-char task prompt |
| 12 generate-document | 192,125 (40 refs × 4,800 chars) | 48,031 | **none sent** | **yes** — unbounded, scales with reference count × chunk size |
| 13 Ollama passthrough | = whole conversation | unbounded | **none sent** | **yes** — unbounded |
| 14 ingest | = whole document | unbounded | n/a (LightRAG chunks) | depends on LightRAG chunk size — LIGHTRAG-INTERNAL |

Paths that can plausibly land in the observed 45k–80k band, in this repo's own words:
**2 (task prompts), 12 (generate-document), 13 (passthrough)**, plus **5/6/8** if the
container's LightRAG token defaults are larger than the bridge's 32000.

---

## Q4 — cache-prefix notes (what the code shows)

- **Bridge direct (row 1):** the variable text (query, history, keywords) travels as JSON
  *fields*; the leading text of the final prompt is LightRAG's fixed answer template, so the
  shared prefix is as large as LightRAG's template before the retrieved context begins —
  LIGHTRAG-INTERNAL, exact boundary not readable here. Consistent with the observed
  cached ~32k requests decoding at 50–60 tok/s.
- **Task short-circuit (row 2):** the prompt is one blob whose first ~200 chars are the
  OpenWebUI task header (`### Task:` …) and everything after is the conversation, which
  differs per chat and per turn → shared prefix is only the header. Cache miss by
  construction. (`app/api.py:449-454`, `app/api.py:162-177`.)
- **Keyword step (row 3):** fixed 2779-char template first, `User Query: {query}` last
  (`app/keywords.py:41-72`) → ~700-token shared prefix.
- **Rewrite step (row 4):** fixed rules block first (~1,000 chars), conversation in the
  middle, latest message last (`app/rewrite.py:104-122`, `app/rewrite.py:187-191`) →
  ~250-token shared prefix.
- **generate-document (row 12):** prompt opens with a 56–65-char document-type instruction
  (`app/generation.py:4-11`), then the user request, then all context → shared prefix
  < 20 tokens.
- **Gateway `/query` vs bridge (rows 5/6/8):** same LightRAG template family, different
  `mode`/params; nothing in this repo reorders the prompt, so the prefix behaviour is the
  same as row 1 modulo LightRAG's mode-specific template.
- Unrelated to oMLX prefix caching but worth repeating for packet-23: LightRAG has its own
  query cache and can answer a byte-identical retry from it
  (`scripts/lightrag_log_stages.py:24`, `tests/test_observability.py:386`).

---

## Recommendations (one-line proposals; nothing implemented)

1. Cap the task prompt before sending it — new `BRIDGE_TASK_MAX_CHARS` applied at `app/api.py:446`.
2. Add `max_total_tokens` (reuse `BRIDGE_MAX_TOTAL_TOKENS`) to the bypass payload at `app/api.py:449-454` — verify first that LightRAG honours it in bypass mode.
3. Send `max_entity_tokens` / `max_relation_tokens` / `max_total_tokens` in the gateway `/query` payload — `app/api.py:1234-1247`, reusing `app/config.py:73-75`.
4. Cap the joined context in `generate_document` — `app/generation.py:33` (new setting or reuse `BRIDGE_MAX_TOTAL_TOKENS`).
5. Decide the Ollama passthrough policy for non-`lightrag:latest` model names — `app/api.py:681`, `app/api.py:1713`, `app/api.py:1728`.
6. Cap the rewrite step's `latest` message with the existing `BRIDGE_REWRITE_MAX_USER_CHARS` — `app/rewrite.py:190`.
7. Cheapest decisive measurement: log `prompt_chars` on the bridge line — `app/observability.py:120-146` currently logs `prompt_sha256` and `task_prompt` but no length, so gateway logs cannot confirm F1.
8. Container side, outside this repo: read `MAX_TOTAL_TOKENS` / `MAX_ENTITY_TOKENS` / `MAX_RELATION_TOKENS` / `TOP_K` / `CHUNK_TOP_K` in the shared `.env` (`compose.yml:5-7`) to settle rows 5/6/8 and 14.
9. Correlate oMLX prompt-token counts with the gateway line by timestamp ordering (LightRAG logs carry no request id — `scripts/lightrag_log_stages.py:31`) and check `task_prompt=true` frequency against the 45k–80k population.

---

## Unknowns (not answerable from this repo)

- **U1** Whether OpenWebUI truncates its own `### Task:` prompts, and how big they actually
  get. OpenWebUI is a separate image (`compose.yml:63-72`); nothing in this repo bounds them.
- **U2** Whether LightRAG v1.4.15 applies `max_total_tokens` in `mode=bypass` at all
  (depends on LightRAG internals, `operate.py` bypass branch).
- **U3** LightRAG's defaults for `max_total_tokens` / `max_entity_tokens` /
  `max_relation_tokens` / `top_k` / `chunk_token_size` when the gateway omits them
  (depends on LightRAG v1.4.15 internals + the container `.env`, which is not in the repo).
- **U4** Whether the 45k–80k population is query-time or ingest-time: the oMLX evidence as
  quoted identifies prompt size only, and the gateway log has no prompt-length field
  (`app/observability.py:120-146`).
- **U5** How LightRAG's Ollama-compat `/api/chat` assembles its prompt (F2's actual size).
- **U6** Which model name OpenWebUI actually sends; F2 fires only when it is not
  `lightrag:latest` (`app/api.py:84`).
- **U7** All token numbers here are chars/4; oMLX's tokenizer was not available offline.

---

## Appendix — `scripts/prompt_size_estimate.py` output (run once)

```
path | gateway chars | ~tokens (chars/4) | LightRAG retrieval cap sent | verdict
--- | --- | --- | --- | ---
bridge keyword call (keywords.py:410-418, direct to LLM) | 3,458 | 864 | none sent | fixed template (2779 chars) + query; max_tokens=2000 out
bridge rewrite call (rewrite.py:331-337, direct to LLM) | 7,110 | 1,778 | none sent | history capped (3 x 600/1200 chars); 'latest' message is UNCAPPED (rewrite.py:190)
graph synthesis (graph_synthesis.py:58-67) | 1,010 | 252 | none sent | capped: 3 items, vector content 140 chars (graph_synthesis.py:12-13)
multi-query rewrite (multi_query.py:203-220) | 400 | 100 | none sent | capped by the query itself; mode=bypass top_k=1
graph expansion entity extraction (graphrag.py:107-116) | 6,513 | 1,628 | none sent | capped: 3 seeds x 2000 chars (graphrag.py:52)
bridge direct /query/stream (api.py:549-552) | 8,400 | 2,100 | 32000 | capped: max_total_tokens=32000 + history <= 6 msgs x 2000 chars
gateway /query pass (api.py:1234-1248) | 600 | 150 | none sent | NO CAP SENT: max_*_tokens absent -> LightRAG container defaults decide (LIGHTRAG-INTERNAL)
bridge task short-circuit /query/stream (api.py:446-455) | 20,000 | 5,000 | none sent | UNBOUNDED: mode=bypass sends the prompt verbatim; no token field in the payload
bridge task short-circuit /query/stream (api.py:446-455) | 80,000 | 20,000 | none sent | UNBOUNDED: mode=bypass sends the prompt verbatim; no token field in the payload
generate_document LLM call (generation.py:40-46) | 192,125 | 48,031 | none sent | UNBOUNDED: every reference's full chunk content is concatenated (generation.py:26-31); no cap, mode=bypass

settings actually used: BRIDGE_TOP_K=40 BRIDGE_CHUNK_TOP_K=20 BRIDGE_MAX_ENTITY_TOKENS=10000 BRIDGE_MAX_RELATION_TOKENS=10000 BRIDGE_MAX_TOTAL_TOKENS=32000 BRIDGE_HISTORY_TURNS=3 GATEWAY_CHUNK_TOP_K=16 RETRIEVAL_MODE_DEFAULT=mix

Note: 'gateway chars' is only what THIS repo puts in the prompt. LightRAG adds its own
template plus the retrieved context (bounded by max_total_tokens when sent). Anything not
listed here is LIGHTRAG-INTERNAL and was not measured.
```

Script labels use the same line numbers as the tables above. It imports `app.*` (so it needs
the six mandatory settings as env vars) and performs no network I/O.
