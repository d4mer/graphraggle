#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/rag-project}"
APP_IMAGE="${APP_IMAGE:-local/rag-gateway:latest}"
FORCE="${FORCE:-0}"

require_env() {
  local name="$1"
  if [[ -z "${!name:-}" ]]; then
    printf "Missing required env var: %s\n" "$name" >&2
    exit 1
  fi
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    printf "Required command not found: %s\n" "$1" >&2
    exit 1
  }
}

check_http() {
  local url="$1"
  local header_name="$2"
  local header_value="$3"
  curl -fsS -H "${header_name}: ${header_value}" "$url" >/dev/null
}

require_env OMLX_API_KEY
require_env RAG_API_KEY
require_env LIGHTRAG_API_KEY

need_cmd podman
need_cmd curl
need_cmd python3

if ! podman compose version >/dev/null 2>&1; then
  printf "podman compose is required\n" >&2
  exit 1
fi

if [[ -e "$PROJECT_DIR" && "$FORCE" != "1" ]]; then
  if [[ -n "$(ls -A "$PROJECT_DIR" 2>/dev/null || true)" ]]; then
    printf "Project directory exists and is not empty: %s\n" "$PROJECT_DIR" >&2
    printf "Re-run with FORCE=1 to overwrite scaffold files.\n" >&2
    exit 1
  fi
fi

mkdir -p \
  "$PROJECT_DIR/app" \
  "$PROJECT_DIR/lightrag_store" \
  "$PROJECT_DIR/source_docs" \
  "$PROJECT_DIR/uploads" \
  "$PROJECT_DIR/logs" \
  "$PROJECT_DIR/state"

printf "Testing oMLX endpoints...\n"

curl -fsS -X POST "http://192.168.1.190:1234/v1/chat/completions" \
  -H "Authorization: Bearer $OMLX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"qwen3.6-35b",
    "messages":[{"role":"user","content":"Reply with OK"}],
    "max_tokens":8
  }' >/dev/null

curl -fsS -X POST "http://192.168.1.180:1234/v1/embeddings" \
  -H "Authorization: Bearer $OMLX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"mxbai-embed-large-v1","input":"smoke test"}' >/dev/null

curl -fsS -X POST "http://192.168.1.180:1234/v1/rerank" \
  -H "Authorization: Bearer $OMLX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"jina-reranker-v3-mlx","query":"smoke","documents":["alpha","beta"],"top_n":1,"return_documents":true}' >/dev/null

python3 - <<'PY' "$PROJECT_DIR" "$OMLX_API_KEY" "$RAG_API_KEY" "$LIGHTRAG_API_KEY" "$APP_IMAGE"
from pathlib import Path
import sys

project_dir = Path(sys.argv[1])
omlx_api_key = sys.argv[2]
rag_api_key = sys.argv[3]
lightrag_api_key = sys.argv[4]
app_image = sys.argv[5]
app_dir = project_dir / "app"

