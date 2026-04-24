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

PORT=9621
WORKERS=2
MAX_ASYNC=4
MAX_PARALLEL_INSERT=2
TIMEOUT=150

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
    app_dir / "__init__.py": "",
    app_dir / "config.py": """from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    rag_api_key: str = Field(alias="RAG_API_KEY")
    lightrag_internal_api_key: str = Field(alias="LIGHTRAG_INTERNAL_API_KEY")
    lightrag_base_url: str = Field(alias="LIGHTRAG_BASE_URL")

    source_docs_dir: str = Field(alias="SOURCE_DOCS_DIR")
    uploads_dir: str = Field(alias="UPLOADS_DIR")
    state_db_path: str = Field(alias="STATE_DB_PATH")

    ingest_scan_interval_seconds: int = Field(default=30, alias="INGEST_SCAN_INTERVAL_SECONDS")
    ingest_max_retries: int = Field(default=3, alias="INGEST_MAX_RETRIES")
    request_timeout_seconds: int = Field(default=300, alias="REQUEST_TIMEOUT_SECONDS")


settings = Settings()
""",
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
    app_dir / "auth.py": """from fastapi import Header, HTTPException
from .config import settings


def require_bearer(authorization: str | None = Header(default=None)) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.rag_api_key:
        raise HTTPException(status_code=401, detail="Invalid bearer token")
""",
    app_dir / "lightrag_client.py": """from __future__ import annotations

import httpx
from .config import settings


class LightRAGClient:
    def __init__(self) -> None:
        self.base_url = settings.lightrag_base_url.rstrip("/")
        self.headers = {"X-API-Key": settings.lightrag_internal_api_key}
        self.timeout = settings.request_timeout_seconds

    async def get(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(f"{self.base_url}{path}", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def post_json(self, path: str, payload: dict) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(f"{self.base_url}{path}", headers=self.headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    async def post_file(self, path: str, file_path: str) -> dict:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            with open(file_path, "rb") as f:
                files = {"file": (file_path.split("/")[-1], f)}
                resp = await client.post(f"{self.base_url}{path}", headers=self.headers, files=files)
            resp.raise_for_status()
            return resp.json()

    async def proxy(self, method: str, path: str, body: bytes | None = None, content_type: str | None = None):
        headers = dict(self.headers)
        if content_type:
            headers["Content-Type"] = content_type
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.request(method, f"{self.base_url}{path}", headers=headers, content=body)
            resp.raise_for_status()
            return resp


client = LightRAGClient()
""",
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
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    track_id TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
'''


async def init_db() -> None:
    Path(settings.state_db_path).parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def upsert_document(
    document_id: str,
    path: str,
    source_type: str,
    company: str | None,
    filename: str,
    sha256: str,
    status: str,
    track_id: str | None,
    last_error: str | None = None,
) -> None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            '''
            INSERT INTO documents (document_id, path, source_type, company, filename, sha256, status, retry_count, track_id, last_error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, datetime('now'), datetime('now'))
            ON CONFLICT(path) DO UPDATE SET
              company=excluded.company,
              filename=excluded.filename,
              sha256=excluded.sha256,
              status=excluded.status,
              track_id=excluded.track_id,
              last_error=excluded.last_error,
              updated_at=datetime('now')
            ''',
            (document_id, path, source_type, company, filename, sha256, status, track_id, last_error),
        )
        await db.commit()


async def update_status(path: str, status: str, last_error: str | None = None) -> None:
    async with aiosqlite.connect(settings.state_db_path) as db:
        await db.execute(
            "UPDATE documents SET status=?, last_error=?, updated_at=datetime('now') WHERE path=?",
            (status, last_error, path),
        )
        await db.commit()


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


async def list_documents() -> list[dict]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM documents ORDER BY updated_at DESC")
        rows = await cur.fetchall()
        return [dict(r) for r in rows]


async def get_summary() -> dict[str, int]:
    async with aiosqlite.connect(settings.state_db_path) as db:
        cur = await db.execute("SELECT status, COUNT(*) FROM documents GROUP BY status")
        rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}
""",
    app_dir / "generation.py": """from .lightrag_client import client


PROMPTS = {
    "summary": "Create a concise grounded summary from the retrieved context.",
    "memo": "Draft a professional memo grounded only in the retrieved context.",
    "report": "Draft a structured report grounded only in the retrieved context.",
    "proposal": "Draft a proposal grounded only in the retrieved context.",
    "policy": "Draft a policy document grounded only in the retrieved context.",
    "brief": "Draft a concise brief grounded only in the retrieved context.",
    "draft": "Draft a document grounded only in the retrieved context.",
}


async def generate_document(query: str, document_type: str) -> dict:
    retrieval = await client.post_json(
        "/query",
        {
            "query": query,
            "mode": "mix",
            "include_references": True,
            "include_chunk_content": True,
        },
    )
    references = retrieval.get("references", [])
    context_parts: list[str] = []
    for ref in references:
        file_path = ref.get("file_path", "")
        content = ref.get("content") or []
        joined = "\\n\\n".join(content)
        if joined:
            context_parts.append(f"Source: {file_path}\\n{joined}")
    context = "\\n\\n".join(context_parts) if context_parts else retrieval.get("response", "")
    prompt = (
        f"{PROMPTS[document_type]}\\n\\n"
        f"User request:\\n{query}\\n\\n"
        f"Context:\\n{context}\\n\\n"
        "Return only the requested document."
    )
    llm = await client.post_json(
        "/query",
        {
            "query": prompt,
            "mode": "bypass",
            "include_references": False,
        },
    )
    return {
        "document_type": document_type,
        "title": document_type.title(),
        "document": llm.get("response", ""),
        "citations": references,
    }
""",
    app_dir / "api.py": """from __future__ import annotations

import uuid
from pathlib import Path

import aiofiles
from fastapi import Depends, FastAPI, File, HTTPException, Request, Response, UploadFile

from .auth import require_bearer
from .generation import generate_document
from .lightrag_client import client
from .models import Envelope, GenerateDocumentRequest, QueryRequest, ReindexRequest
from .state_store import get_summary, init_db, list_documents, upsert_document

app = FastAPI(title="RAG Gateway", version="0.1.0")


@app.on_event("startup")
async def startup() -> None:
    await init_db()


def ok(data):
    return Envelope(ok=True, data=data, meta={"request_id": str(uuid.uuid4())}, error=None)


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
async def upload(file: UploadFile = File(...)):
    upload_dir = Path("/app/uploads")
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / file.filename
    async with aiofiles.open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            await f.write(chunk)
    await upsert_document(
        document_id=str(uuid.uuid4()),
        path=str(dest),
        source_type="upload",
        company=None,
        filename=file.filename,
        sha256="pending",
        status="pending",
        track_id=None,
    )
    return ok({"filename": file.filename, "path": str(dest), "status": "pending"})


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
import uuid
from pathlib import Path

from .config import settings
from .lightrag_client import client
from .state_store import (
    get_document_by_path,
    increment_retry,
    init_db,
    upsert_document,
    update_status,
)


SUPPORTED = {".txt", ".md", ".pdf", ".docx", ".pptx", ".xlsx", ".csv", ".json", ".html", ".htm"}
TEXT_EXTENSIONS = {".txt", ".md", ".html", ".htm", ".json", ".csv"}
TERMINAL_SUCCESS = {"ingested"}
TERMINAL_FAILURE = {"failed"}
LIGHTRAG_INTERNAL_DIRS = {"__enqueued__"}


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


def read_text_with_fallbacks(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


async def submit_file(path: Path, source_type: str, digest: str) -> None:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        text = read_text_with_fallbacks(path)
        resp = await client.post_json(
            "/documents/text",
            {
                "text": text,
                "file_source": str(path),
            },
        )
    else:
        resp = await client.post_file("/documents/upload", str(path))

    track_id = resp.get("track_id", "")
    await upsert_document(
        document_id=str(uuid.uuid4()),
        path=str(path),
        source_type=source_type,
        company=None,
        filename=path.name,
        sha256=digest,
        status="submitted",
        track_id=track_id,
    )


async def poll_track(doc: dict) -> None:
    track_id = doc.get("track_id")
    if not track_id:
        return
    try:
        result = await client.get(f"/documents/track_status/{track_id}")
        docs = result.get("documents", [])
        statuses = {d.get("status", "").upper() for d in docs}
        if "FAILED" in statuses:
            await update_status(doc["path"], "failed", "LightRAG reported failure")
        elif "PROCESSED" in statuses:
            await update_status(doc["path"], "ingested")
        elif statuses:
            await update_status(doc["path"], "processing")
    except Exception as exc:
        await update_status(doc["path"], "failed", str(exc))


async def handle_candidate(path: Path, source_type: str, root: Path) -> None:
    if should_skip_path(path, root, source_type):
        return

    digest = sha256_of(path)
    existing = await get_document_by_path(str(path))

    if existing:
        status = existing["status"]
        if existing["sha256"] == digest and status in TERMINAL_SUCCESS:
            return
        if status in {"submitted", "processing"} and existing.get("track_id"):
            await poll_track(existing)
            refreshed = await get_document_by_path(str(path))
            if refreshed and refreshed["status"] in TERMINAL_SUCCESS:
                return
            if refreshed and refreshed["status"] in {"submitted", "processing"}:
                return
        if status in TERMINAL_FAILURE and existing["retry_count"] >= settings.ingest_max_retries:
            return
        if status in TERMINAL_FAILURE:
            await increment_retry(str(path))

    await submit_file(path, source_type, digest)


async def scan_once() -> None:
    for base, source_type in ((Path(settings.source_docs_dir), "filesystem"), (Path(settings.uploads_dir), "upload")):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in SUPPORTED:
                try:
                    await handle_candidate(path, source_type, base)
                except Exception as exc:
                    existing = await get_document_by_path(str(path))
                    if existing:
                        await update_status(str(path), "failed", str(exc))
                    else:
                        await upsert_document(
                            document_id=str(uuid.uuid4()),
                            path=str(path),
                            source_type=source_type,
                            company=None,
                            filename=path.name,
                            sha256="error",
                            status="failed",
                            track_id=None,
                            last_error=str(exc),
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
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
