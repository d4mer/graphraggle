# Packet 28 - unattended accuracy-loop driver (scripts/overnight_loop.py)

Goal: one self-contained Python script (stdlib only: urllib, json, subprocess, argparse, time, pathlib, re; NO third-party imports) that runs on the
Thinkpad under tmux with no agent and no approval prompts, and runs an ordered list of EXPERIMENTS overnight. Each experiment = change some LightRAG
env vars, optionally re-ingest the Solvay transcripts, run the probe sets, judge the answers, record scores. It must survive errors, be resumable, and
always put the production config back at the end. Also write tests/test_overnight_loop.py (offline: all IO goes through an `Ops` class that tests replace
with a fake; no network, no docker, no sleeping - inject a fake clock/sleep). Test command:
`RAG_API_KEY=test LIGHTRAG_INTERNAL_API_KEY=test LIGHTRAG_BASE_URL=http://localhost:9621 SOURCE_DOCS_DIR=/tmp/src UPLOADS_DIR=/tmp/up STATE_DB_PATH=/tmp/state.db .venv/bin/python -m pytest tests/ -q`
(all 511+ existing tests must still pass). Work in a worktree: `git worktree add ../graphraggle-p28 -b packet-28-loop master`. Do not edit app/.

## CLI
`overnight_loop.py --root ~/rag-project --experiments experiments.json --out ~/rag-project/probe/loop --max-hours 11 [--dry-run] [--resume]`
`--dry-run` uses the fake Ops and prints the plan. State lives in `<out>/state.json` (written atomically after every step), heartbeat in
`<out>/heartbeat` (ISO time, rewritten at least every 60 s while waiting), human log appended to `<out>/loop.log` and a results table to `<out>/results.md`.

## experiments.json
`{"baseline_env": {...}, "experiments": [{"name": "E1-async3", "env": {"MAX_ASYNC": "3", "LLM_MODEL_MAX_ASYNC": "3"}, "reingest": true|false, "probe_concurrency": 1}, ...]}`
Experiments run in file order. An experiment's env is applied ON TOP of baseline_env (not on top of the previous experiment). `reingest:false` means
only the probes run (query-side change). Only keys in this whitelist may be changed: MAX_ASYNC, LLM_MODEL_MAX_ASYNC, MAX_PARALLEL_INSERT, MAX_GLEANING, CHUNK_SIZE,
CHUNK_OVERLAP_SIZE, TOP_K, CHUNK_TOP_K, MAX_ENTITY_TOKENS, MAX_RELATION_TOKENS, MAX_TOTAL_TOKENS, ENTITY_TYPES, SUMMARY_MAX_TOKENS,
FORCE_LLM_SUMMARY_ON_MERGE, RELATED_CHUNK_NUMBER, KG_CHUNK_PICK_METHOD, RERANK_BY_DEFAULT; anything else -> refuse the file with a clear error. Never touch
LLM_*, EMBEDDING_*, *_API_KEY, LIGHTRAG_API_KEY.

## Per experiment
1. Apply env: back up `<root>/.env` once per run to `<out>/env.original` (chmod 600), then rewrite ONLY the whitelisted lines in `<root>/.env` (add if absent), all other lines
   byte-identical. Log keys and values for whitelisted keys only, never any other line.
2. `docker compose -f <root>/compose.yml up -d --no-deps lightrag-server` (cwd=<root>); wait for GET http://localhost:9621/health `status=="healthy"` (timeout 5 min).
   Verify with `docker exec lightrag-server env` that each changed key has the new value; if not -> experiment error.
3. If `reingest`: the worker must be stopped (check `docker ps`; if running -> abort with fatal error, do not stop it yourself). Clear LightRAG via
   `DELETE /documents` (header `X-API-Key` from `<root>/.env` LIGHTRAG_API_KEY, never logged), poll `/documents/status_counts` until all 0 (timeout 10 min). Then insert each
   transcript listed in `<root>/probe/loop_corpus.json` (a JSON list of file paths) one at a time via `POST /documents/text` {"text": <file text>, "file_source": <absolute path>};
   poll `/documents/track_status/<track_id>` every 30 s until processed; per-doc timeout 90 min; record seconds/doc. Failed doc -> retry once, then experiment error.
4. Run probes: for each line of `<root>/probe/solvay-dev-v1.jsonl` then `solvay-heldout-v1.jsonl` (fields: id, question, history?, expected_answer, evidence, type):
   POST http://127.0.0.1:8020/api/chat JSON {"model":"lightrag:latest","stream":false,"messages":[...history turns as {role,content}..., {"role":"user","content":question}]}
   (no auth header; answer = response["message"]["content"]); 900 s timeout, 1 retry on error; `probe_concurrency` threads (default 1). Save answer, latency, error.
5. Judge each answer with an independent LLM: POST http://192.168.1.190:1234/v1/chat/completions, header `Authorization: Bearer 1234`, model `qwen3.6-35b`, temperature 0,
   max_tokens 400, stream false. Judge prompt (put it in the script as a constant) gives question, expected_answer, evidence quote, and the system's answer and asks for ONLY
   JSON {"verdict": "correct|partial|wrong|hallucinated|refused_ok|refused_wrong", "reason": "<one sentence>"}. Rules in the prompt: judge against evidence/expected_answer, never
   against outside knowledge; for questions whose expected_answer is "not in the documents", a clear statement that the information is not available = refused_ok, anything
   invented = hallucinated. Parse robustly (extract the first {...}); unparsable -> retry once, then verdict "judge_error" (excluded from rates, counted separately).
6. Score per set: correct%, (correct+0.5*partial)%, hallucination%, refusal accuracy on unanswerables, by type, latency p50/p95, judge_error count. Write
   `<out>/<name>/answers.jsonl`, `verdicts.jsonl`, `summary.json`; append a row to results.md (dev and heldout columns). Per-question DETAIL for the heldout set stays only in the files.

## Stop / safety rules
- SUCCESS: heldout correct% >= 90 AND hallucination% < 5 AND refusal accuracy >= 80 -> mark state "success", skip remaining experiments, go to finalise.
- Error handling: any step failing twice -> mark the experiment "error" with the reason and continue with the next experiment. If /health or sigma
  (`GET http://192.168.1.195:1234/v1/models` with Bearer 1234) is down, wait/retry up to 10 min before counting it as a failure. 3 consecutive experiment errors -> stop.
- Time: stop starting new experiments after --max-hours (an experiment already running may finish; hard abort at max-hours + 1h).
- FINALISE (always, in a try/finally, also on SIGINT/SIGTERM and on any exception): restore the original env values for every key the run changed (from env.original),
  recreate lightrag-server, wait healthy. Then the data is whatever the LAST reingest produced - record that in results.md ("current corpus built by: <experiment>").
  If the best experiment (highest dev (correct+0.5*partial)%, tie -> lower latency) used different env than baseline, DO NOT apply it; just print the exact env diff recommendation in results.md.
- Never touch: compose.yml, the worker, state DB, source_docs, uploads, any container other than lightrag-server. Never print API keys.
- Resume: with --resume, skip experiments already "done"/"error" in state.json; an experiment interrupted mid-way restarts from step 1.

## Acceptance
- Tests cover: env whitelist/refusal, byte-preserving env rewrite and restore, experiment ordering, resume, stop-on-success, error-continue, 3-consecutive-errors stop,
  judge JSON parsing (clean, fenced, garbage), score maths, finalise-on-exception, worker-running abort, --dry-run output. All pass; full suite passes.
- Report back: commit hash on packet-28-loop, test counts, and any spec ambiguities you resolved (list them).
