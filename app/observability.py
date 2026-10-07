"""Structured per-request logging for the gateway (GRAG-14, Stage 1 adapted).

Two log paths, one JSON line per request, written to stdout so it lands in
container logs, and additionally appended to ``QUERY_LOG_PATH`` when set:

* ``endpoint="bridge"`` — the Ollama-compatible direct bridge
  (``/api/chat`` / ``/api/generate`` for model ``lightrag:latest``). The
  bridge forwards operator-corpus prompts typed by end users, so **the prompt
  text is never logged** — only a short SHA-256 hash of it.
* ``endpoint="query"`` — the gateway ``/query`` pipeline. Per issue-07 the
  query text itself is logged (it is the operator's own corpus), alongside
  the ``query_scope`` metadata the pipeline already computes.

Logging must never break a query: every emit is wrapped and swallowed.

Bridge field meanings:

``prompt_sha256``      first 16 hex chars of sha256(prompt); never the prompt
``task_prompt``        True when the prompt is an OpenWebUI ``### Task:`` call
``history_messages``   conversation_history entries actually sent upstream
``payload_params``     retrieval parameters sent to LightRAG (never the query)
``duration_ms``        whole bridge call, entry to answer ready
``status``             upstream HTTP status, or ``"exception"``
``exception_class``    exception class name when the upstream call failed
``answer_chars_raw``   aggregated stream characters before the reasoning strip
``reasoning_chars_removed``  characters removed by the reasoning strip
``answer_chars_final`` final answer characters (post-strip, incl. Sources)
``answer_words_final`` final answer word count
``reference_count``    LightRAG references returned
``canned_failure``     answer equals LightRAG's canned no-context response
``keyword_source``     keyword step outcome: llm | llm_unwrapped | fallback |
                       rejected | error | off (issue-08 keyword fix)
``keyword_attempts``   keyword LLM attempts made (1 + retries used)
``keyword_failure_reasons``  enum strings only, never payload or query text
``keyword_hl_count`` / ``keyword_ll_count``  supplied keyword list sizes
``keyword_step_ms``    wall time of the whole keyword step
``honest_failure``     user saw the explicit keyword-failure message instead
                       of a canned or blank answer
``rewrite_source``     standalone-query rewrite outcome: rewritten |
                       unchanged | fallback | skipped_no_history | off |
                       error (GRAG-41)
``rewrite_attempts``   rewrite LLM attempts made (1 + retries used)
``rewrite_failure_reasons``  enum strings only, never prompt/history/query text
``rewrite_step_ms``    wall time of the whole rewrite step
``rewrite_len_ratio``  rewritten length / original length, or null
``rewrite_changed``    True when retrieval ran on a rewritten query
``rewrite_ll_seeded``  True when the user's original wording was appended to
                       the supplied ll_keywords after a rewrite (GRAG-41)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

# LightRAG's canned no-context answer, taken from PROMPTS["fail_response"] in
# the installed LightRAG v1.5.7 source (/app/lightrag/prompt.py). Kept as a
# module constant so canned_failure detection is exact, not a guess.
CANNED_FAILURE_RESPONSE = (
    "Sorry, I'm not able to provide an answer to that question.[no-context]"
)


def new_request_id() -> str:
    """Short request id, also returned in the response envelope ``meta``."""
    return uuid.uuid4().hex[:12]


def prompt_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()[:16]


def is_canned_failure(answer: str) -> bool:
    """True when the answer is LightRAG's canned no-context response."""
    if not isinstance(answer, str):
        return False
    return CANNED_FAILURE_RESPONSE in answer or answer.strip() == CANNED_FAILURE_RESPONSE


class StageTimer:
    """Accumulate per-stage wall time for the /query pipeline."""

    def __init__(self) -> None:
        self._started: dict[str, float] = {}
        self.elapsed_ms: dict[str, int] = {}

    def start(self, name: str) -> None:
        self._started[name] = time.perf_counter()

    def stop(self, name: str) -> None:
        t0 = self._started.pop(name, None)
        if t0 is not None:
            self.elapsed_ms[name] = int(round((time.perf_counter() - t0) * 1000))

    def total_ms(self, started: float) -> int:
        return int(round((time.perf_counter() - started) * 1000))


def _emit(line: dict[str, Any]) -> None:
    """Write one JSON line to stdout and, when configured, to QUERY_LOG_PATH.

    Never raises: a failure to log is not a failure to answer.
    """
    try:
        text = json.dumps(line, ensure_ascii=False, default=str)
        sys.stdout.write(text + "\n")
        sys.stdout.flush()
        path = os.environ.get("QUERY_LOG_PATH")
        if path:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text + "\n")
    except Exception:
        pass


def start_bridge_log(
    request_id: str,
    *,
    prompt: str,
    task_prompt: bool,
    history_count: int,
    payload_params: dict[str, Any],
    backend: str = "lightrag_direct",
) -> dict[str, Any]:
    """Open a bridge log record. Call emit_bridge_log to finish it."""
    return {
        "request_id": request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "endpoint": "bridge",
        "backend": backend,
        "task_prompt": task_prompt,
        "history_messages": history_count,
        "prompt_sha256": prompt_sha256(prompt),
        "payload_params": {
            k: v
            for k, v in payload_params.items()
            if k in ("mode", "top_k", "chunk_top_k", "max_entity_tokens",
                     "max_relation_tokens", "max_total_tokens", "enable_rerank",
                     "response_type", "include_references")
        },
        "_t0": time.perf_counter(),
    }


