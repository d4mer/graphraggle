"""Test-session environment defaults.

Why this exists
---------------
app/config.py instantiates ``settings = Settings()`` at import time and six of
its fields are required with no default. Importing an app module therefore
raises a pydantic ValidationError in a bare environment, and pytest fails at
*collection* (test_keywords, test_observability, test_ollama_bridge,
test_rewrite) before a single test runs.

pytest imports a conftest before any test module in its directory, so filling
in fake values here makes the documented command work from a clean checkout:

    .venv/bin/python -m pytest tests/ -q

Rules this file keeps to
------------------------
- ``setdefault`` only: a value exported by the shell, CI, or a test itself
  always wins. Nothing here can mask a real configuration.
- No app/ code changed and no Settings validation weakened; the fields stay
  required and still fail for a real process that forgets them.
- Paths point into a throwaway temp dir, so a test that reaches the ambient
  environment can never touch the real source_docs/, uploads/ or state.db.
- The base URL is a stub. Nothing in the suite connects anywhere: every
  upstream call is mocked.
- Tests that do ``patch.dict(os.environ, ..., clear=True)`` and re-read
  Settings are unaffected: they replace the environment for their own scope.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Kept at module scope for the whole session; TemporaryDirectory removes it at
# interpreter exit.
_TMP_DIR = tempfile.TemporaryDirectory(prefix="graphraggle-pytest-")
_TMP = Path(_TMP_DIR.name)

# Values are fake on purpose. They satisfy Settings' required fields; they are
# never used to reach anything real.
_REQUIRED_ENV = {
    "RAG_API_KEY": "test-rag-key",
    "LIGHTRAG_INTERNAL_API_KEY": "test-internal-key",
    "LIGHTRAG_BASE_URL": "http://example:9621",
    "SOURCE_DOCS_DIR": str(_TMP / "source_docs"),
    "UPLOADS_DIR": str(_TMP / "uploads"),
    "STATE_DB_PATH": str(_TMP / "state.db"),
}

# Directories exist so code that lists or writes into them sees a real, empty
# temp location rather than a missing path.
for _sub in ("source_docs", "uploads"):
    (_TMP / _sub).mkdir(parents=True, exist_ok=True)

for _key, _value in _REQUIRED_ENV.items():
    os.environ.setdefault(_key, _value)
