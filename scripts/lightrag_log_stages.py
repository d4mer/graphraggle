#!/usr/bin/env python3
"""Parse lightrag-server logs into per-query stage timings (GRAG-14 helper).

Usage:
    docker logs lightrag-server 2>&1 | scripts/lightrag_log_stages.py
    scripts/lightrag_log_stages.py path/to/lightrag.log

What it relies on (real lines from the installed LightRAG v1.5.7, captured
from the production container; no secrets):

    INFO:  == LLM cache == saving: mix:keywords:8e43ec2a17989a7b4fb1db61a85ee4f3
    INFO: Query nodes: logistics workshop transcripts, firm horizon (top_k:40, cosine:0.2)
    INFO: Local query: 40 entites, 269 relations
    INFO: Query edges: key points, firm horizon (top_k:40, cosine:0.2)
    INFO: Global query: 49 entites, 40 relations
    INFO: Naive query: 20 chunks (chunk_top_k:20 cosine:0.2)
    INFO: Raw search results: 83 entities, 303 relations, 20 vector chunks
    INFO: After truncation: 55 entities, 223 relations
    INFO: Round-robin merged chunks: 245 -> 225 (deduplicated 20)
    WARNING: Rerank func: Worker timeout for task 140616318417792_907885.433148487 after 60s
    ERROR: Error during reranking: Rerank func: Worker execution timeout after 60s, using original chunks
    INFO: Successfully reranked: 20 chunks from 158 original chunks
    INFO: Final context: 55 entities, 223 relations, 11 chunks
    INFO:  == LLM cache == Query cache hit, using cached response as query result
    INFO: 172.18.0.1:48726 - "POST /query/stream HTTP/1.1" 200
    WARNING: low_level_keywords is empty
    WARNING: high_level_keywords is empty
    INFO: Forced low_level_keywords to origin query: <query text>

A query block runs from the first retrieval line up to and including its
access-log line ("POST /query" or "POST /query/stream"). LightRAG logs carry
no request id, so blocks are reported in log order and matched to a gateway
request_id only by ordering.

Anything the log does not show is reported as "not in log" — never guessed.
In particular keyword-extraction duration, embedding duration, and final
answer-generation duration are NOT in the log; only their boundaries are.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime

TS_RE = re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z)\s+(?P<level>[A-Z]+):\s?(?P<msg>.*)$")
ACCESS_RE = re.compile(r"\"POST /query(?:/stream)? HTTP/1\.1\" (?P<status>\d{3})")
MERGED_RE = re.compile(r"Round-robin merged chunks: (?P<before>\d+) -> (?P<after>\d+)")
RERANK_OK_RE = re.compile(r"Successfully reranked: (?P<after>\d+) chunks from (?P<before>\d+) original chunks")
RERANK_TIMEOUT_RE = re.compile(r"Rerank func: Worker timeout for task \S+ after (?P<seconds>\d+)s")
RERANK_ERR_RE = re.compile(r"Error during reranking: (?P<err>.*)")
FINAL_CTX_RE = re.compile(r"Final context: (?P<entities>\d+) entities, (?P<relations>\d+) relations, (?P<chunks>\d+) chunks")
QUERY_NODES_RE = re.compile(r"Query nodes: (?P<keywords>.*) \(top_k:(?P<top_k>\d+)")
KEYWORDS_CACHE_RE = re.compile(r"== LLM cache == saving: \S*keywords:")
CACHE_HIT_RE = re.compile(r"Query cache hit")
EMPTY_LLM_KW_RE = re.compile(r"low_level_keywords is empty")
EMPTY_HL_KW_RE = re.compile(r"high_level_keywords is empty")
FORCED_KW_RE = re.compile(r"Forced low_level_keywords to origin query:")

NOT_IN_LOG = "not in log"


def parse_ts(text: str) -> datetime | None:
    """Parse an ISO-8601 timestamp with 0-9 fractional digits and optional Z.

    ``datetime.fromisoformat`` accepts at most six fractional digits before
    Python 3.11, while Docker log timestamps carry nanoseconds (nine digits).
    Normalise the fraction here so parsing never depends on the interpreter
    version (works on 3.8+).
    """
    try:
        m = re.match(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$", text)
        if not m:
            return None
        base, frac, rest = m.group(1), m.group(2), m.group(3)
        if frac is not None:
            # 3.8-3.10 accept only exactly 3 or 6 fractional digits; 3.11+
            # accepts 1-6. Normalise to exactly six.
            frac = (frac + "000000")[:6]
            base = base + "." + frac
        return datetime.fromisoformat(base + rest.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ms(a: datetime | None, b: datetime | None) -> int | str:
    if a is None or b is None:
        return NOT_IN_LOG
    return int(round((b - a).total_seconds() * 1000))


def parse_lines(lines):
    """Return one dict per query block, in log order."""
    blocks = []
    current = None
    for raw in lines:
        m = TS_RE.match(raw.rstrip("\n"))
        if not m:
            continue
        ts = parse_ts(m.group("ts"))
        level, msg = m.group("level"), m.group("msg")
        if current is None:
            current = {"lines": []}
        current["lines"].append((ts, level, msg, raw.rstrip("\n")))

        access = ACCESS_RE.search(msg)
        if access:
            blocks.append(_summarize(current, access.group("status"), ts))
            current = None
        elif level == "ERROR" and "Error during reranking" in msg and current is not None:
            current.setdefault("rerank_error_line", msg)
    if current and current["lines"]:
        # Trailing block whose access line was cut off by the log window.
        blocks.append(_summarize(current, "unclosed", current["lines"][-1][0]))
    return blocks


def _summarize(block, access_status, access_ts):
    out = {
        "query_start": NOT_IN_LOG,
        "keyword_extraction": NOT_IN_LOG,
        "keyword_extraction_duration_ms": NOT_IN_LOG,
        "embedding_duration_ms": NOT_IN_LOG,
        "graph_vector_search_duration_ms": NOT_IN_LOG,
        "candidates_before_rerank": NOT_IN_LOG,
        "rerank": NOT_IN_LOG,
        "rerank_duration_ms": NOT_IN_LOG,
        "chunks_after_rerank": NOT_IN_LOG,
        "answer_generation_duration_ms": NOT_IN_LOG,
        "answer_cache_hit": False,
        "access_status": access_status,
        "total_ms": NOT_IN_LOG,
    }
    lines = block["lines"]
    first_ts = lines[0][0]
    out["query_start"] = lines[0][3][:60]

    merged_ts = merged = None
    rerank_done_ts = rerank_done = None
    rerank_timeout = None
    nodes_ts = None
    kw_cache_ts = None
    empty_kw = []
    final_ctx = None
    for ts, level, msg, raw in lines:
        if QUERY_NODES_RE.search(msg) and nodes_ts is None:
            nodes_ts = ts
        if KEYWORDS_CACHE_RE.search(msg):
            kw_cache_ts = ts
        if EMPTY_LLM_KW_RE.search(msg):
            empty_kw.append("low_level_keywords empty")
        if EMPTY_HL_KW_RE.search(msg):
            empty_kw.append("high_level_keywords empty")
        if FORCED_KW_RE.search(msg):
            empty_kw.append("low_level_keywords forced to origin query")
        mm = MERGED_RE.search(msg)
        if mm:
            merged, merged_ts = int(mm.group("after")), ts
            out["candidates_before_rerank"] = merged
        rm = RERANK_OK_RE.search(msg)
        if rm:
            rerank_done = {"after": int(rm.group("after")), "before": int(rm.group("before"))}
            rerank_done_ts = ts
            out["rerank"] = f"ok: {rerank_done['before']} -> {rerank_done['after']} chunks"
        if RERANK_TIMEOUT_RE.search(msg):
            rerank_timeout = int(RERANK_TIMEOUT_RE.search(msg).group("seconds"))
            out["rerank"] = f"timeout after {rerank_timeout}s (original chunks used)"
        if CACHE_HIT_RE.search(msg):
            out["answer_cache_hit"] = True
        fm = FINAL_CTX_RE.search(msg)
        if fm:
            final_ctx = {"entities": int(fm.group("entities")), "relations": int(fm.group("relations")),
                         "chunks": int(fm.group("chunks"))}
            final_ctx_ts = ts

    if empty_kw:
        out["keyword_extraction"] = "; ".join(dict.fromkeys(empty_kw))
    elif kw_cache_ts or nodes_ts:
        out["keyword_extraction"] = "non-empty (keywords logged by Query nodes line)"

    # Boundaries only: keyword extraction runs before the first retrieval line,
    # and the log has no start marker for it, so its duration is not derivable.
    if nodes_ts is not None:
        out["graph_vector_search_duration_ms"] = _ms(nodes_ts, merged_ts)
    if rerank_done_ts is not None:
        out["rerank_duration_ms"] = _ms(merged_ts, rerank_done_ts)
    elif rerank_timeout is not None and merged_ts is not None:
        out["rerank_duration_ms"] = rerank_timeout * 1000
    if final_ctx is not None:
        out["chunks_after_rerank"] = final_ctx["chunks"]
    out["total_ms"] = _ms(first_ts, access_ts)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Summarize lightrag-server query stages from logs")
    ap.add_argument("logfile", nargs="?", help="log file; omit to read stdin")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    if args.logfile:
        with open(args.logfile, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.readlines()
    else:
        lines = sys.stdin.readlines()

    blocks = parse_lines(lines)
    if args.json:
        print(json.dumps(blocks, indent=1))
        return 0
    if not blocks:
        print("no query blocks found (expected timestamped docker logs with a POST /query access line)")
        return 1
    for i, b in enumerate(blocks, 1):
        print(f"--- query {i} ---")
        for key in ("query_start", "keyword_extraction", "keyword_extraction_duration_ms",
                    "embedding_duration_ms", "graph_vector_search_duration_ms",
                    "candidates_before_rerank", "rerank", "rerank_duration_ms",
                    "chunks_after_rerank", "answer_generation_duration_ms",
                    "answer_cache_hit", "access_status", "total_ms"):
            print(f"  {key}: {b[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
