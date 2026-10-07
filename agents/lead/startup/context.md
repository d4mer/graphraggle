# Handoff — lead

Last updated: 2026-10-07 (rig bootstrap, no work done yet)

## Goal

Take the GraphRAG implementation repo forward end to end, using the worker seat
for implementation and testing. Keep `master` on GitHub green at all times.

## Current state

- Fresh clone on sigma at `~/work/graphraggle`, branch `master`, HEAD `05ff985`.
- `.venv/` created with fastapi, pydantic, pydantic-settings, httpx, aiosqlite,
  aiofiles, pytest. Full suite passes: **273 passed in ~0.3s**.
- No work items started yet. No commits made yet.

## In flight

None. qitem-20261007103353-e2fafb7b (worker dedupe hardening) closed done and pushed 2026-10-07; suite 287 passed.

## Decisions and constraints

- Lead delegates all code changes to `dev-worker@graphraggle`; lead verifies and
  pushes.
- Test command is `.venv/bin/python -m pytest tests/ -q`. Tests are offline and
  fast; there is no reason to skip them.
- `TODO.md` near-term list is the default backlog: worker dedupe hardening,
  SQLite status transitions / stale `track_id`, upload vs source-doc ingestion
  semantics, end-to-end ingestion path validation (PDF/DOCX/PPTX/XLSX),
  generate-document prompt templates.
- Live services (LightRAG, Open WebUI) are NOT running on sigma. Only the
  offline test suite is available here. Do not brief the worker on anything that
  needs live services.

## Next three actions

1. Pick TODO.md near-term #2 (SQLite status transitions / stale `track_id` replacement); read app/state_store.py + worker stale-track paths, write a tight brief, create queue item.
2. Verify worker result (diff + full suite; re-run new tests against old code to confirm they fail pre-fix).
3. Commit and push to `origin master`.
