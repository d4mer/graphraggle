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
    rerank_binding_api_key: Optional[str] = Field(default=None, alias="RERANK_BINDING_API_KEY")
    rerank_model: Optional[str] = Field(default=None, alias="RERANK_MODEL")

    multi_query_enabled: bool = Field(default=False, alias="MULTI_QUERY_ENABLED")
    multi_query_long_query_words: int = Field(default=20, alias="MULTI_QUERY_LONG_QUERY_WORDS")
    multi_query_rewrite_count: int = Field(default=2, alias="MULTI_QUERY_REWRITE_COUNT")
    multi_query_transcript_keywords: str = Field(
        default="transcript,workshop,speaker,meeting minutes,recording",
        alias="MULTI_QUERY_TRANSCRIPT_KEYWORDS",
    )

    graph_expansion_enabled: bool = Field(default=False, alias="GRAPH_EXPANSION_ENABLED")
    graph_expansion_hops: int = Field(default=1, alias="GRAPH_EXPANSION_HOPS")
    graph_expansion_max_neighbors: int = Field(default=10, alias="GRAPH_EXPANSION_MAX_NEIGHBORS")
    graph_seed_citation_count: int = Field(default=3, alias="GRAPH_SEED_CITATION_COUNT")

    graph_native_enabled: bool = Field(default=False, alias="GRAPH_NATIVE_ENABLED")
    graph_native_max_seeds: int = Field(default=3, alias="GRAPH_NATIVE_MAX_SEEDS")
    graph_native_max_depth: int = Field(default=2, alias="GRAPH_NATIVE_MAX_DEPTH")
    graph_native_max_nodes: int = Field(default=50, alias="GRAPH_NATIVE_MAX_NODES")

    # ── Stage 0: precision-first retrieval ──────────────────────────────
    # Default retrieval mode for the gateway /query pipeline (not the
    # bridge). "mix" returns KG context *and* raw source chunks; "hybrid"
    # weights context toward entity and relation descriptions.
    retrieval_mode_default: str = Field(default="mix", alias="RETRIEVAL_MODE_DEFAULT")
    # Evidence budget. The Ollama bridge previously hardcoded top_k=4.
    bridge_top_k: int = Field(default=40, alias="BRIDGE_TOP_K")
    # GATEWAY_ prefix: bare CHUNK_TOP_K is LightRAG's own variable (default
    # 20) and the stack shares one .env file, so the gateway alias must not
    # collide with it. Applies to the gateway /query pipeline only.
    chunk_top_k: int = Field(default=16, alias="GATEWAY_CHUNK_TOP_K")
    citation_top_k: int = Field(default=12, alias="CITATION_TOP_K")
    # Number of previous user/assistant exchanges forwarded from the bridge
    # to LightRAG as conversation_history. 0 disables history entirely.
    # LightRAG sends history to the LLM only; it does not affect retrieval.
    bridge_history_turns: int = Field(default=3, alias="BRIDGE_HISTORY_TURNS")

    # ── Bridge backend selection ────────────────────────────────────────
    # "lightrag_direct" = the direct /query/stream bridge (production
    # hot-patch behaviour, the deployed default). "gateway_pipeline" = the
    # Stage 0 bridge through the gateway query pipeline, kept for later
    # comparison.
    bridge_backend: str = Field(default="lightrag_direct", alias="BRIDGE_BACKEND")
    # Direct-bridge retrieval parameters. Defaults equal the production
    # hot-patch values so the deployed payload is unchanged.
    bridge_chunk_top_k: int = Field(default=20, alias="BRIDGE_CHUNK_TOP_K")
    bridge_max_entity_tokens: int = Field(default=10000, alias="BRIDGE_MAX_ENTITY_TOKENS")
    bridge_max_relation_tokens: int = Field(default=10000, alias="BRIDGE_MAX_RELATION_TOKENS")
    bridge_max_total_tokens: int = Field(default=32000, alias="BRIDGE_MAX_TOTAL_TOKENS")
    bridge_enable_rerank: bool = Field(default=True, alias="BRIDGE_ENABLE_RERANK")
    # GRAG-12: short-circuit OpenWebUI "### Task:" prompts with one bypass
    # call instead of running retrieval.
    bridge_task_shortcircuit: bool = Field(default=True, alias="BRIDGE_TASK_SHORTCIRCUIT")
    # Append a markdown Sources block to direct-bridge answers.
    bridge_sources_enabled: bool = Field(default=True, alias="BRIDGE_SOURCES_ENABLED")
    # Strip complete reasoning regions (recognized boundary pairs only) from
    # aggregated /query/stream text. qwen3.6-35b on the MLX server emits its
    # chain-of-thought inline in the streamed response; the non-stream path
    # does not carry it. Orphan markers are never stripped.
    bridge_strip_reasoning: bool = Field(default=True, alias="BRIDGE_STRIP_REASONING")
    # When false, LightRAG's own grounded answer is never replaced by the
    # graph-synthesis pass. Re-enabled in Stage 6 once that prompt carries
    # real edge content instead of node counts.
    graph_synthesis_replace_answer: bool = Field(
        default=False, alias="GRAPH_SYNTHESIS_REPLACE_ANSWER"
    )


settings = Settings()
