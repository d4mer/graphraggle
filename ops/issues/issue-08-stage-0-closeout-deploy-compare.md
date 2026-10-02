# Issue 08 — Stage 0 close-out, deploy, and compare

**Base branch:** `packet-18c-agent-prompt` (contains Stage 0, the corrected docs, and this file)
**Work on:** `packet-18d-stage-0-closeout` (create it; never commit to any existing branch)
**Tracked in Plane:** GRAG-11, GRAG-12 (code); GRAG-1, 2, 3, 4, 6, 9, 10, 13 (verify live)
**Source PRD:** `ops/prds/prd-precision-first-retrieval.md`
**Follows:** `ops/issues/issue-07-stage-1-instrumentation.md` (Part C below hands off to it)

You are an implementation and operations agent. Read this whole file before
touching anything. It has three parts and two hard gates:

- **Part A** — finish two Stage 0 items in code (GRAG-12, then GRAG-11).
- **Part B** — deploy the gateway to production and compare answers before and after.
- **GATE 1** — stop after Part B and report. Do not continue until the user replies.
- **Part C** — hand off to Stage 1 (only after the user confirms).

---

## Why this exists

The system answers questions over SAP/OMP implementation documents. LightRAG
v1.4.15 (`lightrag-server`) holds the index and knowledge graph. A FastAPI
gateway (`app/`, container `rag-gateway-api`) fronts it. OpenWebUI talks to the
gateway through an Ollama-compatible bridge (`POST /api/chat`, model
`lightrag:latest`).

Users get much worse answers through OpenWebUI than through the LightRAG
WebUI. The diagnosed causes that do not depend on any feature flag: the bridge
asked for `top_k=4`, the default mode was `hybrid`, a weak-signal `naive`
fallback overwrote good answers, citations were cut to 5 and never reached
OpenWebUI, conversation history was dropped, and `app/generation.py` had
literal `\n` sequences. Stage 0 (already on your base branch) fixed all of
those except two.

**Still open, and why they matter before deploy:**

- **GRAG-12 — auxiliary calls.** OpenWebUI fires title, tag and follow-up
  generation requests at the same model. Each one runs the full retrieval
  pipeline, now with roughly ten times more context. That is the main
  production-load risk of Stage 0. It must land before deploy.
- **GRAG-11 — history.** The bridge reads only the last user message, so
  follow-up questions ("what about the second one?") lose their context.
  `BRIDGE_HISTORY_TURNS` already exists in `app/config.py` and the installer
  but **nothing reads it**. This item either makes it live or removes it. We
  are making it live.

Do not change retrieval defaults, token budgets, the reranker, or the graph
flags. Do not start on GRAG-5, GRAG-7 or GRAG-8 (see "Out of scope").

---

## Hard rules

1. Work only on `packet-18d-stage-0-closeout`. Create it from
   `packet-18c-agent-prompt`. Do not rebase, force-push, or rewrite history.
2. Do not push unless you have credentials that work non-interactively. If a
   push fails, stop trying, say so, and leave the commits local.
3. Never touch the LightRAG index or any data volume: no deleting or
   re-creating `lightrag-server`, `rag_storage`, `source_docs`, `uploads`,
   `state`, or `open-webui-data`. Never run `docker compose down -v`, `docker
   volume rm`, `docker system prune`, or any re-index script.
4. In production, restart **only** `gateway-api`. Do not restart
   `lightrag-server`, `ingest-worker` or `open-webui`.
5. Do not run `scripts/install_rag_stack.sh` on production. It regenerates
   configuration. Edit the production `.env` by hand, adding only the keys
   listed in Part B.
6. Do not change OpenWebUI settings. Report recommendations instead.
7. Do not print secrets. When you record `.env` values, redact anything whose
   key contains `KEY`, `TOKEN`, `PASSWORD` or `SECRET`.
8. If any STOP condition below fires, stop, write down exactly what you saw,
   and wait for the user. Do not improvise around it.
9. Do not edit Plane or understory. List what should change in your report.

---

## Part A — Code changes

### A0. Orient and get a green baseline

Read, in this order: `app/api.py` (functions `extract_ollama_prompt`,
`build_sources_block`, `answer_ollama_bridge_prompt`,
`maybe_handle_ollama_bridge`, `execute_query_pass`, and the `query` endpoint),
`app/models.py` (`QueryRequest`), `app/config.py`, `scripts/install_rag_stack.sh`.

Then run the existing suite. It needs env vars at import time:

```bash
pip3 install --break-system-packages pytest pytest-asyncio httpx pydantic pydantic-settings
export RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test \
       LIGHTRAG_BASE_URL=http://localhost:9621 \
       SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db
python3 -m pytest tests -q
```

Expect 290 passing. If the baseline is not green, STOP and report; do not
start on new work over a red baseline.

### A1. GRAG-12 — short-circuit OpenWebUI auxiliary calls

**Behaviour.** OpenWebUI's task prompts all begin with the literal text
`### Task:` (title generation, tag generation, follow-up suggestions, search
query generation, autocomplete). When the bridge receives one, it must make
**one** LightRAG call in `bypass` mode and return the answer. It must not run
the retrieval pipeline, multi-query, graph paths, naive fallback, reranking,
or append a Sources block.

**Implement:**

- `is_openwebui_task_prompt(prompt: str) -> bool` in `app/api.py`: true when
  `prompt.lstrip()` starts with `### Task:`. Nothing looser. A normal
  question that mentions the word "task" must be false.
- A new setting `bridge_task_shortcircuit: bool`, alias
  `BRIDGE_TASK_SHORTCIRCUIT`, default `True`, in `app/config.py`. Add
  `BRIDGE_TASK_SHORTCIRCUIT=True` to the gateway env block in
  `scripts/install_rag_stack.sh`.
- In the bridge, before the normal path: if the setting is on and the prompt
  is a task prompt, call `client.post_json("/query", {"query": prompt, "mode":
  "bypass", "include_references": False})` once and return
  `str(result.get("response", "")).strip()`.
- If that bypass call raises, return an empty string. **Never** fall through
  to the full pipeline from a task prompt; that defeats the purpose.
- The task prompt already embeds the chat history OpenWebUI wants summarised.
  Do not extract or attach conversation history for task prompts.

**Tests** (new file `tests/test_ollama_bridge.py`; monkeypatch `client` and
`query`):

- detection: true for prompts that start with `### Task:\nGenerate a concise,
  3-5 word title`, `### Task:\nGenerate 1-3 broad tags`, `### Task:\nSuggest
  3-5 relevant follow-up questions`, and for the same with leading whitespace;
  false for `What is the firm horizon task for CMO?`, `Task: summarise`, and an
  empty string.
- a task prompt makes exactly one `/query` call, with `mode == "bypass"`, and
  never calls `query()`.
- a task prompt with the setting off goes through the normal path.
- a failing bypass call returns `""` and does not call `query()`.
- a normal prompt still goes through `query()` and still gets a Sources block.

### A2. GRAG-11 — thread conversation history through the bridge

**Behaviour.** For `/api/chat`, previous turns from `payload["messages"]` go
to LightRAG as `conversation_history`, so follow-up questions resolve.

**First, verify LightRAG supports it. Do this before writing code.** Fetch
`GET /openapi.json` from the running `lightrag-server` (in a dev or test
environment if you have one, otherwise from production read-only) and confirm
the `/query` request schema contains `conversation_history` and
`history_turns`. If either is missing in v1.4.15: **STOP** and report. Do not
invent a replacement.

**Implement:**

- `QueryRequest` in `app/models.py` gains
  `conversation_history: list[dict[str, str]] | None = None` and
  `history_turns: int | None = None`.
- `extract_ollama_history(payload, turns: int) -> list[dict[str, str]]` in
  `app/api.py`:
  - take `payload["messages"]` without the final user message (the one used as
    the prompt);
  - keep only roles `user` and `assistant` with non-empty string content;
  - keep the most recent `turns * 2` messages (a "turn" is one user message
    plus one assistant reply); `turns <= 0` returns `[]`;
  - **strip the Sources block from assistant messages.** Stage 0 appends
    `\n\n---\n\n**Sources**\n\n...` to every bridge answer, and OpenWebUI sends
    that text back on the next turn. Define the marker once as a module
    constant, use it in both `build_sources_block` and this function, and
    cut everything from the marker onward;
  - cap each message at 2000 characters;
  - `/api/generate` has only `prompt`; it never has history.
- `answer_ollama_bridge_prompt(prompt, top_k=None, history=None)`: when
  `history` is non-empty, pass `conversation_history` and `history_turns`
  (`settings.bridge_history_turns`) on the `QueryRequest`.
- `execute_query_pass` adds `conversation_history` and `history_turns` to the
  LightRAG payload **only when present**. When they are absent the payload
  must be byte-for-byte what it is today. Apply it to every pass (primary,
  multi-query rewrites, and the naive fallback).