def emit_bridge_log(
    fields: dict[str, Any],
    *,
    status: Any = None,
    exception: BaseException | None = None,
    answer_chars_raw: int = 0,
    reasoning_chars_removed: int = 0,
    answer_chars_final: int = 0,
    answer_words_final: int = 0,
    reference_count: int = 0,
    canned_failure: bool = False,
    honest_failure: bool = False,
    keyword_source: str = "off",
    keyword_attempts: int = 0,
    keyword_failure_reasons: Optional[list] = None,
    keyword_hl_count: int = 0,
    keyword_ll_count: int = 0,
    keyword_step_ms: int = 0,
    rewrite_source: str = "off",
    rewrite_attempts: int = 0,
    rewrite_failure_reasons: Optional[list] = None,
    rewrite_step_ms: int = 0,
    rewrite_len_ratio: Optional[float] = None,
    rewrite_changed: bool = False,
    rewrite_ll_seeded: bool = False,
) -> None:
    """Finish and write a bridge log line. Swallows all logging errors."""
    try:
        line = {k: v for k, v in fields.items() if not k.startswith("_")}
        line["duration_ms"] = int(round((time.perf_counter() - fields.get("_t0", time.perf_counter())) * 1000))
        if exception is not None:
            line["status"] = "exception"
            line["exception_class"] = type(exception).__name__
        else:
            line["status"] = status
        line.update(
            {
                "answer_chars_raw": answer_chars_raw,
                "reasoning_chars_removed": reasoning_chars_removed,
                "answer_chars_final": answer_chars_final,
                "answer_words_final": answer_words_final,
                "reference_count": reference_count,
                "canned_failure": bool(canned_failure),
                # Reasoning-only completion (G3 category 4): the raw stream had
                # text but the stripped answer is empty. ``reasoning_only`` is
                # the stricter case where the strip removed (almost) all of it.
                "answer_empty_after_strip": bool(
                    answer_chars_raw > 0 and answer_chars_final == 0
                ),
                "reasoning_only": bool(
                    answer_chars_raw > 0
                    and answer_chars_final == 0
                    and reasoning_chars_removed > 0
                ),
                # Issue-08 keyword fix. Counts and enum strings only: keyword
                # lists are query-text derivatives and are never logged.
                "honest_failure": bool(honest_failure),
                "keyword_source": keyword_source,
                "keyword_attempts": keyword_attempts,
                "keyword_failure_reasons": list(keyword_failure_reasons or []),
                "keyword_hl_count": keyword_hl_count,
                "keyword_ll_count": keyword_ll_count,
                "keyword_step_ms": keyword_step_ms,
                # GRAG-41 rewrite step: counts/enums/ratio only - the
                # rewritten query, conversation and prompt are never logged.
                "rewrite_source": rewrite_source,
                "rewrite_attempts": rewrite_attempts,
                "rewrite_failure_reasons": list(rewrite_failure_reasons or []),
                "rewrite_step_ms": rewrite_step_ms,
                "rewrite_len_ratio": rewrite_len_ratio,
                "rewrite_changed": bool(rewrite_changed),
                # Boolean only: the seeded wording is prompt text and is
                # never logged.
                "rewrite_ll_seeded": bool(rewrite_ll_seeded),
            }
        )
        _emit(line)
    except Exception:
        pass


def emit_query_log(
    *,
    request_id: str,
    query: str,
    company: str | None,
    mode_requested: str | None,
    mode_used: str,
    top_k: int,
    chunk_top_k: int,
    query_scope: dict[str, Any],
    citations_raw: int,
    citations_merged: int,
    citations_final: int,
    answer: str,
    timer: StageTimer,
    started: float,
    status: Any = 200,
    exception: BaseException | None = None,
) -> None:
    """Write one /query log line from the query_scope metadata already built."""
    try:
        line = {
            "request_id": request_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "endpoint": "query",
            "query": query,
            "company": company,
            "mode_requested": mode_requested,
            "mode_used": mode_used,
            "top_k": top_k,
            "chunk_top_k": chunk_top_k,
            "citations_raw": citations_raw,
            "citations_merged": citations_merged,
            "citations_scoped": query_scope.get("rerank_input_count"),
            "citations_final": citations_final,
            "fallback_used": query_scope.get("fallback_used"),
            "fallback_reason": query_scope.get("fallback_reason"),
            "fallback_answer_used": query_scope.get("fallback_answer_used"),
            "mq_triggered": query_scope.get("mq_triggered"),
            "mq_trigger_reason": query_scope.get("mq_trigger_reason"),
            "mq_rewrite_count": query_scope.get("mq_rewrite_count"),
            "mq_error": query_scope.get("mq_error"),
            "graph_native_route": query_scope.get("graph_native_route"),
            "graph_native_applied": query_scope.get("graph_native_applied"),
            "graph_native_error": query_scope.get("graph_native_error"),
            "graph_seed_labels": query_scope.get("graph_native_seed_labels"),
            "graph_synthesis_applied": query_scope.get("graph_synthesis_applied"),
            "graph_synthesis_error": query_scope.get("graph_synthesis_error"),
            "rerank_applied": query_scope.get("rerank_applied"),
            "rerank_error": query_scope.get("rerank_error"),
            "rerank_input_count": query_scope.get("rerank_input_count"),
            "answer_chars": len(answer or ""),
            "canned_failure": is_canned_failure(answer or ""),
            "latency_ms_total": timer.total_ms(started),
            "latency_ms_by_stage": timer.elapsed_ms,
        }
        if exception is not None:
            line["status"] = "exception"
            line["exception_class"] = type(exception).__name__
        else:
            line["status"] = status
        _emit(line)
    except Exception:
        pass
