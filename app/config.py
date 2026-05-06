from __future__ import annotations

from typing import Optional

from pydantic import Field
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

    rerank_enabled: bool = Field(default=False, alias="RERANK_ENABLED")
    rerank_binding_host: Optional[str] = Field(default=None, alias="RERANK_BINDING_HOST")

    multi_query_enabled: bool = Field(default=False, alias="MULTI_QUERY_ENABLED")
    multi_query_long_query_words: int = Field(default=20, alias="MULTI_QUERY_LONG_QUERY_WORDS")
    multi_query_rewrite_count: int = Field(default=2, alias="MULTI_QUERY_REWRITE_COUNT")
    multi_query_transcript_keywords: str = Field(
        default="transcript,workshop,speaker,meeting minutes,recording",
        alias="MULTI_QUERY_TRANSCRIPT_KEYWORDS",
    )


settings = Settings()