- `maybe_handle_ollama_bridge` builds the history from the payload and passes
  it in, except for task prompts (A1).
- Clarify the comment on `bridge_history_turns` in `app/config.py`: it is the
  number of previous user/assistant exchanges, and 0 disables history.

**Tests** (extend `tests/test_ollama_bridge.py`):

- history excludes the final user message, drops `system` messages, keeps
  order, and respects `turns`;
- Sources blocks are removed from assistant messages, including when the
  marker appears mid-message;
- empty and non-string content is dropped; over-long content is capped;
- `turns=0` gives `[]`;
- the LightRAG payload has no history keys when history is empty, and has them
  when it is not (assert on the dict passed to `client.post_json`);
- history reaches the multi-query and naive-fallback passes too.

### A3. Fix an environment-variable collision (do this first, it is a real bug)

`lightrag-server`, `gateway-api` and `ingest-worker` all load the **same**
`.env` file (`env_file: - .env` on each service in the installer's
`compose.yml`). Stage 0 named the gateway's chunk setting `CHUNK_TOP_K`, which
is also the name LightRAG itself reads for its own default `chunk_top_k`
(LightRAG default 20). One line in a shared file therefore silently changes
LightRAG's own behaviour for every client that does not pass `chunk_top_k`,
including the LightRAG WebUI we compare against, the next time
`lightrag-server` restarts. The gateway always sends `chunk_top_k` explicitly,
so the gateway itself is unaffected, which is why it went unnoticed.

Fix:

- In `app/config.py`, change the alias of `chunk_top_k` from `CHUNK_TOP_K` to
  `GATEWAY_CHUNK_TOP_K` (same default, 16).
- In `scripts/install_rag_stack.sh`, change `CHUNK_TOP_K=16` in the gateway
  block to `GATEWAY_CHUNK_TOP_K=16`.
- Check every other alias added in Stage 0 and in this issue for the same
  problem: `RETRIEVAL_MODE_DEFAULT`, `BRIDGE_TOP_K`, `CITATION_TOP_K`,
  `BRIDGE_HISTORY_TURNS`, `BRIDGE_TASK_SHORTCIRCUIT`,
  `GRAPH_SYNTHESIS_REPLACE_ANSWER`. Compare each against LightRAG v1.4.15's
  `env.example` (`https://raw.githubusercontent.com/HKUDS/LightRAG/v1.4.15/env.example`).
  Report any other collision and fix it the same way. Do not rename anything
  that does not collide.
- Add a test that fails if any gateway setting alias equals a LightRAG
  variable name. Keep a short hard-coded list of LightRAG names in the test
  (`TOP_K`, `CHUNK_TOP_K`, `MAX_ENTITY_TOKENS`, `MAX_RELATION_TOKENS`,
  `MAX_TOTAL_TOKENS`, `RERANK_BY_DEFAULT`, `COSINE_THRESHOLD`, `MAX_GLEANING`,
  `ENTITY_TYPES`, `CHUNK_SIZE`, `CHUNK_OVERLAP_SIZE`, `WORKERS`, `TIMEOUT`,
  `PORT`, `MAX_ASYNC`, `MAX_PARALLEL_INSERT`).

### A3b. Fix two wrong comments in the installer

In `scripts/install_rag_stack.sh`, two comments overstate things and should
be corrected (comments only, no value changes):

- the "Retrieval defaults" comment says `hybrid` "returns only index-time
  entity/relation summaries". Replace with: `hybrid` weights context toward
  entity and relation descriptions; `mix` also adds directly retrieved
  chunks.
- the "Context budget" comment says that without these variables entity and
  relation context crowd out chunks. Replace with: these values equal
  LightRAG v1.4.15's defaults and are pinned to document intent.

### A4. Finish Part A

- The full suite passes (290 plus your new tests). Paste the final count.
- Commit in small, logical commits on `packet-18d-stage-0-closeout`. Message
  style: `feat: ...`, `fix: ...`, `docs: ...`. End each message with the
  attribution trailer your environment specifies.
- Do not push yet. Part B deploys from this branch, and you push at the end of
  Part B once the deploy has passed verification.

---

## Part B — Deploy and compare

The deploy has a hard STOP early on, and nothing after it runs until that
check passes. Production is believed to be the Thinkpad host and is believed
to be a **non-git copy that may have diverged** from the repo. Confirm both.

### B0. Recon (read-only)

On the production host, without changing anything:

1. Find the compose project directory, how `gateway-api` is built, and the
   current image id for `local/rag-gateway:latest`.
