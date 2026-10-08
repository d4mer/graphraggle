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
    # Cap on the OpenWebUI task prompt the bridge forwards (title/tag/
    # follow-up generation). Those prompts embed the whole chat, and they go
    # upstream in mode=bypass where no retrieval truncation runs. 24000 chars
    # is ~6k tokens; the cap protects the LLM prefix cache and decode speed.
    # 0 disables the cap.
    bridge_task_max_chars: int = Field(default=24000, alias="BRIDGE_TASK_MAX_CHARS")
    # Append a markdown Sources block to direct-bridge answers.
    bridge_sources_enabled: bool = Field(default=True, alias="BRIDGE_SOURCES_ENABLED")
    # Strip complete reasoning regions (recognized boundary pairs only) from
    # aggregated /query/stream text. qwen3.6-35b on the MLX server emits its
    # chain-of-thought inline in the streamed response; the non-stream path
    # does not carry it. Orphan markers are never stripped.
    bridge_strip_reasoning: bool = Field(default=True, alias="BRIDGE_STRIP_REASONING")
    # Extra attempts after the first /query/stream call when it comes back
    # HTTP 200 with an empty answer (oMLX early stop). 0 disables retrying.
    bridge_empty_retries: int = Field(default=1, alias="BRIDGE_EMPTY_RETRIES")
    # When false, LightRAG's own grounded answer is never replaced by the
    # graph-synthesis pass. Re-enabled in Stage 6 once that prompt carries
    # real edge content instead of node counts.
    graph_synthesis_replace_answer: bool = Field(
        default=False, alias="GRAPH_SYNTHESIS_REPLACE_ANSWER"
    )

    # ── Issue-08 keyword fix: gateway supplies the bridge keywords ──────
    # Master switch. Off means today's behaviour exactly (LightRAG extracts
    # its own keywords). BRIDGE_KEYWORD_* names cannot collide with LightRAG
    # variables (LightRAG's keyword call has no env knobs).
    bridge_keyword_supply: bool = Field(default=True, alias="BRIDGE_KEYWORD_SUPPLY")
    # Extra attempts after the first rejected/failed keyword call.
    bridge_keyword_retries: int = Field(default=2, alias="BRIDGE_KEYWORD_RETRIES")
    bridge_keyword_timeout: int = Field(default=120, alias="BRIDGE_KEYWORD_TIMEOUT")
    bridge_keyword_max_items: int = Field(default=10, alias="BRIDGE_KEYWORD_MAX_ITEMS")
    # Optional overrides; when empty the keyword call falls back to the same
    # LLM_BINDING_HOST / LLM_MODEL / LLM_BINDING_API_KEY values LightRAG is
    # configured with (already in the shared .env; never logged).
    bridge_keyword_llm_base_url: Optional[str] = Field(
        default=None, alias="BRIDGE_KEYWORD_LLM_BASE_URL")
    bridge_keyword_llm_model: Optional[str] = Field(
        default=None, alias="BRIDGE_KEYWORD_LLM_MODEL")
    bridge_keyword_llm_api_key: Optional[str] = Field(
        default=None, alias="BRIDGE_KEYWORD_LLM_API_KEY")
    # Per-request thinking switch for the keyword call only (see report for
    # what oMLX honours). Off by default; never touches thinking elsewhere.
    bridge_keyword_no_think: bool = Field(default=False, alias="BRIDGE_KEYWORD_NO_THINK")

    # ── GRAG-41: rewrite follow-ups into standalone retrieval queries ──
    # Master switch. Off means today's behaviour exactly (retrieve on the
    # literal last user message). BRIDGE_REWRITE_* names cannot collide with
    # LightRAG variables (LightRAG has no rewrite knobs).
    bridge_rewrite_enabled: bool = Field(default=True, alias="BRIDGE_REWRITE_ENABLED")
    # Prior exchanges (user+assistant pairs) shown to the rewrite call.
    bridge_rewrite_turns: int = Field(default=3, alias="BRIDGE_REWRITE_TURNS")
    # Extra attempts after the first rejected/failed rewrite call.
    bridge_rewrite_retries: int = Field(default=1, alias="BRIDGE_REWRITE_RETRIES")
    bridge_rewrite_timeout: int = Field(default=120, alias="BRIDGE_REWRITE_TIMEOUT")
    # Assistant answers run 500+ words and swamp the rewrite prompt; cap
    # them hard. User turns are short already; cap is a guard.
    bridge_rewrite_max_assistant_chars: int = Field(
        default=1200, alias="BRIDGE_REWRITE_MAX_ASSISTANT_CHARS")
    bridge_rewrite_max_user_chars: int = Field(
        default=600, alias="BRIDGE_REWRITE_MAX_USER_CHARS")
    # Per-request thinking switch for the rewrite call only (same finding as
    # the keyword step: oMLX honours it per-request). Off by default.
    bridge_rewrite_no_think: bool = Field(default=False, alias="BRIDGE_REWRITE_NO_THINK")
    # Model/URL/key: deliberately no new settings - the rewrite call reuses
    # the BRIDGE_KEYWORD_LLM_* overrides and LLM_BINDING_* defaults above.
    # Fallback sources: the LightRAG LLM endpoint the keyword call reuses.
    llm_binding_host: Optional[str] = Field(default=None, alias="LLM_BINDING_HOST")
    llm_model: Optional[str] = Field(default=None, alias="LLM_MODEL")
    llm_binding_api_key: Optional[str] = Field(default=None, alias="LLM_BINDING_API_KEY")


settings = Settings()
