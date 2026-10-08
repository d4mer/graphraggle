#!/usr/bin/env python3
"""Estimate the gateway-controllable size of every LLM prompt this repo builds.

Throwaway measurement helper for qitem-20261008185225-b0661b3d (why do some
prompts reach 45k-80k tokens?). NOT wired into the app: nothing imports it.

Usage (app.config requires the six mandatory settings; dummy values are fine,
no services are contacted):

    RAG_API_KEY=x LIGHTRAG_INTERNAL_API_KEY=x LIGHTRAG_BASE_URL=http://x \
    SOURCE_DOCS_DIR=/tmp UPLOADS_DIR=/tmp STATE_DB_PATH=/tmp/st.db \
    .venv/bin/python scripts/prompt_size_estimate.py

Method and limits:
  * Token estimate is chars/4 (stated in the brief). It is a rough proxy; the
    real tokenizer is oMLX's and is not available offline.
  * Only the GATEWAY-side contribution is measured. Anything LightRAG adds
    (its prompt templates, its own truncation of retrieved context) is marked
    "LIGHTRAG-INTERNAL" and not guessed at.
  * "UNBOUNDED" means: no cap exists anywhere in this repo on that input.
"""

from __future__ import annotations

import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import generation, graph_synthesis, graphrag, keywords, rewrite
from app.api import (
    MAX_HISTORY_MESSAGE_CHARS,
    build_ollama_bridge_query_payload,
    extract_ollama_history,
)
from app.config import settings

TOK = 4  # chars per token, rough


def tok(n_chars: float) -> int:
    return int(round(n_chars / TOK))


def bridge_direct(history_turns: int, user_msg_chars: int, assistant_msg_chars: int) -> dict[str, Any]:
    """Direct bridge: the production OpenWebUI path (payload app/api.py:214-235, POST 549-552)."""
    messages = []
    for i in range(history_turns * 2):
        role = "user" if i % 2 == 0 else "assistant"
        size = user_msg_chars if role == "user" else assistant_msg_chars
        messages.append({"role": role, "content": "x" * size})
    messages.append({"role": "user", "content": "x" * user_msg_chars})
    history = extract_ollama_history({"messages": messages}, settings.bridge_history_turns)
    payload = build_ollama_bridge_query_payload("x" * user_msg_chars, history)
    hist_chars = sum(len(h["content"]) for h in history)
    return {
        "path": "bridge direct /query/stream (api.py:549-552)",
        "gateway_chars": hist_chars + len(payload["query"]) + len(payload["user_prompt"]),
        "retrieval_cap_tokens": settings.bridge_max_total_tokens,
        "verdict": "capped: max_total_tokens=%d + history <= %d msgs x %d chars"
        % (settings.bridge_max_total_tokens, settings.bridge_history_turns * 2, MAX_HISTORY_MESSAGE_CHARS),
    }


def bridge_task(task_prompt_chars: int) -> dict[str, Any]:
    """Task short-circuit (app/api.py:446-455): bypass, no caps at all."""
    return {
        "path": "bridge task short-circuit /query/stream (api.py:446-455)",
        "gateway_chars": task_prompt_chars,
        "retrieval_cap_tokens": None,
        "verdict": "UNBOUNDED: mode=bypass sends the prompt verbatim; no token field in the payload",
    }


def gateway_query(query_chars: int) -> dict[str, Any]:
    """Gateway /query pass (app/api.py:1234-1248)."""
    payload = {
        "query": "x" * query_chars,
        "mode": settings.retrieval_mode_default,
        "top_k": 12,
        "chunk_top_k": settings.chunk_top_k,
        "include_references": True,
        "include_chunk_content": True,
    }
    caps = [k for k in ("max_entity_tokens", "max_relation_tokens", "max_total_tokens") if k in payload]
    return {
        "path": "gateway /query pass (api.py:1234-1248)",
        "gateway_chars": len(payload["query"]),
        "retrieval_cap_tokens": None,
        "verdict": "NO CAP SENT: %s absent -> LightRAG container defaults decide (LIGHTRAG-INTERNAL)" % (caps or "max_*_tokens"),
    }


def generate_document(ref_count: int, chunk_chars: int) -> dict[str, Any]:
    """generate_document (app/generation.py:16-46): refs joined with no cap.

    ref_count/chunk_chars are illustrative inputs, not measured constants:
    LightRAG decides how many references come back and how big each chunk is
    (LIGHTRAG-INTERNAL). The gateway-side defect is that it concatenates all
    of them with no cap.
    """
    context_chars = ref_count * chunk_chars
    prompt_chars = len(generation.PROMPTS["report"]) + context_chars + 60
    return {
        "path": "generate_document LLM call (generation.py:40-46)",
        "gateway_chars": prompt_chars,
        "retrieval_cap_tokens": None,
        "verdict": "UNBOUNDED: every reference's full chunk content is concatenated (generation.py:26-31); no cap, mode=bypass",
    }


