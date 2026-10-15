# Worker context — graphraggle dev-worker

Last rewrite: after delivering qitem-20261015133902-cf21b1c5 (multi-stack loop flags).
Earlier per-item history is in git (`git log --oneline` on `master` and `chunking`); this file
keeps only live state, load-bearing facts and open threads.

## Current state — nothing owned, waiting on the lead

Queue: empty after this handoff. Watchdog `parked-owner-consumer`
(job 01M4AYEG0WK885GGJPDGRNVGD5) is parked on the handoff; if it fires with no new row,
report honestly and park again rather than inventing work.

**Uncommitted work sits in the worktree `/home/maverick/work/graphraggle-chunking`**
(branch `chunking`, base `b7fe10f`). The main checkout `/home/maverick/work/graphraggle` is on
`master` (4741cab, 2 docs commits ahead of origin) — never edit chunking work there.
Shared venv: `/home/maverick/work/graphraggle/.venv`. I never `git push`; the lead commits.

Worktree diff left for the lead (uncommitted):
- `scripts/overnight_loop.py` +101/-18 — `--worker-container NAME` (default production
  `rag-ingest-worker`; `none` skips the guard), `worker_guard_refusal()` refuses `none` when the
  RESOLVED LightRAG URL is production, `--compose-file`/`--compose-project` threaded through
  every `docker compose` call, `--lightrag-service` (default = container name) added because the
  loop used the container name as the compose service name.
- `tests/conftest.py` +43 — patches `socket.socket.connect{,_ex}` to raise on ports 9621/8020.
- `tests/test_overnight_loop.py` +208 — 16 new tests; fixes the pre-existing undefined `Any`.
- NEW `tests/test_conftest_socket_guard.py` — 4 tests proving the guard.
- `docs/ops/loop/experiments-chunking.json` — `_readme` second-stack run line + the exact
  warning sentence.

Gates: `env -i PATH=$PATH HOME=$HOME /home/maverick/work/graphraggle/.venv/bin/python -m pytest
tests/ -q` in the worktree → **776 passed, 2 warnings, 16 subtests** (baseline b7fe10f = 760).
pyflakes clean (`PYTHONPATH=/tmp/pyflakes_lib ... -m pyflakes`, installed with
`-i https://pypi.org/simple/` — the corporate index has no flakes8/pyflakes).
Controls: revert `scripts/` → 9 failed / 164 passed; revert `tests/conftest.py` → the guard test
cannot import.

## Load-bearing rules I learned the hard way

1. **A test must never open a socket to production.** Patching module constants only works if
   the code reads them at call time; a default frozen at import time aimed the overnight-loop
   suite at localhost:9621 / 127.0.0.1:8020 for hours. Hence the conftest guard; keep new
   network-y drivers resolved at construction (`Target.__post_init__` with `None` sentinels).
2. Prove controls by reverting `scripts/` (and `app/`), not the tests.
3. Never enable `COMPANY_AWARE_DEDUPE` without a fresh cross-company leak probe — the
   `cross_company_not_indexed` guard is dead code in `app/graphraggle/ingest.py`
   (`_find_duplicate` runs before the branch).
4. The overnight loop's `--reingest` wipes the LightRAG store; `--allow-production` is required
   when the *resolved* URL is production, and `--dry-run` warns instead of refusing.
5. `docs/` files are hand-maintained markdown with an index + a "Last reviewed" line: bump the
   month, add one line, don't restructure.
6. `app/config.py` builds `settings` at import; tests rely on `tests/conftest.py` env defaults.

## LightRAG 1.5.7 chunker reference (Hermes-confirmed, from the image)

Shared: `chunk_token_size`. `fixed_token` adds `chunk_overlap_token_size`, `split_by_character(_only)`;
`recursive_character` adds overlap + `separators` (list[str], ≤64 items, ≤256 chars each);
`paragraph_semantic` adds overlap + `drop_references`; `semantic_vector` has **no overlap key** and
adds `breakpoint_threshold_type` (sentence|paragraph|semantic_similarity), `breakpoint_threshold_amount`,
`buffer_size` (≤10). `sentence_split_regex` is env-only — rejected by the API. Defaults the running
production container reports: fixed_token, 1200/100. Strategy values include `custom`.

## Chunking program state (branch `chunking`)

Committed by the lead: `b535792` transcript prep (`app/transcript_prep.py`, config flags,
worker + loop `transcript_prep` block), `b7fe10f` real `(Label)` dialect + per-request chunking
arms (`Target`, `CHUNKING_PARAM_KEYS`, `chunking_param_problems()`, `production_wipe_refusal()`,
doc-position logs, `docs/ops/loop/experiments-chunking.json` with C0/P1/C2/C1/P2 — C0 and P1 are
the mandatory A/B). The program still needs **real transcript labels**:
`grep -cE '^\([A-Z][A-Za-z0-9 ._-]{1,24}\)' source_docs/Transcripts/*/*.txt`.

## Standing out-of-brief findings (lead's call)

1. Stale podman/ports facts in `docs/01,02,03,04,06,09,10,12`,
   `docs/quick-reference.md`, `docs/README.md`, `docs/lightrag-webui-guide.md`,
   `docs/ops/rollback-and-containment.md`, `scripts/install_rag_stack.sh`.
2. `docs/07-openwebui-guide.md:180` says query mode defaults to `hybrid`; the code
   default is `mix`.
3. Repo `compose.yml` has no chromadb service though production runs `rag-chromadb`.
4. HyperQwen endpoints use `192.168.1.195`; docs say the oMLX is `192.168.1.190:1234`.
5. Docs still say "binary docs ingest through `/documents/upload`" — stale for
   `.pptx` (packet-27) and for `source_docs` files (packet-25).
6. `run()`'s generic `except Exception` turns any internal bug into run status
   `fatal` with exit 2 and only a `self.log` line (invisible when `print_fn` is
   silenced). It hid a test-harness `NameError` for me twice.

### Added while on the chunking branch (2026-10-15)

7. `POST /documents/text` failures map to **409**, not 410: `app/api.py` catches
   `HTTPException` before `except Exception`, so the `status_code=410` branch in
   `_handle_lightrag_error` is unreachable. One `raise HTTPException(410, ...)` remains at
   `app/api.py:1061`.
8. `/upload` calls `rag.ainsert(rag.scan_env)` with no `chunk_*` kwargs → binary docs keep the
   container's chunking even under a chunking arm.
9. `app/api.py:_apply_ollama_bridge_restart` shells `docker restart GATEWAY_CONTAINER` and
   `app/config.py` has no env for it; the loop recreates the gateway instead. A bridge
   `restart: true` arm against a second stack is broken until that reads an env var.
10. `docs/04-troubleshooting.md` curl examples use `localhost:8000` / `localhost:8020/v1`;
    production hosts the gateway on 8020 (8000 is the dev mapping).
11. No test covers the bearer guard on `GET /ingest/status` (added in `3284cc5`, 2026-05-05).
12. `chunking_param_problems()` cannot validate `strategy: custom` keys (unreported contract) and
    never sees `sentence_split_regex` (env-only). LightRAG's 422 is the backstop either way.
13. `--dev-probes` existence is checked before the target gates, so a bad-target refusal often
    surfaces as the probe-file message first.
14. `--lightrag-service` was added beyond the flag list in qitem-…cf21b1c5 item 2 (the loop used
    the LightRAG **container** name as the compose **service** name). Drop the hunk if unwanted.
15. `FakeOps` in the loop tests grew `names` (scripted `docker ps --format {{.Names}}`) and
    `env_file` (which `.env` the fake reads/snapshots).
