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
    # Basenames the scanner never ingests, comma-separated, matched on the whole
    # basename case-insensitively. The default exists because of a measured
    # failure: every synthetic company folder carries a manifest.json written by
    # the test harness, `.json` is in SUPPORTED/TEXT_EXTENSIONS, so the worker
    # submitted it and LightRAG failed it on chunk size in every company folder
    # (GL-07/08 on stack2, 2026-11-12). A refused file gets a terminal `skipped`
    # row naming the rule; it is never silently invisible, and it is never a
    # failure row. This names FILES, not extensions - a real orders.json, or any
    # file whose name merely contains "manifest", still ingests. Empty disables.
    ingest_skip_basenames: str = Field(default="manifest.json",
                                       alias="INGEST_SKIP_BASENAMES")
    request_timeout_seconds: int = Field(default=300, alias="REQUEST_TIMEOUT_SECONDS")

    # Company-aware duplicate selection. Default false = shipped behaviour is
    # byte-identical to the company-blind picker: SHA matches suppress a new
    # document whichever company owns them. Set true to let a company-scoped
    # document through when the only SHA match belongs to another company (that
    # company can never cite it, so the upload was silently lost). Kept dark
    # because what LightRAG does with a second submit of identical bytes is not
    # established offline; the operator enables it after the live probe. The
    # poll_track file_path guard only ever runs for rows this flag marked.
    company_aware_dedupe: bool = Field(default=False, alias="COMPANY_AWARE_DEDUPE")

    # GL-10: the company named in the QUESTION scopes the search and the answer. Default OFF, and
    # with it off no part of the query path looks at the question text for a company, so the shipped
    # envelope and the query log line are what they were. The operator turns it on when the stack2
    # probe plan in docs/ops/probes has been run.
    company_from_question: bool = Field(default=False, alias="COMPANY_FROM_QUESTION")

    # What the operator types -> the company name the store holds, because a folder name is rarely
    # what a person asks with: `Northwind Chemicals=nwind,north wind;Contoso Pharma=contoso`.
    # Aliases naming a company with no rows are ignored, so a typo here cannot scope a query to a
    # company that has no documents (app/company_scope.validate_aliases).
    company_aliases: str = Field(default="", alias="COMPANY_ALIASES")

    # GL-10 part 3: how far the question-named company reaches. `citations` (the default) is part
    # 2: the returned Sources are filtered and the answer is whatever LightRAG produced. `retrieval`
    # answers from the named companies' material only: retrieve, drop every reference whose document
    # row is not one of those companies, and generate again from what is left (app/scoped_pass.py).
    # `retrieval` costs a second model call and, on the bridge, the token stream - which is exactly
    # why it is an explicit value rather than a side effect of the flag above.
    company_scope_enforcement: str = Field(default="citations", alias="COMPANY_SCOPE_ENFORCEMENT")

    # Ceiling on the scoped pass's prompt, in characters, whole references only. Measured prompt
    # pressure on this stack (docs/ops/runs/prompt-size-investigation.md) is the reason a cap exists
    # at all; dropping a whole reference rather than cutting one keeps a citation in the prompt
    # complete or absent.
    company_scoped_context_max_chars: int = Field(default=12000,
                                                  alias="COMPANY_SCOPED_CONTEXT_MAX_CHARS")

    # GL-10: over-fetch the scoped retrieval. LightRAG ranks over ALL companies and the pass then
    # throws most of what came back away (other company, unattributable, ambiguous, cap). On stack2
    # cutover-9 that was 246 + 90 + 100 + 116 of 1056 references: the named company's answer chunk is
    # often not in the survivors, and since the refusal prompt landed an honest model refuses 21
    # answerable questions where it used to guess. The lever is recall BEFORE the filter, so ask for
    # `multiplier` x the door's top_k/chunk_top_k, never above COMPANY_SCOPED_RETRIEVAL_CEILING. The
    # context cap is untouched and stays the only limit on what the model sees.
    # Default 1 = unchanged shipped behaviour, until Hermes measures it on stack2.
    company_scoped_retrieval_multiplier: int = Field(
        default=1, alias="COMPANY_SCOPED_RETRIEVAL_MULTIPLIER")
    company_scoped_retrieval_ceiling: int = Field(
        default=200, alias="COMPANY_SCOPED_RETRIEVAL_CEILING")

    # GL-10: if the kept, attributable set is thinner than this, retrieve ONCE more at double the
    # multiplier before generating. Counts blocks that actually reached the prompt, not references
    # kept, because a block with no chunk text cannot answer anything. 0 disables the second round, and
    # the round never runs at MULTIPLIER=1: the second retrieval belongs to the over-fetch lever, so
    # flag-off behaviour stays one retrieval and one generation per row.
    company_scoped_retrieval_floor: int = Field(
        default=3, alias="COMPANY_SCOPED_RETRIEVAL_FLOOR")

    # GL-10: when the question names an identifier (a code like NC-3617) and that identifier appears
    # nowhere in the material the scoped pass chose to send, refuse instead of generating. This is the
    # one unanswerable case the gateway can decide for itself; the matcher is the shipped
    # app/grounding.py identifier/verbatim-token rule, not a new one. Off by default because the M1
    # investigation measured this classifier's FALSE-REFUSAL risk only on synthetic probes, never on a
    # dev set of answerable questions - measure it on stack2 (the log carries
    # `scoped_pass_hard_refusal_verdict`) before trusting it on answers people read.
    company_scoped_hard_refusal: bool = Field(default=False, alias="COMPANY_SCOPED_HARD_REFUSAL")

    # GL-10: name the scoped companies in the retrieval keywords (`hl_keywords`/`ll_keywords`) so
    # similar chunks from other companies are less likely to fill the pool. A bias, not a boundary:
    # the state-join filter is what excludes, and the guarantee does not depend on this. Off by
    # default so a run can measure whether the bias is worth its effect on recall.
    company_keyword_hint: bool = Field(default=False, alias="COMPANY_KEYWORD_HINT")

    # GL-10: submit a company-qualified `file_source` ("<company-slug>--<basename>") for new ingests so
    # the name LightRAG keeps is unique per company, and record that exact name in `documents.
    # submitted_source` (the join prefers it). Off by default: turning it on is a decision about future
    # ingests, not a switch that changes existing documents - re-submitting an existing document under a
    # new name creates a second LightRAG document, so this is applied to new files, not flipped over an
    # already-indexed corpus.
    company_qualified_file_source: bool = Field(default=False, alias="COMPANY_QUALIFIED_FILE_SOURCE")

    # packet-27: the worker extracts .pptx itself (python-pptx) into structured
    # text and submits it through /documents/text, because LightRAG's built-in
    # pptx extractor drops tables, grouped shapes, speaker notes and slide
    # numbers. Set false to fall back to the old upload path.
    worker_pptx_extract_enabled: bool = Field(default=True, alias="WORKER_PPTX_EXTRACT_ENABLED")
    # python-pptx reads the whole package into memory, so this byte limit is the
    # guard against the zip-bomb class of files.
    worker_pptx_max_bytes: int = Field(default=104_857_600, alias="WORKER_PPTX_MAX_BYTES")
    # Extracted text is capped before submission; the cap leaves a marker line.
    worker_pptx_max_chars: int = Field(default=2_000_000, alias="WORKER_PPTX_MAX_CHARS")

    # PDF and DOCX take the same route, but only while structure chunking is on: with
    # STRUCTURE_CHUNKING_ENABLED off they keep going to /documents/upload exactly as they
    # do today, because extracting them and letting LightRAG re-cut the text would trade one
    # invisible cut for another. Extraction is per-type killable so a bad parser cannot take
    # the whole ingest down. Any extraction failure falls back to the upload route: a
    # document is never lost to a parser.
    worker_pdf_extract_enabled: bool = Field(default=False, alias="WORKER_PDF_EXTRACT_ENABLED")
    worker_docx_extract_enabled: bool = Field(default=False, alias="WORKER_DOCX_EXTRACT_ENABLED")
    # pypdf and python-docx both hold the parsed document in memory; the byte limit is the
    # guard for the zip-bomb / huge-scan class, and the char cap bounds what is submitted.
    worker_pdf_max_bytes: int = Field(default=104_857_600, alias="WORKER_PDF_MAX_BYTES")
    # Which PDF text extractor to use: "auto" prefers poppler's pdftotext -layout (already in
    # the worker image, and it reproduces column positions as spaces, which is what the row
    # reassembly reads), then pypdf. Pin one for a reproducible experiment.
    worker_pdf_text_backend: str = Field(default="auto", alias="WORKER_PDF_TEXT_BACKEND")
    worker_pdf_max_chars: int = Field(default=2_000_000, alias="WORKER_PDF_MAX_CHARS")
    worker_docx_max_bytes: int = Field(default=104_857_600, alias="WORKER_DOCX_MAX_BYTES")
    worker_docx_max_chars: int = Field(default=2_000_000, alias="WORKER_DOCX_MAX_CHARS")

    # Contextual chunk headers + speaker-turn normalisation for transcript-shaped
    # text, applied to the /documents/text payload before LightRAG ever sees it
    # (app/transcript_prep.py). LightRAG cuts fixed-size token chunks, so a
    # transcript's later chunks lose the meeting's title, date and turn structure;
    # this puts a one-line context header every ~N tokens into the TEXT itself.
    # Default false: the submitted payload is byte-for-byte what it is today.
    # The dedupe content hash is always computed on the ORIGINAL file bytes, never
    # on prepared text, so preparation can never change a document's identity.
    transcript_prep_enabled: bool = Field(default=False, alias="TRANSCRIPT_PREP_ENABLED")
    # Rough token distance between context headers. Production LightRAG runs at its
    # default 1200-token chunks (100 overlap), so 200 puts several headers inside
    # every chunk LightRAG produces. Tokens are approximated by
    # app/transcript_prep.py (~4 chars/token) because tiktoken lives in LightRAG's
    # image, not this venv; the 6x margin below the chunk size absorbs the error.
    transcript_prep_header_every_tokens: int = Field(
        default=200, alias="TRANSCRIPT_PREP_HEADER_EVERY_TOKENS"
    )

    # Structure-aware pre-chunking (app/structure_chunker.py): our code chooses every
    # cut, and the request tells LightRAG to cut ONLY at those markers (fixed_token +
    # split_by_character_only). Default false, exactly like TRANSCRIPT_PREP_ENABLED:
    # with it off the submitted text and request body are what they are today. The
    # dedupe content hash is still taken from the original file bytes, so chunking
    # can never change a document's identity.
    structure_chunking_enabled: bool = Field(default=False,
                                             alias="STRUCTURE_CHUNKING_ENABLED")
    # The window, not a budget: a natural unit (a section, a table, a run of turns) is
    # cut ONLY when it cannot fit this. 7500 = BGE-M3's 8192-token window less a safety
    # margin, expressed in app/structure_chunker.py's conservative estimate, which
    # over-counts English prose by ~10-15% and therefore errs small. There is no
    # chunk-size target any more: the document decides how big a piece is.
    structure_chunking_max_piece_tokens: int = Field(
        default=7500, alias="STRUCTURE_CHUNKING_MAX_PIECE_TOKENS")
    # The merge floor. A unit below this is welded to its neighbour, so a page of
    # numbered sub-headings becomes a few readable pieces instead of a hundred
    # fragments. It is a floor, never a cap: nothing is cut for being large until it
    # cannot fit the window above.
    structure_chunking_min_piece_tokens: int = Field(
        default=150, alias="STRUCTURE_CHUNKING_MIN_PIECE_TOKENS")
    # How far a merge chain runs. "floor" (default) stops it once the piece clears the
    # floor, so a piece stays a natural unit; "window" lets unrelated short sections chain
    # into one big piece. A section whose whole extent fits the window is one unit either way.
    structure_chunking_merge_until: str = Field(
        default="floor", alias="STRUCTURE_CHUNKING_MERGE_UNTIL")

    # Topic-shift segmentation for documents with no headings and no tables (the flat
    # transcripts, the "(Name)"-label meetings, the monolithic single-line minutes). The
    # chunker asks an injected embedder where adjacent windows stop resembling each other
    # and cuts there, so a piece is one topic of whatever length that topic happens to be.
    # With no URL configured there is no embedder and the deterministic fallback is used -
    # cut at unit boundaries near the window - and the state row says which happened.
    structure_chunking_embed_url: str = Field(default="", alias="STRUCTURE_CHUNKING_EMBED_URL")
    structure_chunking_embed_model: str = Field(
        default="bge-m3-mlx-fp16", alias="STRUCTURE_CHUNKING_EMBED_MODEL")
    # The NAME of the environment variable holding the model server key. The key itself is
    # never in a setting, a log line or a report.
    structure_chunking_embed_api_key_env: str = Field(
        default="OMLX_API_KEY", alias="STRUCTURE_CHUNKING_EMBED_API_KEY_ENV")
    structure_chunking_embed_timeout_seconds: float = Field(
        default=15.0, alias="STRUCTURE_CHUNKING_EMBED_TIMEOUT_SECONDS")
    structure_chunking_embed_batch: int = Field(default=16,
                                                alias="STRUCTURE_CHUNKING_EMBED_BATCH")
    # Similarity drop (TextTiling depth) needed to call a place a topic change. Lower cuts
    # more often. 0.30 is a starting point measured on synthetic vocabularies, NOT on the
    # corpus: the first real run should print the depth distribution before moving it.
    structure_chunking_topic_threshold: float = Field(
        default=0.30, alias="STRUCTURE_CHUNKING_TOPIC_THRESHOLD")
    structure_chunking_topic_units_per_window: int = Field(
        default=6, alias="STRUCTURE_CHUNKING_TOPIC_UNITS_PER_WINDOW")
    structure_chunking_topic_max_windows: int = Field(
        default=400, alias="STRUCTURE_CHUNKING_TOPIC_MAX_WINDOWS")
    # The context header is capped separately so a long section path can never eat the
    # body's room: fields are dropped (path, date, speakers, title last) until it fits.
    structure_chunking_header_tokens: int = Field(
        default=25, alias="STRUCTURE_CHUNKING_HEADER_TOKENS")
    structure_chunking_headers: bool = Field(default=True,
                                             alias="STRUCTURE_CHUNKING_HEADERS")

    # The fit check: the served embedder's real window, in ITS tokens.
    # chunk_token_size in the request must be at least the largest piece, or LightRAG is
    # allowed to re-cut our pieces and the whole exercise means nothing. The margin absorbs
    # the difference between our conservative estimate and the server's tokenizer.
    structure_chunking_fit_margin_tokens: int = Field(
        default=64, alias="STRUCTURE_CHUNKING_FIT_MARGIN_TOKENS")
    # BGE-M3's window (8192 tokens, dense 1024-dim, served by oMLX). With no tokenizer
    # files configured the check runs on the chunker's own conservative over-count.
    structure_chunking_embed_window_tokens: int = Field(
        default=8192, alias="STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS")
    # Optional local directory of the SERVED embedder's tokenizer (tokenizer.json or
    # vocab.txt). Never fetched: app/embed_tokenizer.py refuses a hub id. When it is set,
    # the fit check is stated in the embedder's own tokens instead of cl100k-style ones.
    structure_chunking_embed_tokenizer_path: str = Field(
        default="", alias="STRUCTURE_CHUNKING_EMBED_TOKENIZER_PATH")
    # Make a coverage gap (the chunker failed to account for words that were in the source)
    # fall back to unchunked submission instead of only recording it. Default false: the gap
    # is a bug signal, and blocking ingest on a heuristic is a worse outcome than the bug.
    structure_chunking_fail_on_lost_text: bool = Field(
        default=False, alias="STRUCTURE_CHUNKING_FAIL_ON_LOST_TEXT")
    # Relative safety factor on the chunk_token_size we ask LightRAG to enforce:
    # size = ceil(largest_piece_in_our_units * SAFETY) + FIT_MARGIN. It exists because the
    # counter that prices a piece (``conservative-overcount``, since tiktoken is not in
    # requirements.txt) UNDER-counts the classes a PDF extraction produces. Measured against
    # a cl100k pre-tokenizer LOWER bound (tests/test_pdf_chunk_token_overflow.py): prose 0.62x
    # (safe), JSON-shaped manifest text 1.04x, code/mixed 1.55x, PDF pipe tables 1.94x, dotted
    # number runs 3.00x; the two stack2 rejections were at 1.21x and 1.145x. 3.2 is the largest
    # measured ratio plus headroom - and because those ratios are LOWER bounds, it is a bound of
    # experience, not a proof. Counting in LightRAG's own tokenizer family is the real fix; this
    # is the interim. It changes no piece boundary: it only moves the acceptance threshold.
    # Values below 1.0 are clamped to 1.0, which is the pre-factor behaviour.
    structure_chunking_chunk_token_safety: float = Field(
        default=3.2, alias="STRUCTURE_CHUNKING_CHUNK_TOKEN_SAFETY")
    # Never ask LightRAG to enforce a chunk larger than this, in our counter's units. 0 disables
    # the cap. Default equals STRUCTURE_CHUNKING_EMBED_WINDOW_TOKENS: a limit above the served
    # embedder's window would only mean "ingest it and let the embedder truncate it silently".
    # The cap can never push the number BELOW largest+FIT_MARGIN (the pre-factor value), so a
    # misconfigured ceiling cannot introduce rejections that did not already exist; when the cap
    # binds, structure_chunking_chunk_token_capped=true says so on the row.
    structure_chunking_chunk_token_ceiling: int = Field(
        default=8192, alias="STRUCTURE_CHUNKING_CHUNK_TOKEN_CEILING")

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
    # M1 from the GL-08 unanswerable-probe investigation: record whether the identifier the
    # QUESTION named appears verbatim (token match) in the evidence the answer was written
    # from. FLAG ONLY - it never changes the answer, the citations or the status code, and it
    # ships off, in which case the response and the upstream payload are exactly what they are
    # today. A measured run on the probe sets decides whether it ever acts on the verdict.
    grounding_check_asked_identifier: bool = Field(
        default=False, alias="GROUNDING_CHECK_ASKED_IDENTIFIER")
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
    # upstream in mode=bypass where no retrieval truncation runs. 0 = off
    # (shipped default); set e.g. 24000 to enable, which is ~6k tokens and
    # protects the LLM prefix cache and decode speed. The prompt_chars /
    # forwarded_chars log fields are on regardless.
    bridge_task_max_chars: int = Field(default=0, alias="BRIDGE_TASK_MAX_CHARS")
    # Append a markdown Sources block to direct-bridge answers.
    bridge_sources_enabled: bool = Field(default=True, alias="BRIDGE_SOURCES_ENABLED")
    # ── OpenAI-compatible door citations (the Open WebUI "N Sources" chip) ──
    # The gateway's second door (GET /v1/models, POST /v1/chat/completions) is
    # the only Open WebUI path that can carry structured citations; the Ollama
    # door's answers are byte-identical either way. Ships OFF: with this off the
    # door still answers, it just carries no annotations, and the text Sources
    # block stays in the body. Setting it on changes only what the client is
    # handed, never retrieval or generation.
    bridge_citations_enabled: bool = Field(default=False, alias="BRIDGE_CITATIONS_ENABLED")
    # Required before annotations are worth emitting. Open WebUI keys one
    # citation chip per distinct url (0.12.0 Citations.svelte dedupes on it, so
    # an empty or repeated url collapses several documents into a single chip),
    # and the door cannot mint a url without a base - with none set it emits no
    # annotations and the text Sources block stays. No host is hardcoded here:
    # whatever the operator's LAN serves is what this must point at. Each
    # document's url is "<base>/<urlencoded label>", and the label is what the
    # operator reads - note Open WebUI shows the url as the chip label when the
    # url starts with http, so an http base makes the chips read as URLs.
    # Set this to the surface where the operator can actually look the document
    # up; a link is only rendered when the url is a valid http url.
    bridge_citation_base_url: str = Field(default="", alias="BRIDGE_CITATION_BASE_URL")
    # Cap on annotations per answer (chips). 0 = no cap. References are deduped
    # by document name first, the same way the Sources block dedupes them.
    bridge_citation_limit: int = Field(default=12, alias="BRIDGE_CITATION_LIMIT")
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