def keyword_call(query_chars: int) -> dict[str, Any]:
    p = keywords.build_keyword_prompt("x" * query_chars)
    return {
        "path": "bridge keyword call (keywords.py:410-418, direct to LLM)",
        "gateway_chars": len(p),
        "retrieval_cap_tokens": None,
        "verdict": "fixed template (%d chars) + query; max_tokens=%d out" % (len(keywords.KEYWORDS_EXTRACTION_TEMPLATE), 2000),
    }


def rewrite_call(latest_chars: int, turns: int) -> dict[str, Any]:
    view = [
        {"role": "user" if i % 2 == 0 else "assistant",
         "content": "x" * (settings.bridge_rewrite_max_user_chars if i % 2 == 0
                           else settings.bridge_rewrite_max_assistant_chars)}
        for i in range(turns * 2)
    ]
    p = rewrite.build_rewrite_prompt("x" * latest_chars, view)
    return {
        "path": "bridge rewrite call (rewrite.py:331-337, direct to LLM)",
        "gateway_chars": len(p),
        "retrieval_cap_tokens": None,
        "verdict": "history capped (%d x %d/%d chars); 'latest' message is UNCAPPED (rewrite.py:190)"
        % (settings.bridge_rewrite_turns, settings.bridge_rewrite_max_user_chars,
           settings.bridge_rewrite_max_assistant_chars),
    }


def graph_entity_extraction(seed_citations: int) -> dict[str, Any]:
    citations = [{"content": "x" * 2000, "file_path": "p"} for _ in range(seed_citations)]
    texts = [graphrag.citation_to_text_for_entity_extraction(c) for c in citations]
    prompt = graphrag._ENTITY_EXTRACTION_PROMPT.format(query="x" * 200, text="\n\n---\n\n".join(texts))
    return {
        "path": "graph expansion entity extraction (graphrag.py:107-116)",
        "gateway_chars": len(prompt),
        "retrieval_cap_tokens": None,
        "verdict": "capped: %d seeds x 2000 chars (graphrag.py:52)" % seed_citations,
    }


def synthesis_prompt(query_chars: int) -> dict[str, Any]:
    evidence = [{"kind": "vector", "evidence": {"path": "p", "content": "x" * 5000}}
                for _ in range(10)]
    p = graph_synthesis.build_graph_aware_prompt("x" * query_chars, evidence)
    return {
        "path": "graph synthesis (graph_synthesis.py:58-67)",
        "gateway_chars": len(p),
        "retrieval_cap_tokens": None,
        "verdict": "capped: %d items, vector content %d chars (graph_synthesis.py:12-13)"
        % (graph_synthesis.MAX_SYNTHESIS_EVIDENCE_ITEMS, graph_synthesis.MAX_VECTOR_CONTENT_CHARS),
    }


def mq_rewrite_prompt(query_chars: int) -> dict[str, Any]:
    chars = 200 + query_chars
    return {
        "path": "multi-query rewrite (multi_query.py:203-220)",
        "gateway_chars": chars,
        "retrieval_cap_tokens": None,
        "verdict": "capped by the query itself; mode=bypass top_k=1",
    }


def main() -> int:
    rows = [
        keyword_call(600),
        rewrite_call(600, settings.bridge_rewrite_turns),
        synthesis_prompt(200),
        mq_rewrite_prompt(200),
        graph_entity_extraction(settings.graph_seed_citation_count),
        bridge_direct(settings.bridge_history_turns, 600, 4000),
        gateway_query(600),
        bridge_task(20_000),
        bridge_task(80_000),
        generate_document(40, 4800),
    ]
    print("path | gateway chars | ~tokens (chars/4) | LightRAG retrieval cap sent | verdict")
    print("--- | --- | --- | --- | ---")
    for r in rows:
        cap = r["retrieval_cap_tokens"]
        print("%s | %s | %s | %s | %s" % (
            r["path"],
            f"{r['gateway_chars']:,}",
            f"{tok(r['gateway_chars']):,}",
            cap if cap is not None else "none sent",
            r["verdict"],
        ))
    print()
    print("settings actually used: BRIDGE_TOP_K=%d BRIDGE_CHUNK_TOP_K=%d BRIDGE_MAX_ENTITY_TOKENS=%d "
          "BRIDGE_MAX_RELATION_TOKENS=%d BRIDGE_MAX_TOTAL_TOKENS=%d BRIDGE_HISTORY_TURNS=%d "
          "GATEWAY_CHUNK_TOP_K=%d RETRIEVAL_MODE_DEFAULT=%s" % (
              settings.bridge_top_k, settings.bridge_chunk_top_k, settings.bridge_max_entity_tokens,
              settings.bridge_max_relation_tokens, settings.bridge_max_total_tokens,
              settings.bridge_history_turns, settings.chunk_top_k, settings.retrieval_mode_default))
    print()
    print("Note: 'gateway chars' is only what THIS repo puts in the prompt. LightRAG adds its own")
    print("template plus the retrieved context (bounded by max_total_tokens when sent). Anything not")
    print("listed here is LIGHTRAG-INTERNAL and was not measured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