2. Read the live `.env` and record these values (redact secrets):
   `RETRIEVAL_MODE_DEFAULT`, `BRIDGE_TOP_K`, `GATEWAY_CHUNK_TOP_K`,
   `CITATION_TOP_K`, `GRAPH_EXPANSION_ENABLED`, `GRAPH_NATIVE_ENABLED`,
   `MULTI_QUERY_ENABLED`, `RERANK_ENABLED`, `RERANK_BINDING_HOST`,
   `RERANK_BY_DEFAULT`, `TOP_K`,
   `CHUNK_TOP_K` (**this one is LightRAG's own; if the shared `.env` already
   has it, say who set it and to what**), `MAX_ENTITY_TOKENS`, `MAX_RELATION_TOKENS`,
   `MAX_TOTAL_TOKENS`, `MAX_GLEANING`, `ENTITY_TYPES`. Mark any that are unset.
   **This answers a question we have never been able to answer: whether the
   graph flags are on in production.**
3. **Divergence check.** Compare the production `app/` directory against the
   tree of branch `packet-17-true-graphrag-reset` (the version Stage 0 was
   built on). Use checksums or `diff -r`, ignoring `__pycache__`.
   **STOP if production `app/` differs from `packet-17` in any file.**
   Report the exact diff. Do not deploy over unknown changes.
4. Confirm `GET /health` on the gateway and on `lightrag-server` both
   respond, and record container uptime.

### B1. Backup (before any change)

- `docker tag local/rag-gateway:latest local/rag-gateway:pre-packet-18`
- Copy the production `.env` to `.env.pre-packet-18` in the same directory,
  mode `600`.
- Copy the production `app/` directory to `app.pre-packet-18/` beside it.
- Copy the gateway state database (`STATE_DB_PATH`, in `./state`) to
  `state/ingest.db.pre-packet-18`.

### B2. Baseline, before the deploy

Use the questions in `eval/datasets/curated/queries.json`, ids `q001` to
`q010`, unless the user has supplied `eval/datasets/stage0_compare.json`, in
which case use that file instead (same shape: `id`, `query`).

For each question, record two measurements and save them as JSON under
`docs/ops/runs/` (this folder is gitignored):

1. **Gateway path (what OpenWebUI uses):** `POST /api/chat` on the gateway
   with `{"model": "lightrag:latest", "messages": [{"role": "user",
   "content": <query>}], "stream": false}`. Work out the auth the gateway
   needs from `app/` and the `.env`; do not guess.
2. **LightRAG direct (the ceiling):** `POST /query` on `lightrag-server` with
   `{"query": <query>, "mode": "mix", "top_k": 40, "chunk_top_k": 20,
   "include_references": true}`. (`chunk_top_k` 20 is LightRAG's own default,
   which the LightRAG WebUI uses; that is why it differs from the gateway's 16.)

For each, record: the full answer text, word count, the number of distinct
sources cited, wall-clock latency, and HTTP status. Run each question once
sequentially. Do not run them in parallel.

### B3. Deploy

1. Check out `packet-18d-stage-0-closeout` and put its `app/` into the
   production directory. Do not copy anything else.
2. Add these keys to the production `.env`, keeping any existing value you
   find for them unless it contradicts the list (if it does, report it and use
   the value below):

   ```
   RETRIEVAL_MODE_DEFAULT=mix
   BRIDGE_TOP_K=40
   GATEWAY_CHUNK_TOP_K=16
   CITATION_TOP_K=12
   BRIDGE_HISTORY_TURNS=3
   BRIDGE_TASK_SHORTCIRCUIT=True
   GRAPH_SYNTHESIS_REPLACE_ANSWER=False
   ```

   Leave LightRAG's own settings (`MAX_*_TOKENS`, `TOP_K`, `CHUNK_TOP_K`,
   rerank, etc.) exactly as found. **Do not add a bare `CHUNK_TOP_K` line.**
   If an earlier Stage 0 deploy already added `CHUNK_TOP_K=16`, report it, and
   remove it only if the value is 16 and you can show it was added by Stage 0
   (compare against `.env.pre-packet-18`); LightRAG reads that file only at
   start, so tell the user that `lightrag-server` would pick it up on its next
   restart. Do not restart `lightrag-server` yourself.
3. Rebuild and restart only the gateway: `docker compose up -d --build
   gateway-api` (adapt to the compose command production actually uses).
4. Wait for `/health`, then read the last 100 log lines for errors.