files = {
    project_dir / ".env": f"""OMLX_API_KEY={omlx_api_key}
RAG_API_KEY={rag_api_key}
LIGHTRAG_API_KEY={lightrag_api_key}

LIGHTRAG_BASE_URL=http://lightrag-server:9621
LIGHTRAG_INTERNAL_API_KEY={lightrag_api_key}

SOURCE_DOCS_DIR=/app/source_docs
UPLOADS_DIR=/app/uploads
STATE_DB_PATH=/app/state/ingest.db

INGEST_SCAN_INTERVAL_SECONDS=30
INGEST_MAX_RETRIES=3
REQUEST_TIMEOUT_SECONDS=300

# Retrieval defaults. "mix" returns KG context AND raw source chunks;
# "hybrid" weights context toward entity and relation descriptions; "mix"
# also adds directly retrieved chunks.
RETRIEVAL_MODE_DEFAULT=mix
BRIDGE_TOP_K=40
# GATEWAY_ prefix: bare CHUNK_TOP_K is LightRAG's own variable (default 20)
# and all services share this .env, so the gateway alias must not collide.
GATEWAY_CHUNK_TOP_K=16
CITATION_TOP_K=12
BRIDGE_HISTORY_TURNS=3
BRIDGE_TASK_SHORTCIRCUIT=True
# Cap on the OpenWebUI task prompt the bridge forwards (~6k tokens); 0 disables.
BRIDGE_TASK_MAX_CHARS=24000
# Leave false until the synthesis prompt carries real edge content (Stage 6).
GRAPH_SYNTHESIS_REPLACE_ANSWER=False

PORT=9621
WORKERS=2
MAX_ASYNC=4
MAX_PARALLEL_INSERT=2
TIMEOUT=150

# Context budget. These values equal LightRAG v1.4.15's defaults and are
# pinned here to document intent.
MAX_ENTITY_TOKENS=6000
MAX_RELATION_TOKENS=8000
MAX_TOTAL_TOKENS=30000

LIGHTRAG_API_KEY={lightrag_api_key}
WHITELIST_PATHS=/health

LLM_BINDING=openai
LLM_BINDING_HOST=http://192.168.1.190:1234/v1
LLM_BINDING_API_KEY={omlx_api_key}
LLM_MODEL=qwen3.6-35b

EMBEDDING_BINDING=openai
EMBEDDING_BINDING_HOST=http://192.168.1.180:1234/v1
EMBEDDING_BINDING_API_KEY={omlx_api_key}
EMBEDDING_MODEL=mxbai-embed-large-v1
EMBEDDING_DIM=1024

RERANK_BINDING=jina
RERANK_BINDING_HOST=http://192.168.1.180:1234/v1/rerank
RERANK_BINDING_API_KEY={omlx_api_key}
RERANK_MODEL=jina-reranker-v3-mlx
RERANK_BY_DEFAULT=True
""",
    project_dir / "requirements.txt": """fastapi==0.115.12
uvicorn[standard]==0.34.2
httpx==0.28.1
pydantic==2.11.4
pydantic-settings==2.8.1
python-multipart==0.0.20
aiofiles==24.1.0
aiosqlite==0.20.0
""",
    project_dir / "Containerfile": """FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    libmagic1 \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app /app/app

ENV PYTHONUNBUFFERED=1

CMD ["uvicorn", "app.api:app", "--host", "0.0.0.0", "--port", "8000"]
""",
    project_dir / "compose.yml": f"""services:
  lightrag-server:
    image: ghcr.io/hkuds/lightrag:v1.4.15
    container_name: lightrag-server
    env_file:
      - .env
    command:
      - --host
      - 0.0.0.0
      - --port
      - "9621"
      - --working-dir
      - /data/lightrag_store
      - --input-dir
      - /data/source_docs
    volumes:
      - ./lightrag_store:/data/lightrag_store
      - ./source_docs:/data/source_docs
      - ./logs:/data/logs
    restart: unless-stopped

  gateway-api:
      build:
        context: .
        dockerfile: Containerfile
      image: local/rag-gateway:latest
      pull_policy: never
      container_name: rag-gateway-api
      env_file:
        - .env
      volumes:
        - ./source_docs:/app/source_docs
        - ./uploads:/app/uploads
        - ./logs:/app/logs
        - ./state:/app/state
      ports:
        - "8000:8000"
      depends_on:
        - lightrag-server
      restart: unless-stopped

  ingest-worker:
      build:
        context: .
        dockerfile: Containerfile
      image: local/rag-gateway:latest
      pull_policy: never
      container_name: rag-ingest-worker
      env_file:
        - .env
      command: ["python", "-m", "app.worker"]
      volumes:
        - ./source_docs:/app/source_docs
        - ./uploads:/app/uploads
        - ./logs:/app/logs
        - ./state:/app/state
      depends_on:
        - lightrag-server
      restart: unless-stopped

  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    container_name: open-webui
    environment:
      WEBUI_AUTH: "False"
      ENABLE_OLLAMA_API: "True"
      OLLAMA_BASE_URLS: '["http://gateway-api:8000"]'
    ports:
      - "3000:8080"
    depends_on:
      - gateway-api
    volumes:
      - open-webui-data:/app/backend/data
    restart: unless-stopped

volumes:
  open-webui-data:
""",
    app_dir / "__init__.py": None,
    app_dir / "config.py": None,
    app_dir / "models.py": """from typing import Any, Literal

from pydantic import BaseModel


class Envelope(BaseModel):
    ok: bool
    data: Any | None
    meta: dict[str, Any]
    error: dict[str, Any] | None = None


class QueryRequest(BaseModel):
    query: str
    company: str | None = None
    top_k: int = 12
    mode: Literal["mix", "hybrid", "local", "global", "naive", "bypass"] = "mix"
    thread_id: str | None = None


class GenerateDocumentRequest(BaseModel):
    query: str
    document_type: Literal["summary", "memo", "report", "proposal", "policy", "brief", "draft"]
    company: str | None = None
    style: str | None = None
    thread_id: str | None = None


class ReindexRequest(BaseModel):
    path: str | None = None
    force: bool = True
""",
    app_dir / "auth.py": None,
    app_dir / "lightrag_client.py": None,
    app_dir / "state_store.py": """from __future__ import annotations

import aiosqlite
from pathlib import Path
from .config import settings


SCHEMA = '''
CREATE TABLE IF NOT EXISTS documents (
    document_id TEXT PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    source_type TEXT NOT NULL,
    company TEXT,
    filename TEXT NOT NULL,
    original_filename TEXT,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    validation_state TEXT NOT NULL DEFAULT 'not_run',
    error_stage TEXT,
    error_code TEXT,
    error_message TEXT,
    error_detail TEXT,
    query_ready INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    track_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    content_hash TEXT,
    detected_mime TEXT,
    detected_encoding TEXT,
    byte_size INTEGER,
    page_count INTEGER,
    source_uri TEXT,
    supersedes_document_id TEXT,
    ingested_at TEXT,
    warnings_json TEXT
);
'''

MIGRATIONS: dict[str, str] = {
    "validation_state": "ALTER TABLE documents ADD COLUMN validation_state TEXT NOT NULL DEFAULT 'not_run'",
    "error_stage": "ALTER TABLE documents ADD COLUMN error_stage TEXT",
    "error_code": "ALTER TABLE documents ADD COLUMN error_code TEXT",
    "error_message": "ALTER TABLE documents ADD COLUMN error_message TEXT",
    "error_detail": "ALTER TABLE documents ADD COLUMN error_detail TEXT",
    "query_ready": "ALTER TABLE documents ADD COLUMN query_ready INTEGER NOT NULL DEFAULT 0",
    "content_hash": "ALTER TABLE documents ADD COLUMN content_hash TEXT",
    "detected_mime": "ALTER TABLE documents ADD COLUMN detected_mime TEXT",
    "detected_encoding": "ALTER TABLE documents ADD COLUMN detected_encoding TEXT",
    "byte_size": "ALTER TABLE documents ADD COLUMN byte_size INTEGER",
    "page_count": "ALTER TABLE documents ADD COLUMN page_count INTEGER",
    "source_uri": "ALTER TABLE documents ADD COLUMN source_uri TEXT",
    "supersedes_document_id": "ALTER TABLE documents ADD COLUMN supersedes_document_id TEXT",
    "ingested_at": "ALTER TABLE documents ADD COLUMN ingested_at TEXT",
    "warnings_json": "ALTER TABLE documents ADD COLUMN warnings_json TEXT",
    "original_filename": "ALTER TABLE documents ADD COLUMN original_filename TEXT",
}

UNSET = object()


async def init_db() -> None:
    Path(settings.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.executescript(SCHEMA)
        cur = await db.execute("PRAGMA table_info(documents)")
        rows = await cur.fetchall()
        existing = {row[1] for row in rows}
        for column, statement in MIGRATIONS.items():
            if column not in existing:
                await db.execute(statement)
        await db.commit()


async def upsert_document(
    document_id: str,
    path: str,
    source_type: str,
    company: str | None,
    filename: str,
    original_filename: str | None,
    sha256: str,
    status: str,
    track_id: str | None,
    last_error: str | None = None,
    validation_state: str = "not_run",
    error_stage: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    error_detail: str | None = None,
    query_ready: bool = False,
    content_hash: str | None = None,
    detected_mime: str | None = None,
    detected_encoding: str | None = None,
    byte_size: int | None = None,
    page_count: int | None = None,
    source_uri: str | None = None,
    supersedes_document_id: str | None = None,
    ingested_at: str | None = None,
    warnings_json: str | None = None,
) -> None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            '''
            INSERT INTO documents (
              document_id, path, source_type, company, filename, original_filename, sha256, status,
              validation_state, error_stage, error_code, error_message, error_detail,
              query_ready, retry_count, track_id, last_error, created_at, updated_at,
              content_hash, detected_mime, detected_encoding, byte_size, page_count,
              source_uri, supersedes_document_id, ingested_at, warnings_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, datetime('now'), datetime('now'),
              ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
              company=excluded.company,
              filename=excluded.filename,
              original_filename=excluded.original_filename,
              sha256=excluded.sha256,
              status=excluded.status,
              validation_state=excluded.validation_state,
              error_stage=excluded.error_stage,
              error_code=excluded.error_code,
              error_message=excluded.error_message,
              error_detail=excluded.error_detail,
              query_ready=excluded.query_ready,
              track_id=excluded.track_id,
              last_error=excluded.last_error,
              content_hash=excluded.content_hash,
              detected_mime=excluded.detected_mime,
              detected_encoding=excluded.detected_encoding,
              byte_size=excluded.byte_size,
              page_count=excluded.page_count,
              source_uri=excluded.source_uri,
              supersedes_document_id=excluded.supersedes_document_id,
              ingested_at=excluded.ingested_at,
              warnings_json=excluded.warnings_json,
              updated_at=datetime('now')
            ''',
            (
                document_id,
                path,
                source_type,
                company,
                filename,
                original_filename,
                sha256,
                status,
                validation_state,
                error_stage,
                error_code,
                error_message,
                error_detail,
                int(query_ready),
                track_id,
                last_error,
                content_hash,
                detected_mime,
                detected_encoding,
                byte_size,
                page_count,
                source_uri,
                supersedes_document_id,
                ingested_at,
                warnings_json,
            ),
        )
        await db.commit()


async def update_document_state(
    path: str,
    *,
    sha256: str | object = UNSET,
    status: str | object = UNSET,
    validation_state: str | object = UNSET,
    error_stage: str | object = UNSET,
    error_code: str | object = UNSET,
    error_message: str | object = UNSET,
    error_detail: str | object = UNSET,
    last_error: str | object = UNSET,
    track_id: str | object = UNSET,
    query_ready: bool | object = UNSET,
    content_hash: str | object = UNSET,
    detected_mime: str | object = UNSET,
    detected_encoding: str | object = UNSET,
    byte_size: int | object = UNSET,
    page_count: int | object = UNSET,
    source_uri: str | object = UNSET,
    supersedes_document_id: str | object = UNSET,
    ingested_at: str | object = UNSET,
    warnings_json: str | object = UNSET,
    original_filename: str | object = UNSET,
    retry_count: int | object = UNSET,
) -> None:
    assignments: list[str] = []
    values: list[object] = []
    updates = {
        "sha256": sha256,
        "status": status,
        "validation_state": validation_state,
        "error_stage": error_stage,
        "error_code": error_code,
        "error_message": error_message,
        "error_detail": error_detail,
        "last_error": last_error,
        "track_id": track_id,
        "content_hash": content_hash,
        "detected_mime": detected_mime,
        "detected_encoding": detected_encoding,
        "byte_size": byte_size,
        "page_count": page_count,
        "source_uri": source_uri,
        "supersedes_document_id": supersedes_document_id,
        "warnings_json": warnings_json,
        "original_filename": original_filename,
        "retry_count": retry_count,
    }
    for column, value in updates.items():
        if value is not UNSET:
            assignments.append(f"{column}=?")
            values.append(value)
    if query_ready is not UNSET:
        assignments.append("query_ready=?")
        values.append(int(query_ready))
    if ingested_at is not UNSET:
        if ingested_at is None:
            assignments.append("ingested_at=NULL")
        else:
            assignments.append("ingested_at=datetime('now')")
    if not assignments:
        return
    assignments.append("updated_at=datetime('now')")
    values.append(path)
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(f"UPDATE documents SET {', '.join(assignments)} WHERE path=?", values)
        await db.commit()


async def update_status(path: str, status: str, last_error: str | None = None) -> None:
    await update_document_state(path, status=status, last_error=last_error, query_ready=(status == "ingested"))


async def increment_retry(path: str) -> int:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            "UPDATE documents SET retry_count=retry_count+1, updated_at=datetime('now') WHERE path=?",
            (path,),
        )
        await db.commit()
        cur = await db.execute("SELECT retry_count FROM documents WHERE path=?", (path,))
        row = await cur.fetchone()
        return row[0] if row else 0


async def get_document_by_path(path: str) -> dict | None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM documents WHERE path=?", (path,))
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_document_by_sha256(sha256: str) -> dict | None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM documents WHERE sha256=? ORDER BY updated_at DESC LIMIT 1",
            (sha256,),
        )
        row = await cur.fetchone()
        return dict(row) if row else None


async def get_documents_by_sha256(sha256: str) -> list[dict]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT * FROM documents WHERE sha256=? ORDER BY updated_at DESC",
            (sha256,),
        )
        rows = await cur.fetchall()
        return [dict(row) for row in rows]


async def list_documents() -> list[dict]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM documents ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_summary() -> dict[str, dict[str, int]]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        status_rows = await (await db.execute("SELECT status, COUNT(*) FROM documents GROUP BY status")).fetchall()
        validation_rows = await (await db.execute("SELECT validation_state, COUNT(*) FROM documents GROUP BY validation_state")).fetchall()
        readiness_rows = await (await db.execute("SELECT query_ready, COUNT(*) FROM documents GROUP BY query_ready")).fetchall()
        error_stage_rows = await (await db.execute(
            "SELECT error_stage, COUNT(*) FROM documents GROUP BY error_stage"
        )).fetchall()
        company_rows = await (await db.execute(
            "SELECT company, COUNT(*) FROM documents GROUP BY company"
        )).fetchall()
        return {
            "by_status": {row[0]: row[1] for row in status_rows if row[0] is not None},
            "by_validation_state": {row[0]: row[1] for row in validation_rows if row[0] is not None},
            "by_query_ready": {"true" if row[0] else "false": row[1] for row in readiness_rows},
            "by_error_stage": {row[0]: row[1] for row in error_stage_rows if row[0] is not None},
            "by_company": {row[0]: row[1] for row in company_rows if row[0] is not None},
        }
""",
    app_dir / "validation.py": """from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

TEXT_LIKE_EXTENSIONS: frozenset[str] = frozenset(
    {".txt", ".md", ".html", ".htm", ".json", ".csv"}
)

BINARY_OFFICE_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".pptx", ".xlsx"}
)

SUPPORTED_EXTENSIONS: frozenset[str] = (
    TEXT_LIKE_EXTENSIONS | BINARY_OFFICE_EXTENSIONS
)

AUTO_SPLIT_THRESHOLD: int = 500_000

WARN_SIZE_THRESHOLD: int = 100_000


class ValidationVerdict:

    __slots__ = ("verdict", "meta")

    def __init__(self, verdict: str, meta: dict[str, Any] | None = None) -> None:
        self.verdict = verdict
        self.meta = meta or {}

    def __repr__(self) -> str:
        return f"ValidationVerdict(verdict={self.verdict!r}, meta={self.meta})"


def reset_seen_hashes() -> None:
    return None


def classify_file(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in TEXT_LIKE_EXTENSIONS:
        return "text_like"
    if ext in BINARY_OFFICE_EXTENSIONS:
        return "binary_office"
    return "unsupported"


def validate_upload(filename: str, file_size: int) -> ValidationVerdict:
    file_class = classify_file(filename)

    if file_class == "unsupported":
        return ValidationVerdict(
            "reject",
            {
                "error_code": "unsupported_extension",
                "file_class": file_class,
                "error_message": "File extension is not supported",
            },
        )

    if file_size == 0:
        return ValidationVerdict(
            "reject",
            {
                "error_code": "empty_file",
                "file_class": file_class,
                "error_message": "File is empty (0 bytes)",
            },
        )

    if file_class == "text_like" and file_size >= AUTO_SPLIT_THRESHOLD:
        return ValidationVerdict(
            "auto_split",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": AUTO_SPLIT_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes (>= {AUTO_SPLIT_THRESHOLD} "
                    "threshold); flagging for auto-split to avoid timeout"
                ),
            },
        )

    if file_class == "text_like" and file_size >= WARN_SIZE_THRESHOLD:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": WARN_SIZE_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes; may process slowly"
                ),
            },
        )

    return ValidationVerdict(
        "accept",
        {"file_class": file_class},
    )


def validate_file(
    path: Path,
    *,
    file_size: int | None = None,
    sha256: str | None = None,
    duplicate_of: dict[str, Any] | None = None,
) -> ValidationVerdict:
    if file_size is None:
        file_size = path.stat().st_size
    if sha256 is None:
        sha256 = _compute_sha256(path)

    file_class = classify_file(path.name)

    if file_class == "unsupported":
        return ValidationVerdict(
            "reject",
            {
                "error_code": "unsupported_extension",
                "file_class": file_class,
                "error_message": "File extension is not supported",
            },
        )

    if file_size == 0:
        return ValidationVerdict(
            "reject",
            {
                "error_code": "empty_file",
                "file_class": file_class,
                "error_message": "File is empty (0 bytes)",
            },
        )

    if duplicate_of is not None:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "duplicate_content",
                "file_class": file_class,
                "sha256": sha256,
                "duplicate_of": duplicate_of,
                "error_message": "Duplicate content detected (same SHA-256 as existing document)",
            },
        )

    if file_class == "text_like" and file_size >= AUTO_SPLIT_THRESHOLD:
        return ValidationVerdict(
            "auto_split",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": AUTO_SPLIT_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes (>= {AUTO_SPLIT_THRESHOLD} "
                    "threshold); flagging for auto-split to avoid timeout"
                ),
            },
        )

    if file_class == "text_like" and file_size >= WARN_SIZE_THRESHOLD:
        return ValidationVerdict(
            "warn",
            {
                "error_code": "large_text_file",
                "file_class": file_class,
                "file_size": file_size,
                "threshold": WARN_SIZE_THRESHOLD,
                "error_message": (
                    f"Text file is {file_size} bytes; may process slowly"
                ),
            },
        )

    if file_class == "text_like":
        raw = path.read_bytes()
        encoding = _check_encoding(raw)
        if encoding is not None and encoding not in ("utf-8", "utf-8-sig"):
            return ValidationVerdict(
                "warn",
                {
                    "error_code": "fallback_encoding",
                    "file_class": file_class,
                    "encoding": encoding,
                    "error_message": (
                        f"File decoded with fallback encoding {encoding}; "
                        "original encoding may not be UTF-8"
                    ),
                },
            )

    return ValidationVerdict(
        "accept",
        {"file_class": file_class, "sha256": sha256},
    )


_DECODE_ORDER: tuple[str, ...] = ("utf-8", "utf-8-sig", "cp1252", "latin-1")


def _check_encoding(raw: bytes) -> str | None:
    for encoding in _DECODE_ORDER:
        try:
            raw.decode(encoding)
            if encoding in ("utf-8", "utf-8-sig"):
                return None
            return encoding
        except UnicodeDecodeError:
            continue
    return "utf-8"


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
""",
    app_dir / "generation.py": None,
    app_dir / "api.py": """from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path

import aiofiles
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile

from .auth import require_bearer
from .config import settings
from .generation import generate_document
from .lightrag_client import client
from .models import Envelope, GenerateDocumentRequest, QueryRequest, ReindexRequest
from .state_store import get_document_by_sha256, get_summary, init_db, list_documents, upsert_document
from .validation import validate_upload

app = FastAPI(title="RAG Gateway", version="0.1.0")

FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9._-]+")


@app.on_event("startup")
async def startup() -> None:
    await init_db()


def ok(data):
    return Envelope(ok=True, data=data, meta={"request_id": str(uuid.uuid4())}, error=None)


def sanitize_original_name(filename: str) -> str:
    original = Path(filename).name
    stem = Path(original).stem or "upload"
    sanitized = FILENAME_SANITIZE_RE.sub("-", stem).strip(".-_")
    return sanitized or "upload"


def build_stored_filename(document_id: str, original_filename: str) -> str:
    suffix = Path(original_filename).suffix.lower()
    return f"{document_id}--{sanitize_original_name(original_filename)}{suffix}"


def build_upload_payload(
    *,
    document_id: str,
    source_type: str,
    company: str | None,
    filename: str,
    original_filename: str,
    path: str,
    status: str,
    validation_state: str,
    query_ready: bool,
    validation: dict,
    content_hash: str,
    byte_size: int,
    duplicate_of: dict | None = None,
) -> dict:
    return {
        "document_id": document_id,
        "source_type": source_type,
        "company": company,
        "filename": filename,
        "original_filename": original_filename,
        "path": path,
        "status": status,
        "validation_state": validation_state,
        "query_ready": query_ready,
        "validation": validation,
        "content_hash": content_hash,
        "sha256": content_hash,
        "byte_size": byte_size,
        "duplicate_of": duplicate_of,
        "warnings_json": None,
        "detected_mime": None,
        "detected_encoding": None,
        "page_count": None,
        "source_uri": None,
        "supersedes_document_id": None,
        "ingested_at": None,
    }


@app.get("/health", response_model=Envelope)
async def health():
    try:
        data = await client.get("/health")
        return ok({"status": "ok", "lightrag": data})
    except Exception as exc:
        return Envelope(
            ok=False,
            data=None,
            meta={"request_id": str(uuid.uuid4())},
            error={"code": "upstream_error", "message": str(exc)},
        )


@app.get("/ingest/status", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def ingest_status():
    return ok({"summary": await get_summary(), "documents": await list_documents()})


@app.get("/documents", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def documents():
    return ok({"documents": await list_documents()})


@app.get("/documents/{document_id}", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def document_detail(document_id: str):
    docs = await list_documents()
    for doc in docs:
        if doc["document_id"] == document_id:
            return ok({"document": doc})
    raise HTTPException(status_code=404, detail="Document not found")


@app.post("/upload", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def upload(file: UploadFile = File(...), company: str | None = Form(default=None)):
    original_filename = file.filename or "unknown"
    document_id = str(uuid.uuid4())
    stored_filename = build_stored_filename(document_id, original_filename)
    filename = stored_filename
    normalized_company = company.strip() if company and company.strip() else None

    # Use the actual file size from the UploadFile so that
    # validate_upload can distinguish empty from non-empty uploads.
    preflight_size = file.size if file.size is not None else 1
    verdict = validate_upload(original_filename, preflight_size)
    rejected_path = f"rejected/{stored_filename}"

    if verdict.verdict == "reject":
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            path=rejected_path,
            status="rejected",
            validation_state="reject",
            query_ready=False,
            validation=verdict.meta,
            content_hash="pending",
            byte_size=file.size if file.size is not None else 0,
        )
        await upsert_document(
            document_id=document_id,
            path=rejected_path,
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            sha256="pending",
            status="rejected",
            track_id=None,
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
        )
        return Envelope(
            ok=False,
            data=payload,
            meta={"request_id": str(uuid.uuid4())},
            error={
                "code": verdict.meta.get("error_code", "validation_error"),
                "message": verdict.meta.get("error_message", "Validation failed"),
            },
        )

    upload_dir = Path(settings.uploads_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / stored_filename
    temp_dest = upload_dir / f".{document_id}.part"
    digest = hashlib.sha256()
    async with aiofiles.open(temp_dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            digest.update(chunk)
            await f.write(chunk)

    file_size = temp_dest.stat().st_size
    verdict = validate_upload(original_filename, file_size)
    sha256 = digest.hexdigest()

    if verdict.verdict == "reject":
        temp_dest.unlink(missing_ok=True)
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            path=rejected_path,
            status="rejected",
            validation_state="reject",
            query_ready=False,
            validation=verdict.meta,
            content_hash=sha256,
            byte_size=file_size,
        )
        await upsert_document(
            document_id=document_id,
            path=rejected_path,
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            sha256=sha256,
            status="rejected",
            track_id=None,
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            byte_size=file_size,
            content_hash=sha256,
        )
        return Envelope(
            ok=False,
            data=payload,
            meta={"request_id": str(uuid.uuid4())},
            error={
                "code": verdict.meta.get("error_code", "validation_error"),
                "message": verdict.meta.get("error_message", "Validation failed"),
            },
        )

    temp_dest.replace(dest)

    duplicate_doc = await get_document_by_sha256(sha256)
    duplicate_of = None
    warnings_json = None

    if duplicate_doc and duplicate_doc.get("path") != str(dest):
        duplicate_of = {
            "document_id": duplicate_doc.get("document_id"),
            "path": duplicate_doc.get("path"),
            "original_filename": duplicate_doc.get("original_filename"),
            "company": duplicate_doc.get("company"),
        }
        verdict.meta = {
            **verdict.meta,
            "duplicate_of": duplicate_of,
        }
        warnings_json = json.dumps({"duplicate_of": duplicate_of}, sort_keys=True)
        if verdict.verdict in {"accept", "warn"}:
            verdict = type(verdict)("warn", {
                **verdict.meta,
                "error_code": verdict.meta.get("error_code", "duplicate_content"),
                "error_message": verdict.meta.get(
                    "error_message",
                    "Duplicate content detected at upload time",
                ),
            })

    if verdict.verdict == "auto_split":
        payload = build_upload_payload(
            document_id=document_id,
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            path=str(dest),
            status="pending",
            validation_state="auto_split",
            query_ready=False,
            validation=verdict.meta,
            content_hash=sha256,
            byte_size=file_size,
            duplicate_of=duplicate_of,
        )
        payload["warnings_json"] = warnings_json
        await upsert_document(
            document_id=document_id,
            path=str(dest),
            source_type="upload",
            company=normalized_company,
            filename=filename,
            original_filename=original_filename,
            sha256=sha256,
            status="pending",
            track_id=None,
            validation_state="auto_split",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            byte_size=file_size,
            content_hash=sha256,
            warnings_json=warnings_json,
        )
        return ok(payload)

    validation_state = "warn" if verdict.verdict == "warn" else "accept"
    payload = build_upload_payload(
        document_id=document_id,
        source_type="upload",
        company=normalized_company,
        filename=filename,
        original_filename=original_filename,
        path=str(dest),
        status="pending",
        validation_state=validation_state,
        query_ready=False,
        validation=verdict.meta,
        content_hash=sha256,
        byte_size=file_size,
        duplicate_of=duplicate_of,
    )
    payload["warnings_json"] = warnings_json
    await upsert_document(
        document_id=document_id,
        path=str(dest),
        source_type="upload",
        company=normalized_company,
        filename=filename,
        original_filename=original_filename,
        sha256=sha256,
        status="pending",
        track_id=None,
        validation_state=validation_state,
        error_stage="validation" if verdict.verdict == "warn" else None,
        error_code=verdict.meta.get("error_code") if verdict.verdict == "warn" else None,
        error_message=verdict.meta.get("error_message") if verdict.verdict == "warn" else None,
        error_detail=None,
        query_ready=False,
        byte_size=file_size,
        content_hash=sha256,
        warnings_json=warnings_json,
    )
    return ok(payload)


@app.post("/query", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def query(req: QueryRequest):
    result = await client.post_json(
        "/query",
        {
            "query": req.query,
            "mode": req.mode,
            "top_k": req.top_k,
            "include_references": True,
            "include_chunk_content": True,
        },
    )
    return ok({"answer": result.get("response", ""), "citations": result.get("references", [])})


@app.post("/generate-document", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def generate(req: GenerateDocumentRequest):
    return ok(await generate_document(req.query, req.document_type))


@app.post("/ingest/reindex", response_model=Envelope, dependencies=[Depends(require_bearer)])
async def reindex(req: ReindexRequest):
    return ok({"queued": True, "scope": req.model_dump()})


@app.get("/api/version")
async def api_version():
    resp = await client.proxy("GET", "/api/version")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.get("/api/tags")
async def api_tags():
    resp = await client.proxy("GET", "/api/tags")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.get("/api/ps")
async def api_ps():
    resp = await client.proxy("GET", "/api/ps")
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.post("/api/generate")
async def api_generate(request: Request):
    body = await request.body()
    resp = await client.proxy("POST", "/api/generate", body=body, content_type=request.headers.get("content-type"))
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))


@app.post("/api/chat")
async def api_chat(request: Request):
    body = await request.body()
    resp = await client.proxy("POST", "/api/chat", body=body, content_type=request.headers.get("content-type"))
    return Response(content=resp.content, media_type=resp.headers.get("content-type", "application/json"))
""",
    app_dir / "worker.py": """from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

from .config import settings
from .lightrag_client import client
from .state_store import (
    get_document_by_path,
    get_documents_by_sha256,
    increment_retry,
    init_db,
    upsert_document,
    update_document_state,
)
from .validation import validate_file

SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".html", ".htm"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".json", ".csv"}
TERMINAL_SUCCESS = {"ingested"}
LIGHTRAG_INTERNAL_DIRS = {"__enqueued__"}
ACTIVE_DUPLICATE_STATUSES = {"accepted", "submitted", "processing", "ingested"}
NON_RETRYABLE_FAILURE_CODES = {
    "decode_failed",
    "lightrag_processing_failed",
    "submit_terminal_failed",
    "track_status_failed",
}
TRANSIENT_HTTP_STATUS_CODES = {408, 429, 500, 502, 503, 504}
TRANSIENT_EXCEPTION_NAMES = {
    "ConnectError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadError",
    "ReadTimeout",
    "RemoteProtocolError",
    "RequestError",
    "TimeoutException",
    "WriteError",
    "WriteTimeout",
}
METADATA_UNSET = object()


def warn(message: str) -> None:
    import sys

    print(f"WARNING: {message}", file=sys.stderr)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def should_skip_path(path: Path, root: Path, source_type: str) -> bool:
    if source_type != "filesystem":
        return False
    try:
        relative_parts = path.relative_to(root).parts
    except ValueError:
        return False
    return any(part in LIGHTRAG_INTERNAL_DIRS for part in relative_parts)


def read_text_with_fallbacks(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def parse_warning_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"legacy_warning": raw}
    return parsed if isinstance(parsed, dict) else {}


def merge_warning_metadata(
    raw: str | None,
    *,
    duplicate_of: dict[str, Any] | None | object = METADATA_UNSET,
    worker_updates: dict[str, Any] | None = None,
) -> str:
    meta = parse_warning_metadata(raw)
    if duplicate_of is not METADATA_UNSET:
        if duplicate_of is None:
            meta.pop("duplicate_of", None)
        else:
            meta["duplicate_of"] = duplicate_of
    if worker_updates:
        worker = meta.get("worker")
        if not isinstance(worker, dict):
            worker = {}
        for key, value in worker_updates.items():
            if value is None:
                worker.pop(key, None)
            else:
                worker[key] = value
        if worker:
            meta["worker"] = worker
        else:
            meta.pop("worker", None)
    return json.dumps(meta, sort_keys=True)


def build_duplicate_reference(doc: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": doc.get("document_id"),
        "path": doc.get("path"),
        "original_filename": doc.get("original_filename"),
        "company": doc.get("company"),
        "status": doc.get("status"),
        "validation_state": doc.get("validation_state"),
    }


def pick_duplicate_candidate(documents: list[dict[str, Any]], current_path: str) -> dict[str, Any] | None:
    rank = {"ingested": 0, "processing": 1, "submitted": 2, "accepted": 3}
    candidates = [
        doc
        for doc in documents
        if doc.get("path") != current_path and doc.get("status") in ACTIVE_DUPLICATE_STATUSES
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda doc: (
            rank.get(str(doc.get("status")), 99),
            str(doc.get("updated_at", "")),
            str(doc.get("path", "")),
            str(doc.get("document_id", "")),
        ),
        reverse=False,
    )
    duplicate_of = build_duplicate_reference(candidates[0])
    duplicate_of["match_count"] = len(candidates)
    duplicate_of["match_paths"] = sorted(str(doc.get("path", "")) for doc in candidates)
    duplicate_of["selection_basis"] = "sha256_status_updated_at_path"
    return duplicate_of


def stale_track_worker_updates(track_id: str, reason: str) -> dict[str, Any]:
    return {
        "failure_class": "transient",
        "retryable": True,
        "retry_reason": "stale_track_id",
        "last_polled_track_id": track_id,
        "stale_track_id": track_id,
        "stale_track_marker": True,
        "stale_track_reason": reason,
        "track_id_policy": "clear_and_resubmit",
        "track_poll_payload": None,
    }


def response_status_code(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def is_transient_exception(exc: Exception) -> bool:
    status_code = response_status_code(exc)
    if status_code in TRANSIENT_HTTP_STATUS_CODES:
        return True
    return exc.__class__.__name__ in TRANSIENT_EXCEPTION_NAMES


def is_retryable_failure(doc: dict[str, Any]) -> bool:
    error_code = doc.get("error_code")
    return bool(error_code) and error_code not in NON_RETRYABLE_FAILURE_CODES


async def mark_duplicate_candidate(path: Path, digest: str, duplicate_of: dict[str, Any], existing: dict[str, Any] | None) -> None:
    warnings_json = merge_warning_metadata(
        existing.get("warnings_json") if existing else None,
        duplicate_of=duplicate_of,
        worker_updates={
            "dedupe_basis": duplicate_of.get("selection_basis"),
            "dedupe_action": "skip_duplicate_content",
            "duplicate_match_count": duplicate_of.get("match_count"),
            "duplicate_match_paths": duplicate_of.get("match_paths"),
            "duplicate_status": duplicate_of.get("status"),
            "last_submitted_track_id": None,
            "stale_track_id": None,
            "stale_track_marker": None,
            "stale_track_reason": None,
            "track_id_policy": None,
            "track_poll_payload": None,
        },
    )
    await update_document_state(
        str(path),
        sha256=digest,
        status="accepted",
        validation_state="warn",
        error_stage="validation",
        error_code="duplicate_content",
        error_message="Duplicate content already represented by another document",
        error_detail=(
            f"Duplicate content matches {duplicate_of.get('path')} "
            f"among {duplicate_of.get('match_count')} active SHA matches"
        ),
        last_error=None,
        track_id=None,
        query_ready=False,
        content_hash=digest,
        byte_size=path.stat().st_size,
        ingested_at=None,
        retry_count=0,
        warnings_json=warnings_json,
    )
    warn(f"Skipping duplicate content for {path}: {duplicate_of.get('path')}")


async def submit_file(path: Path, source_type: str, digest: str, *, retry_reason: str | None = None) -> None:
    existing = await get_document_by_path(str(path))
    company = existing.get("company") if existing else None
    original_filename = existing.get("original_filename") if existing else None
    filename = existing.get("filename") if existing else None
    byte_size = path.stat().st_size

    verdict = validate_file(path, sha256=digest, file_size=byte_size)
    warnings_json = existing.get("warnings_json") if existing else None

    if verdict.verdict == "reject":
        await update_document_state(
            str(path),
            sha256=digest,
            status="rejected",
            validation_state="reject",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            last_error=verdict.meta.get("error_message"),
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=byte_size,
            ingested_at=None,
        )
        warn(f"Validation rejected {path}: {verdict.meta.get('error_code')}")
        return

    if verdict.verdict == "auto_split":
        await update_document_state(
            str(path),
            sha256=digest,
            status="accepted",
            validation_state="auto_split",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates={
                    "dedupe_action": None,
                    "submission_blocked": "auto_split",
                    "duplicate_match_count": None,
                    "duplicate_match_paths": None,
                    "duplicate_status": None,
                },
            ),
        )
        warn(f"Auto-split flagged for {path}: {verdict.meta.get('error_code')}")
        return

    detected_encoding = None
    text_payload = None
    if path.suffix.lower() in TEXT_EXTENSIONS:
        try:
            text_payload, detected_encoding = read_text_with_fallbacks(path)
        except Exception as exc:
            await update_document_state(
                str(path),
                sha256=digest,
                status="failed",
                validation_state=existing.get("validation_state") if existing else "accept",
                error_stage="decode",
                error_code="decode_failed",
                error_message="Worker failed to decode text content",
                error_detail=str(exc),
                last_error=str(exc),
                track_id=None,
                query_ready=False,
                content_hash=digest,
                byte_size=byte_size,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    duplicate_of=None,
                    worker_updates={
                        "dedupe_action": None,
                        "failure_class": "terminal",
                        "submission_blocked": None,
                        "retryable": False,
                    },
                ),
            )
            return

    if verdict.verdict == "warn":
        warnings_json = merge_warning_metadata(
            warnings_json,
            duplicate_of=None,
            worker_updates={
                "dedupe_action": None,
                "validation_warning": verdict.meta.get("error_code"),
                "detected_encoding": detected_encoding,
                "submission_blocked": None,
            },
        )
        await update_document_state(
            str(path),
            sha256=digest,
            validation_state="warn",
            error_stage="validation",
            error_code=verdict.meta.get("error_code"),
            error_message=verdict.meta.get("error_message"),
            error_detail=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            warnings_json=warnings_json,
        )
        warn(f"Validation warning for {path}: {verdict.meta.get('error_code')}")
    else:
        warnings_json = merge_warning_metadata(
            warnings_json,
            duplicate_of=None,
            worker_updates={
                "dedupe_action": None,
                "detected_encoding": detected_encoding,
                "submission_blocked": None,
            },
        )

    try:
        if text_payload is not None:
            resp = await client.post_json(
                "/documents/text",
                {
                    "text": text_payload,
                    "file_source": str(path),
                },
            )
        else:
            resp = await client.post_file("/documents/upload", str(path))
    except Exception as exc:
        retryable = is_transient_exception(exc)
        error_code = "submit_transient_failed" if retryable else "submit_terminal_failed"
        error_message = (
            "Transient LightRAG submit failure; worker will retry"
            if retryable
            else "Terminal LightRAG submit failure"
        )
        await update_document_state(
            str(path),
            sha256=digest,
            status="failed",
            validation_state="warn" if verdict.verdict == "warn" else "accept",
            error_stage="submit",
            error_code=error_code,
            error_message=error_message,
            error_detail=str(exc),
            last_error=str(exc),
            track_id=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates={
                    "dedupe_action": None,
                    "failure_class": "transient" if retryable else "terminal",
                    "retryable": retryable,
                    "retry_reason": retry_reason,
                    "submission_blocked": None,
                },
            ),
        )
        return

    track_id = resp.get("track_id") or None
    if not track_id:
        await update_document_state(
            str(path),
            sha256=digest,
            status="failed",
            validation_state="warn" if verdict.verdict == "warn" else "accept",
            error_stage="submit",
            error_code="submit_missing_track_id",
            error_message="LightRAG submit response missing track_id; worker will retry",
            error_detail=json.dumps(resp, sort_keys=True),
            last_error="missing track_id",
            track_id=None,
            query_ready=False,
            content_hash=digest,
            detected_encoding=detected_encoding,
            byte_size=byte_size,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                warnings_json,
                duplicate_of=None,
                worker_updates={
                    "dedupe_action": None,
                    "failure_class": "transient",
                    "retryable": True,
                    "retry_reason": retry_reason,
                    "submission_blocked": None,
                },
            ),
        )
        return

    warnings_json = merge_warning_metadata(
        warnings_json,
        duplicate_of=None,
        worker_updates={
            "dedupe_action": None,
            "duplicate_match_count": None,
            "duplicate_match_paths": None,
            "duplicate_status": None,
            "last_submitted_track_id": track_id,
            "last_polled_track_id": track_id,
            "retry_reason": retry_reason,
            "submission_blocked": None,
            "stale_track_id": None,
            "stale_track_marker": None,
            "stale_track_reason": None,
            "track_id_policy": None,
            "track_poll_payload": None,
        },
    )
    await upsert_document(
        document_id=str(uuid.uuid4()),
        path=str(path),
        source_type=source_type,
        company=company,
        filename=filename or path.name,
        original_filename=original_filename or path.name,
        sha256=digest,
        status="submitted",
        track_id=track_id,
        last_error=None,
        validation_state=verdict.verdict if verdict.verdict in ("accept", "warn") else "accept",
        error_stage="validation" if verdict.verdict == "warn" else None,
        error_code=verdict.meta.get("error_code") if verdict.verdict == "warn" else None,
        error_message=verdict.meta.get("error_message") if verdict.verdict == "warn" else None,
        error_detail=None,
        query_ready=False,
        content_hash=digest,
        detected_encoding=detected_encoding,
        byte_size=byte_size,
        warnings_json=warnings_json,
    )
    await update_document_state(str(path), retry_count=0)


async def poll_track(doc: dict[str, Any]) -> None:
    track_id = doc.get("track_id")
    if not track_id:
        return
    warnings_json = doc.get("warnings_json")
    try:
        result = await client.get(f"/documents/track_status/{track_id}")
        docs = result.get("documents", [])
        statuses = {str(d.get("status", "")).upper() for d in docs if d.get("status")}
        if not docs:
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="stale_track_id",
                error_message="LightRAG track_id no longer resolves; worker will resubmit",
                error_detail=f"Track {track_id} returned no documents",
                last_error="stale track_id",
                track_id=None,
                query_ready=False,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates=stale_track_worker_updates(track_id, "empty_documents"),
                ),
            )
            return
        if not statuses:
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="track_status_transient",
                error_message="Transient LightRAG track poll failure; worker will retry",
                error_detail=f"Track {track_id} returned documents without statuses",
                last_error="track status missing",
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "transient",
                        "retryable": True,
                        "retry_reason": "track_status_transient",
                        "last_polled_track_id": track_id,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": "missing_status",
                    },
                ),
            )
            return
        if "FAILED" in statuses:
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="lightrag_processing_failed",
                error_message="LightRAG reported failure",
                error_detail=f"Track {track_id} reported FAILED",
                last_error="LightRAG reported failure",
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "terminal",
                        "last_polled_track_id": track_id,
                        "retryable": False,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": None,
                    },
                ),
            )
        elif "PROCESSED" in statuses:
            await update_document_state(
                doc["path"],
                status="ingested",
                error_stage=None,
                error_code=None,
                error_message=None,
                error_detail=None,
                last_error=None,
                query_ready=True,
                retry_count=0,
                ingested_at="datetime('now')",
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "last_polled_track_id": track_id,
                        "last_track_status": "PROCESSED",
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                        "track_poll_payload": None,
                    },
                ),
            )
        else:
            await update_document_state(
                doc["path"],
                status="processing",
                error_stage=None,
                error_code=None,
                error_message=None,
                error_detail=None,
                last_error=None,
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "last_polled_track_id": track_id,
                        "last_track_status": sorted(statuses),
                        "track_poll_payload": None,
                    },
                ),
            )
    except Exception as exc:
        status_code = response_status_code(exc)
        if status_code == 404:
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="stale_track_id",
                error_message="LightRAG track_id no longer resolves; worker will resubmit",
                error_detail=str(exc),
                last_error=str(exc),
                track_id=None,
                query_ready=False,
                ingested_at=None,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates=stale_track_worker_updates(track_id, "http_404"),
                ),
            )
        elif is_transient_exception(exc):
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="track_status_transient",
                error_message="Transient LightRAG track poll failure; worker will retry",
                error_detail=str(exc),
                last_error=str(exc),
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "transient",
                        "last_polled_track_id": track_id,
                        "retryable": True,
                        "retry_reason": "track_status_transient",
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                    },
                ),
            )
        else:
            await update_document_state(
                doc["path"],
                status="failed",
                error_stage="track_poll",
                error_code="track_status_failed",
                error_message="Failed to poll LightRAG track status",
                error_detail=str(exc),
                last_error=str(exc),
                query_ready=False,
                warnings_json=merge_warning_metadata(
                    warnings_json,
                    worker_updates={
                        "failure_class": "terminal",
                        "last_polled_track_id": track_id,
                        "retryable": False,
                        "stale_track_id": None,
                        "stale_track_marker": None,
                        "stale_track_reason": None,
                        "track_id_policy": None,
                    },
                ),
            )


async def handle_candidate(path: Path, source_type: str, root: Path) -> None:
    if should_skip_path(path, root, source_type):
        return

    if str(path).startswith("rejected/"):
        return

    existing = await get_document_by_path(str(path))
    if not existing:
        await upsert_document(
            document_id=str(uuid.uuid4()),
            path=str(path),
            source_type=source_type,
            company=None,
            filename=path.name,
            original_filename=path.name,
            sha256="pending",
            status="validating",
            track_id=None,
            validation_state="not_run",
            query_ready=False,
            byte_size=path.stat().st_size,
        )

    digest = sha256_of(path)
    existing = await get_document_by_path(str(path))
    if existing is None:
        return

    status = existing.get("status")
    current_sha256 = existing.get("sha256")
    same_content = current_sha256 == digest
    content_changed = current_sha256 not in {None, "", "pending", "error"} and current_sha256 != digest

    if same_content and status in TERMINAL_SUCCESS:
        return
    if same_content and status == "rejected":
        return
    if same_content and existing.get("validation_state") == "auto_split":
        return
    if same_content and existing.get("error_code") == "duplicate_content" and not existing.get("track_id"):
        return

    if content_changed:
        await update_document_state(
            str(path),
            sha256=digest,
            status="validating",
            error_stage=None,
            error_code=None,
            error_message=None,
            error_detail=None,
            last_error=None,
            track_id=None,
            query_ready=False,
            content_hash=digest,
            byte_size=path.stat().st_size,
            retry_count=0,
            ingested_at=None,
            warnings_json=merge_warning_metadata(
                existing.get("warnings_json"),
                duplicate_of=None,
                worker_updates={
                    "dedupe_action": None,
                    "duplicate_match_count": None,
                    "duplicate_match_paths": None,
                    "duplicate_status": None,
                    "previous_sha256": current_sha256,
                    "previous_track_id": existing.get("track_id"),
                    "retry_reason": "content_changed",
                    "stale_track_id": None,
                    "stale_track_marker": None,
                    "stale_track_reason": None,
                    "submission_blocked": None,
                    "track_id_policy": "replace_on_content_change",
                    "track_poll_payload": None,
                },
            ),
        )
        existing = await get_document_by_path(str(path)) or existing
        status = existing.get("status")

    duplicate_of = pick_duplicate_candidate(await get_documents_by_sha256(digest), str(path))
    if duplicate_of is not None:
        await mark_duplicate_candidate(path, digest, duplicate_of, existing)
        return

    if status in {"submitted", "processing"} and existing.get("track_id"):
        await poll_track(existing)
        refreshed = await get_document_by_path(str(path))
        if refreshed and refreshed.get("status") in TERMINAL_SUCCESS:
            return
        if refreshed and refreshed.get("status") in {"submitted", "processing"}:
            return
        existing = refreshed or existing
        status = existing.get("status")

    if status == "failed" and existing.get("track_id") and existing.get("error_code") == "track_status_transient":
        await poll_track(existing)
        refreshed = await get_document_by_path(str(path))
        if refreshed and refreshed.get("status") in TERMINAL_SUCCESS:
            return
        if refreshed and refreshed.get("status") in {"submitted", "processing"}:
            return
        existing = refreshed or existing
        status = existing.get("status")

    if status == "failed":
        if not is_retryable_failure(existing):
            return
        if int(existing.get("retry_count") or 0) >= settings.ingest_max_retries:
            return
        if existing.get("track_id") and existing.get("error_code") == "track_status_transient":
            await increment_retry(str(path))
            return
        await increment_retry(str(path))
        existing = await get_document_by_path(str(path)) or existing

    retry_reason = None
    if content_changed:
        retry_reason = "content_changed"
    elif status == "failed":
        retry_reason = str(existing.get("error_code") or "retry")

    await update_document_state(
        str(path),
        sha256=digest,
        status="accepted",
        query_ready=False,
        content_hash=digest,
        byte_size=path.stat().st_size,
        ingested_at=None,
    )
    await submit_file(path, source_type, digest, retry_reason=retry_reason)


async def scan_once() -> None:
    for base, source_type in ((Path(settings.source_docs_dir), "filesystem"), (Path(settings.uploads_dir), "upload")):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() not in SUPPORTED:
                await upsert_document(
                    document_id=str(uuid.uuid4()),
                    path=str(path),
                    source_type=source_type,
                    company=None,
                    filename=path.name,
                    original_filename=path.name,
                    sha256="pending",
                    status="rejected",
                    track_id=None,
                    validation_state="reject",
                    error_stage="validation",
                    error_code="unsupported_extension",
                    error_message="File extension is not supported",
                    error_detail=None,
                    query_ready=False,
                )
                continue
            try:
                await handle_candidate(path, source_type, base)
            except Exception as exc:
                existing = await get_document_by_path(str(path))
                if existing:
                    await update_document_state(
                        str(path),
                        status="failed",
                        error_stage="submit",
                        error_code="ingest_execution_failed",
                        error_message="Worker failed while handling candidate",
                        error_detail=str(exc),
                        last_error=str(exc),
                        query_ready=False,
                        warnings_json=merge_warning_metadata(
                            existing.get("warnings_json"),
                            worker_updates={
                                "failure_class": "transient",
                                "retryable": True,
                            },
                        ),
                    )
                else:
                    await upsert_document(
                        document_id=str(uuid.uuid4()),
                        path=str(path),
                        source_type=source_type,
                        company=None,
                        filename=path.name,
                        original_filename=path.name,
                        sha256="error",
                        status="failed",
                        track_id=None,
                        validation_state="not_run",
                        error_stage="submit",
                        error_code="ingest_execution_failed",
                        error_message="Worker failed while handling candidate",
                        error_detail=str(exc),
                        query_ready=False,
                        last_error=str(exc),
                        warnings_json=merge_warning_metadata(
                            None,
                            worker_updates={
                                "failure_class": "transient",
                                "retryable": True,
                            },
                        ),
                    )


async def main() -> None:
    await init_db()
    while True:
        await scan_once()
        await asyncio.sleep(settings.ingest_scan_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
""",
}

for path, content in files.items():
    if content is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

import shutil
src_app = Path(__file__).parent.parent / "app"
dst_app = project_dir / "app"
for name in ["__init__.py", "config.py", "auth.py", "lightrag_client.py", "generation.py"]:
    src = src_app / name
    if src.exists():
        shutil.copy2(src, dst_app / name)

PY

printf "Building and starting services...\n"
podman compose -f "$PROJECT_DIR/compose.yml" -p ragproj up -d --build

printf "Running post-start smoke tests...\n"
curl -fsS "http://localhost:8000/health" >/dev/null
curl -fsS "http://localhost:8000/api/version" >/dev/null
curl -fsS "http://localhost:8000/api/tags" >/dev/null

printf "\nDone.\n"
printf "Gateway API docs: http://localhost:8000/docs\n"
printf "Open WebUI:      http://localhost:3000\n"
printf "Project dir:     %s\n" "$PROJECT_DIR"
printf "\nUse this bearer token for gateway calls:\n"
printf "Authorization: Bearer %s\n" "$RAG_API_KEY"
