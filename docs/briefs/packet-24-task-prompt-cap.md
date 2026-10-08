# Brief: packet-24 - cap OpenWebUI task prompts and log prompt length

Branch: create `packet-24-task-prompt-cap` FROM `packet-23-empty-retry` (that is what production runs). Do not branch from
packet-22-ll-seed. Do not merge anything. Do not touch the seed code.
Background (read only): `git show packet-22-ll-seed:docs/ops/runs/prompt-size-investigation.md` (your own report, Finding F1,
recommendations 1 and 7). Production fact: oMLX sees a population of 45k-80k-token prompts that decode at 3-5 tok/s; the leading
suspect is the `### Task:` short-circuit (app/api.py ~L437-455) which forwards the whole OpenWebUI task prompt (it embeds the chat)
to LightRAG `mode=bypass` with no length limit.

## Intent
Bound the size of the task prompt the bridge forwards, and make prompt length visible in logs so we can verify the cause.
Behaviour for normal (non-task) requests must not change.

## Changes (exactly these)
1. `app/config.py`: new setting `bridge_task_max_chars: int = Field(default=24000, alias="BRIDGE_TASK_MAX_CHARS")`. 0 disables the cap.
   Comment: ~6k tokens; protects the LLM prefix cache and decode speed.
2. `app/api.py` in the task short-circuit branch (the `is_openwebui_task_prompt` block): before the POST, if
   `settings.bridge_task_max_chars > 0` and `len(prompt) > settings.bridge_task_max_chars`, shorten the prompt with a helper
   `cap_task_prompt(prompt, max_chars) -> str` placed near `is_openwebui_task_prompt`. Rule: keep the first 25% of the budget
   (it holds the task header/instructions) and the last 75% (it holds the most recent chat turns and the closing instruction),
   joined by a single line `\n[...truncated...]\n`. Result length must be <= max_chars. Pure function, no I/O.
   Keep the log call using the ORIGINAL prompt for `prompt_sha256` as today.
3. `app/observability.py`: add field `prompt_chars` (int, len of the prompt passed to `start_bridge_log`) to the record built in
   `start_bridge_log`, and for the task branch also `forwarded_chars` (int, length actually sent after capping). Update the module
   docstring field list (top of file, near line 18). NEVER log prompt text. To pass forwarded_chars, add an optional keyword
   argument `forwarded_chars: int | None = None` to `start_bridge_log`; only include the key when it is not None.
4. Do NOT add max_total_tokens to the bypass payload (unverified that LightRAG honours it in bypass mode).
5. Docs: `docs/08-change-log.md` one entry; `docs/05-known-good-config.md` mention `BRIDGE_TASK_MAX_CHARS` (default 24000).
   If `.env`-style variable lists exist in `scripts/install_rag_stack.sh`, add `BRIDGE_TASK_MAX_CHARS=24000` with a one-line comment.

## Tests (tests/, unittest style like neighbouring tests; no network)
- cap_task_prompt: below the limit -> unchanged; above -> len <= max, starts with the first 25% of the original prefix,
  ends with the original suffix, contains the marker once; max=0 -> unchanged (test via the call site or setting check).
- Task branch end-to-end with a mocked client.proxy: a 100k-char task prompt results in a proxied body whose `query` is <= the cap;
  a 1k-char task prompt is sent verbatim; the log record has `prompt_chars=100000` and `forwarded_chars<=cap`; no log value contains prompt text.
- Non-task request: payload unchanged (existing tests must still pass untouched).

## Acceptance (run yourself, paste output)
`RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q`
Expected: all pass (450 on this base minus the seed tests, plus yours - report the exact number). Then prove the new tests fail on the old code:
`git stash`-free way: `git checkout packet-23-empty-retry -- app/` in a scratch copy is NOT allowed here; instead state which tests reference
`cap_task_prompt`/`prompt_chars`/`forwarded_chars` (they cannot pass without the change) and show `git diff --stat packet-23-empty-retry`.
Commit on packet-24-task-prompt-cap with a clear message. Do not push; I will. Hand the row back to orch-lead@graphraggle.