### B4. Verify

All of these must pass. Record the evidence for each.

- `GET /health` is OK and the container is not restarting.
- `POST /api/chat` with a normal question returns 200, a non-empty answer, and
  a `**Sources**` block.
- **Auxiliary-call check.** Send
  `{"role":"user","content":"### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history.\n### Chat History:\n<chat_history>\nUSER: What is eCommit?\nASSISTANT: eCommit is a manual trigger in shipment planning.\n</chat_history>"}`
  to `/api/chat`. It must return in under 15 seconds, with no Sources block,
  and the gateway log must show no retrieval passes for it.
- **History check.** Send a two-turn conversation: first a real question
  and its previous answer (including a `**Sources**` block in the assistant
  message), then a follow-up that only makes sense with that context (for
  example "and who owns that step?"). The answer must reflect the earlier
  topic. Compare with the same follow-up sent alone.
- **No regression on direct API use.** `POST /query` with a plain body (no
  `mode`) returns 200 and `query_scope` reports the default mode as `mix` and
  `chunk_top_k_used` as 16.
- Re-run the B2 questions in the same way, and save the "after" results
  beside the "before" ones.

**Rollback triggers (act without asking):** any 5xx from the gateway; any
timeout; the container restarting; or the median `/api/chat` latency in the
"after" run exceeding 120 seconds. These thresholds are defaults, so mention
it in the report if you used them.

### B5. Rollback (only if a trigger fires)

- **Tier 1, environment only:** set `RETRIEVAL_MODE_DEFAULT=hybrid`,
  `BRIDGE_TOP_K=4`, `CITATION_TOP_K=5`; then `docker compose up -d
  gateway-api` (no rebuild). Re-check `/health` and one `/api/chat` call.
- **Tier 2, full:** restore `.env.pre-packet-18` and `app.pre-packet-18/`,
  retag `local/rag-gateway:pre-packet-18` as `latest`, then recreate
  `gateway-api`. Do not touch any other service.

### B6. Report (this is GATE 1)

Write `docs/ops/runs/stage0-deploy-report.md` (gitignored) **and** paste the
whole thing into your final message. It must contain:

1. **Live flag values** from B0 (secrets redacted), and the divergence result.
2. **What was deployed:** commit hashes, image id before and after.
3. **Before and after table**, per question: gateway-path word count and
   source count and latency, against the LightRAG-direct figures. Mark every
   question where the gateway answer is still clearly thinner than LightRAG's.
4. **B4 evidence** for each check, pass or fail.
5. **Production impact:** latency change, any errors, and an estimate of how
   many OpenWebUI auxiliary calls were short-circuited (count them in the
   logs).
6. **Recommendations for OpenWebUI** (do not apply): whether to set a
   dedicated Task Model, and whether to disable title, tag or follow-up
   auto-generation.
7. **What you did not verify** and why. Do not claim anything you did not
   observe.
8. **Plane updates to make** (GRAG-11, GRAG-12, and which of GRAG-1, 2, 3, 4,
   6, 9, 10, 13 are now verified live). Do not edit Plane yourself.

Then push the branch if you can (rule 2), and **stop. Do not begin Part C.**

---

## Part C — Stage 1 (do not start until the user says so)

When the user replies that the comparison looks right, implement
`ops/issues/issue-07-stage-1-instrumentation.md` in full, with one change:
branch from `packet-18d-stage-0-closeout`, not `packet-18`, onto
`packet-19-stage-1-instrumentation`.

Reminder of the one rule in that file that matters most: **you must not
invent the probe questions.** The user writes them. Build the scaffolding,
the validator and the comparison script, and stop for the user to supply the
probe set.

---

## Out of scope

- **GRAG-5** (synthesis prompt word cap) and **GRAG-7** (delete the
  pseudo-graph expansion code). Both only matter if the graph flags are on in
  production. Report the live flag values (B0) and let the user decide.
- **GRAG-8** (double rerank). LightRAG's internal rerank decides which chunks
  enter the answer context; the gateway's only reorders citations. Leave
  `RERANK_BY_DEFAULT` as found. This is decided later with the probe set.
- Anything in Stage 2 or later (entity types, extraction prompts, gleaning,
  chunking, re-indexing).
- Changing LightRAG's environment, image, or index.

## Definition of done for this issue

Part A: tests green, commits on `packet-18d-stage-0-closeout`. Part B: the
deploy verified (or rolled back with the reason stated), the before and after
report delivered. Part C only starts on the user's go-ahead.
