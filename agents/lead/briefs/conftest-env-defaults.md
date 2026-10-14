# Brief: tests/conftest.py env defaults so bare pytest works

## Problem
`.venv/bin/python -m pytest tests/ -q` on a clean checkout fails at collection
in tests/test_keywords.py, test_observability.py, test_ollama_bridge.py and
test_rewrite.py: they import app modules that construct `Settings` at import
time, and six env vars are required with no defaults (pydantic ValidationError):
RAG_API_KEY, LIGHTRAG_INTERNAL_API_KEY, LIGHTRAG_BASE_URL, SOURCE_DOCS_DIR,
UPLOADS_DIR, STATE_DB_PATH.

With dummy values exported, the whole suite passes (467 passed), so this is
purely a missing test-environment setup, not a code regression.

## Goal
The documented command works with no exported env:
`.venv/bin/python -m pytest tests/ -q` -> all pass.

## Approach (your call on details)
- Add `tests/conftest.py` that sets those env vars via `os.environ.setdefault`
  at import time (before any app import), pointing paths at a temp dir
  (e.g. tempfile.mkdtemp) so nothing touches real dirs or DBs.
- setdefault only: must not override values a test or the shell provides.
- Do not change app/ code and do not weaken Settings validation.
- Existing tests that use `patch.dict(os.environ, ..., clear=True)` must keep
  passing unchanged.

## Done when
1. Bare `pytest tests/ -q` with a clean env (`env -i PATH=$PATH ...` or unset the
   six vars) passes the full suite, 467 tests.
2. Same command with the six vars exported still passes.
3. Confirm the 4 files fail at collection without your conftest (stash it) so
   we know the fix is what made the difference.
4. No live services, no corpus/LightRAG content access.

Hand back: diff summary and the two pytest results. Do not commit; lead commits.
