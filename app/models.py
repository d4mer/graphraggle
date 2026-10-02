from typing import Any, Literal

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
    chunk_top_k: int | None = None
    mode: Literal["mix", "hybrid", "local", "global", "naive", "bypass"] | None = None
    thread_id: str | None = None
    # Prior turns forwarded to LightRAG (LLM context only; LightRAG v1.4.15
    # and v1.5.7 have no history_turns field, so turn capping is gateway-side).
    conversation_history: list[dict[str, str]] | None = None


class GenerateDocumentRequest(BaseModel):
    query: str
    document_type: Literal["summary", "memo", "report", "proposal", "policy", "brief", "draft"]
    company: str | None = None
    style: str | None = None
    thread_id: str | None = None


class ReindexRequest(BaseModel):
    document_id: str | None = None
    path: str | None = None
    status: str | None = None
    validation_state: str | None = None
    company: str | None = None
    source_type: Literal["upload", "filesystem"] | None = None
    force: bool = False
