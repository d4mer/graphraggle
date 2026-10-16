#!/usr/bin/env python3
"""scripts/chunk_embed_bench.py — offline chunk x embedder x reranker benchmark.

Why this exists
---------------
The embedder truncates silently at 512 tokens (Hermes' proof, summarised in
docs/09-retrieval-accuracy-plan.md), so most of every 1200-token chunk is invisible to
vector search. Answering "which chunk size, which embedder, which reranker" by
re-ingesting costs hours per arm and needs the whole stack. The VECTOR retrieval side can
be measured offline instead: chunk the documents, embed chunks and questions, rank by
cosine, optionally rerank, and ask whether the gold evidence quote is in the top-k chunks.
No LightRAG, no LLM extraction, no ingest — minutes per configuration. The metric is the
same idea as the overnight loop's ``evidence_in_context%``, computed without a judge.

What this is NOT
----------------
It does not measure LightRAG's graph side (``mix`` mode, entity/relation vectors, the
gateway's token budgets) and it does not measure answer quality. A config that wins here
still has to earn its keep as a loop arm.

Chunker fidelity — read before comparing counts
-----------------------------------------------
These chunkers APPROXIMATE LightRAG 1.5.7's ``fixed_token`` and ``recursive_character``
chunkers; they are not LightRAG's code.

* ``fixed_token`` slides a token window with stride ``chunk_token_size - overlap``, in
  spirit with LightRAG's default. With tiktoken present it cuts on TOKEN boundaries, so it
  CAN split a word — that is part of what the fixed_token arm measures.
* ``recursive`` tries the separator list in order, then packs whole pieces up to the token
  size. Overlap is whole trailing PIECES (up to ``overlap`` tokens), so this chunker never
  cuts mid-word. The divergence from a token-level overlap is deliberate: a mid-word cut
  changes the embedding on both sides of a boundary and would confound the comparison.

Hermes compares chunk counts to production (108 chunks for the current corpus at
1200/100). Close counts mean the approximation is good enough to rank configs; a big gap
means compare configs against each other, not against production.

Token counting
--------------
``tiktoken`` cl100k_base when importable, otherwise a documented whitespace fallback (one
token per whitespace-separated run). The tokenizer used is printed and stored in
results.json — chunk token percentiles are meaningless without it. Both differ from the
embedder's own tokenizer: mxbai is BERT WordPiece and usually counts MORE tokens on names,
numbers and STT garbles, so 500 cl100k tokens is not proven to fit a 512-token WordPiece
window (docs/research/decision-embed-rerank-chunk.md marks that ratio UNVERIFIED).

What never leaves this script
-----------------------------
No chunk text, no question or evidence text, no corpus file names. Reports carry counts,
probe ids and numbers; documents are named by position (``doc 3/6``). The API key is read
from the environment and never printed, logged or written.

Usage
-----
    python3 scripts/chunk_embed_bench.py \
        --corpus probe/loop_corpus.json --probes probe/solvay-dev-v1.jsonl \
        [--probes probe/solvay-heldout-v1.jsonl] \
        --out probe/chunk-bench --embed-url http://localhost:8080 \
        --embed-model mxbai-embed-large-v1 --rerank-url http://localhost:8080 \
        --rerank-model jina-reranker-v3 --api-key-env OMLX_API_KEY \
        --configs docs/ops/loop/chunk-embed-bench-configs.json \
        --compare mxbai-recursive-350 current-1200-100-mxbai

Endpoints are OpenAI-shaped ``POST {url}/v1/embeddings`` and Jina-shaped ``POST
{url}/v1/rerank``. Embeddings are cached on disk keyed by sha256(model + prefix + text),
so reruns and added configs are free.

A URL pointing at a production port (9621 LightRAG, 8020 gateway) is refused before any
request: this script measures models, and the model server is a different process.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Reuse the overnight loop's probe/gold-evidence rules instead of restating them, so both
# instruments agree on what "the gold evidence" is. Scripts run as scripts/<name>.py, so
# the sibling module is importable.
_SCRIPT_DIR = str(Path(__file__).resolve().parent)
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from overnight_loop import (  # noqa: E402
    gold_evidence_of,
    normalise_quote,
    paired_bootstrap_difference,
    percentile,
)

# ── guardrails ──────────────────────────────────────────────────────────────

# 9621 is LightRAG and 8020 is the gateway in this deployment. Neither serves
# /v1/embeddings or /v1/rerank, and pointing an offline benchmark at production is how an
# experiment ends up hammering the stack a person is using.
PRODUCTION_BLOCKED_PORTS = frozenset({9621, 8020})

TOP_K_DEFAULTS = (5, 10, 20, 40)
EMBED_BATCH_MAX = 16
HTTP_RETRIES = 3
CHUNKERS = ("fixed_token", "recursive")


class BenchError(Exception):
    """A configuration or input problem to fix before running; no partial run."""


def endpoint_problem(url: str, *, what: str) -> str | None:
    """Return why this endpoint URL must be refused, or None if it is acceptable."""
    if not url or not url.strip():
        return f"{what} URL is empty"
    candidate = url.strip()
    if "://" not in candidate:
        return f"{what} URL {candidate!r} needs a scheme (http:// or https://)"
    scheme, _, rest = candidate.partition("://")
    if scheme.lower() not in ("http", "https"):
        return f"{what} URL scheme {scheme!r} is not http/https"
    authority = rest.split("/", 1)[0]
    host = authority.rsplit("@", 1)[-1]
    port = ""
    if host.startswith("["):                       # IPv6 literal: [::1]:8080
        _, _, tail = host.partition("]")
        port = tail.lstrip(":")
    elif host.count(":") == 1:
        host, _, port = host.partition(":")
    if not host:
        return f"{what} URL {candidate!r} has no host"
    if port.isdigit() and int(port) in PRODUCTION_BLOCKED_PORTS:
        return (f"{what} URL {candidate!r} points at a production port ({port}). This "
                "benchmark talks to the model server, never to LightRAG or the gateway.")
    return None


# ── token counting ──────────────────────────────────────────────────────────

class WhitespaceTokenizer:
    """Fallback: one token per whitespace-separated run.

    Documented, not hidden: this under-counts real tokens (punctuation and subword splits
    are free), so chunk sizes derived from it are optimistic. The run reports which
    tokenizer was used.
    """

    name = "whitespace-fallback"

    def encode(self, text: str) -> list[int]:
        return list(range(len(text.split())))

    def count(self, text: str) -> int:
        return len(text.split())

    def decode_slice(self, text: str, start: int, end: int) -> str:
        return " ".join(text.split()[max(0, start):max(0, end)])


class TiktokenTokenizer:
    """cl100k_base, the tokenizer family LightRAG counts with.

    Slicing is done on token ids, so a slice can end inside a word — the same thing
    LightRAG's fixed_token chunker does, and part of what that arm measures.
    """

    def __init__(self, encoding: Any) -> None:
        self._encoding = encoding
        self.name = f"tiktoken:{getattr(encoding, 'name', 'unknown')}"

    def encode(self, text: str) -> list[int]:
        return list(self._encoding.encode(text, disallowed_special=()))

    def count(self, text: str) -> int:
        return len(self.encode(text))

    def decode_slice(self, text: str, start: int, end: int) -> str:
        ids = self.encode(text)
        return self._encoding.decode(ids[max(0, start):max(0, end)])


def build_tokenizer(requested: str = "auto") -> Any:
    if requested == "whitespace":
        return WhitespaceTokenizer()
    try:
        import tiktoken                                   # type: ignore[import-not-found]

        return TiktokenTokenizer(tiktoken.get_encoding("cl100k_base"))
    except Exception:
        if requested == "tiktoken":
            raise BenchError("--tokenizer tiktoken was asked for but tiktoken is not usable")
        return WhitespaceTokenizer()


# ── chunkers ────────────────────────────────────────────────────────────────

def fixed_token_chunks(text: str, size: int, overlap: int, tokenizer: Any) -> list[str]:
    """LightRAG-``fixed_token``-shaped sliding token window. May cut inside a word."""
    ids = tokenizer.encode(text)
    if not ids:
        return []
    stride = max(1, size - overlap)
    chunks: list[str] = []
    start = 0
    while start < len(ids):
        piece = tokenizer.decode_slice(text, start, start + size)
        if piece.strip():
            chunks.append(piece)
        start += stride
    return chunks


# The separator order the decision memo asks for: paragraph, line, sentence, clause, word.
# Transcript turns are "(Speaker) text" lines, so "\n" keeps turns whole.
DEFAULT_SEPARATORS = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", "")


def _split_keep_separator(segment: str, separator: str) -> list[str]:
    """Split, re-attaching the separator to the piece on its left.

    Keeping it is what lets a normalised quote survive inside one piece: dropping "\n" or
    ". " at every join would cut sentences apart and inflate the split-quote count.
    """
    if separator == "":
        return [segment]
    parts = segment.split(separator)
    if len(parts) < 2:
        return []
    out: list[str] = []
    for index, part in enumerate(parts):
        if index < len(parts) - 1:
            part += separator
        if part:
            out.append(part)
    return out


def _hard_split_words(segment: str, size: int, tokenizer: Any) -> list[str]:
    """Last-resort split: whole words up to the budget.

    Reached when the separator list cannot make a segment fit (the degenerate STT shape:
    one long unpunctuated run). Breaking between words is a deliberate divergence from a
    token cut — it keeps both sides real words. A single word longer than the budget
    cannot be broken and stands alone.
    """
    words = segment.split()
    if len(words) < 2:
        return [segment]
    pieces: list[str] = []
    current: list[str] = []
    for word in words:
        if current and tokenizer.count(" ".join(current + [word])) > size:
            pieces.append(" ".join(current) + " ")
            current = []
        current.append(word)
    if current:
        pieces.append(" ".join(current))
    return pieces


def recursive_split(text: str, size: int, separators: Sequence[str], tokenizer: Any) -> list[str]:
    """Split into the largest pieces that fit ``size``, trying separators in order."""
    if tokenizer.count(text) <= size:
        return [text] if text.strip() else []
    for index, separator in enumerate(separators):
        parts = _split_keep_separator(text, separator)
        if len(parts) < 2:
            continue
        pieces: list[str] = []
        for part in parts:
            if tokenizer.count(part) <= size:
                pieces.append(part)
            elif separator == "":
                pieces.extend(_hard_split_words(part, size, tokenizer))
            else:
                pieces.extend(recursive_split(part, size, separators[index + 1:], tokenizer))
        return [piece for piece in pieces if piece.strip()]
    return _hard_split_words(text, size, tokenizer)


def recursive_chunks(text: str, size: int, overlap: int, separators: Sequence[str],
                     tokenizer: Any) -> list[str]:
    """Separator-ordered splitting, then packing whole pieces up to the token size.

    Overlap repeats the trailing PIECES of the previous chunk (up to ``overlap`` tokens) at
    the head of the next one, so no word is ever cut and a quote that sits inside one piece
    appears in two consecutive chunks rather than being sliced.
    """
    pieces = recursive_split(text, size, separators, tokenizer)
    if not pieces:
        return []
    if size <= 0:
        raise BenchError("chunk_token_size must be positive")
    chunks: list[str] = []
    current: list[str] = []
    current_tokens = 0
    for piece in pieces:
        piece_tokens = tokenizer.count(piece)
        if current and current_tokens + piece_tokens > size:
            chunks.append("".join(current))
            tail: list[str] = []
            tail_tokens = 0
            for previous in reversed(current):
                previous_tokens = tokenizer.count(previous)
                if overlap <= 0 or tail_tokens + previous_tokens > overlap:
                    break
                tail.insert(0, previous)
                tail_tokens += previous_tokens
            if tail and tail_tokens + piece_tokens > size:
                # The overlap would push this chunk over the limit, so it is skipped at
                # this join. Overlap is a bonus, never a licence to exceed the size.
                current, current_tokens = [], 0
            else:
                current, current_tokens = tail, tail_tokens
        current.append(piece)
        current_tokens += piece_tokens
    if current and "".join(current).strip():
        chunks.append("".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def chunk_document(text: str, spec: dict[str, Any], tokenizer: Any) -> list[str]:
    strategy = spec.get("strategy", "fixed_token")
    size = int(spec.get("chunk_token_size", 1200))
    overlap = int(spec.get("overlap", 0))
    if strategy == "fixed_token":
        return fixed_token_chunks(text, size, overlap, tokenizer)
    if strategy == "recursive":
        separators = list(spec.get("separators") or DEFAULT_SEPARATORS)
        return recursive_chunks(text, size, overlap, separators, tokenizer)
    raise BenchError(f"unknown chunker strategy {strategy!r} (expected one of {CHUNKERS})")


# ── configs file ────────────────────────────────────────────────────────────

def load_configs(path: Path) -> list[dict[str, Any]]:
    """Parse and validate the configs file. Unknown keys are ignored, bad ones refused."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchError(f"configs file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchError(f"configs file is not valid JSON: {path}: {exc}") from exc

    entries = raw.get("configs") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise BenchError("configs file must hold a non-empty 'configs' list")
    configs: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise BenchError(f"configs[{index}] must be an object")
        name = str(entry.get("name") or "").strip()
        if not name:
            raise BenchError(f"configs[{index}]: name is required")
        if name in seen:
            raise BenchError(f"duplicate config name: {name}")
        seen.add(name)

        chunker = entry.get("chunker")
        if not isinstance(chunker, dict):
            raise BenchError(f"config {name}: chunker must be an object")
        strategy = chunker.get("strategy", "fixed_token")
        if strategy not in CHUNKERS:
            raise BenchError(f"config {name}: chunker.strategy must be one of {CHUNKERS}")
        size = chunker.get("chunk_token_size", 1200)
        overlap = chunker.get("overlap", 0)
        if not isinstance(size, int) or isinstance(size, bool) or size < 1:
            raise BenchError(f"config {name}: chunker.chunk_token_size must be a positive int")
        if not isinstance(overlap, int) or isinstance(overlap, bool) or overlap < 0:
            raise BenchError(f"config {name}: chunker.overlap must be a non-negative int")
        if overlap >= size:
            raise BenchError(f"config {name}: chunker.overlap must be smaller than "
                             f"chunk_token_size ({overlap} >= {size})")
        separators = chunker.get("separators")
        if separators is not None and (
                not isinstance(separators, list)
                or not all(isinstance(item, str) for item in separators)):
            raise BenchError(f"config {name}: chunker.separators must be a list of strings")
        if separators is not None and (len(separators) > 64
                                       or any(len(item) > 256 for item in separators)):
            raise BenchError(f"config {name}: chunker.separators may hold at most 64 entries "
                             "of at most 256 characters")

        embedder = entry.get("embedder")
        if not isinstance(embedder, dict) or not str(embedder.get("model") or "").strip():
            raise BenchError(f"config {name}: embedder.model is required")
        for prefix_key in ("doc_prefix", "query_prefix"):
            if embedder.get(prefix_key) is not None and not isinstance(embedder[prefix_key], str):
                raise BenchError(f"config {name}: embedder.{prefix_key} must be a string")
        max_input = embedder.get("max_input_tokens")
        if max_input is not None and (not isinstance(max_input, int) or isinstance(max_input, bool)
                                      or max_input < 1):
            raise BenchError(f"config {name}: embedder.max_input_tokens must be a positive int")

        rerank = entry.get("rerank")
        if rerank is not None:
            if not isinstance(rerank, dict) or not str(rerank.get("model") or "").strip():
                raise BenchError(f"config {name}: rerank.model is required (or rerank: null)")
            top_n_in = rerank.get("top_n_in", 20)
            if not isinstance(top_n_in, int) or isinstance(top_n_in, bool) or top_n_in < 1:
                raise BenchError(f"config {name}: rerank.top_n_in must be a positive int")
            rerank = {"model": str(rerank["model"]), "top_n_in": int(top_n_in)}

        configs.append({
            "name": name,
            "note": str(entry.get("_note") or ""),
            "chunker": {
                "strategy": strategy,
                "chunk_token_size": int(size),
                "overlap": int(overlap),
                **({"separators": [str(item) for item in separators]} if separators else {}),
            },
            "embedder": {
                "model": str(embedder["model"]),
                **({"doc_prefix": str(embedder["doc_prefix"])} if embedder.get("doc_prefix") else {}),
                **({"query_prefix": str(embedder["query_prefix"])}
                   if embedder.get("query_prefix") else {}),
                **({"max_input_tokens": int(max_input)} if max_input else {}),
            },
            "rerank": rerank,
        })
    return configs


# ── HTTP ────────────────────────────────────────────────────────────────────

class Http:
    """JSON-over-HTTP client. Tests pass an object with this same shape instead."""

    def http_json(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                  payload: Any = None, timeout: float = 30.0) -> tuple[int, Any]:
        data = None
        request_headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status = response.getcode()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status = exc.code
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"{method} {url} transport error: {exc}") from exc
        if not raw.strip():
            return status, {}
        try:
            return status, json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{method} {url} returned non-JSON body") from exc


class EmbedCache:
    """Disk cache of vectors keyed by sha256(model + prefix + text).

    Keyed by the embedding INPUT, not by config: adding a config that reuses a chunker and
    an embedder costs nothing, and changing a prefix legitimately re-embeds. One JSON file
    per vector keeps the directory greppable and lets a partial run keep whatever it got.
    Text never appears in the file name or the contents.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.hits = 0
        self.misses = 0
        self.directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(model: str, prefix: str, text: str) -> str:
        return hashlib.sha256(f"{model}\x00{prefix}\x00{text}".encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key[:2]}-{key}.json"

    def get(self, model: str, prefix: str, text: str) -> list[float] | None:
        path = self.path_for(self.key(model, prefix, text))
        try:
            body = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        vector = body.get("vector")
        if not isinstance(vector, list) or not all(isinstance(x, (int, float)) for x in vector):
            self.misses += 1
            return None
        self.hits += 1
        return [float(x) for x in vector]

    def put(self, model: str, prefix: str, text: str, vector: Sequence[float]) -> None:
        path = self.path_for(self.key(model, prefix, text))
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps({"model": model, "dim": len(vector),
                                    "vector": [float(x) for x in vector]}), encoding="utf-8")
        temp.replace(path)


class ModelClient:
    """Embeddings and rerank calls: batching, retries with backoff, timeouts.

    Logs counts and elapsed time only. The API key is passed through in a header and never
    appears in a log line or an exception message this script writes.
    """

    def __init__(self, *, http: Any, embed_url: str, embed_model: str, rerank_url: str,
                 rerank_model: str, api_key: str, cache: EmbedCache,
                 sleep: Any = time.sleep, log: Any = None, timeout: float = 60.0,
                 retries: int = HTTP_RETRIES) -> None:
        self.http = http
        self.embed_url = embed_url.rstrip("/")
        self.embed_model = embed_model
        self.rerank_url = rerank_url.rstrip("/") if rerank_url else ""
        self.rerank_model = rerank_model
        self.api_key = api_key
        self.cache = cache
        self.sleep = sleep
        self.log = log or (lambda line: None)
        self.timeout = timeout
        self.retries = max(1, int(retries))
        self.embed_calls = 0
        self.embed_texts = 0
        self.embed_seconds = 0.0
        self.rerank_calls = 0
        self.rerank_seconds = 0.0
        self.max_batch = 0

    def _headers(self) -> dict[str, str]:
        if self.api_key:
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    def _post_retry(self, url: str, payload: dict[str, Any], what: str) -> Any:
        last = "no attempt"
        for attempt in range(self.retries):
            try:
                status, body = self.http.http_json("POST", url, headers=self._headers(),
                                                   payload=payload, timeout=self.timeout)
            except RuntimeError as exc:
                last = str(exc)
            else:
                if status < 400:
                    return body
                last = f"HTTP {status}"
            if attempt < self.retries - 1:
                self.sleep(0.5 * (2 ** attempt))
        raise BenchError(f"{what} failed after {self.retries} attempts: {last}")

    def embed(self, texts: Sequence[str], *, prefix: str = "", model: str | None = None) -> list[list[float]]:
        """Return one vector per text, in order. Cache first, then batched POSTs."""
        model = model or self.embed_model
        out: list[list[float] | None] = [None] * len(texts)
        pending: list[int] = []
        for index, text in enumerate(texts):
            cached = self.cache.get(model, prefix, text)
            if cached is None:
                pending.append(index)
            else:
                out[index] = cached
        for start in range(0, len(pending), EMBED_BATCH_MAX):
            batch = pending[start:start + EMBED_BATCH_MAX]
            self.max_batch = max(self.max_batch, len(batch))
            payload = {"model": model, "input": [texts[i] for i in batch]}
            started = time.monotonic()
            body = self._post_retry(f"{self.embed_url}/v1/embeddings", payload, "embeddings")
            self.embed_seconds += time.monotonic() - started
            self.embed_calls += 1
            self.embed_texts += len(batch)
            vectors = _embedding_vectors(body, len(batch))
            for index, vector in zip(batch, vectors):
                out[index] = vector
                self.cache.put(model, prefix, texts[index], vector)
        missing = [index for index, vector in enumerate(out) if vector is None]
        if missing:
            raise BenchError(f"embeddings: {len(missing)} of {len(texts)} inputs got no vector")
        return [vector for vector in out if vector is not None]

    def rerank_order(self, query: str, documents: Sequence[str], top_n: int) -> list[int]:
        """Return candidate indices best-first using a Jina-shaped /v1/rerank.

        The request body is Jina's documented shape (``query``, ``results`` of ``{"text":
        ...}``, ``top_n``). The response is read tolerantly: ``results[].index`` with
        ``relevance_score``, or a bare list of scores. oMLX's exact rerank contract is
        UNVERIFIED from this repo, so an unusable body is an explicit error rather than a
        silent no-op.
        """
        if not self.rerank_url:
            raise BenchError("a rerank config needs --rerank-url")
        payload = {
            "model": self.rerank_model,
            "query": query,
            "top_n": min(top_n, len(documents)),
            "results": [{"text": document} for document in documents],
        }
        started = time.monotonic()
        body = self._post_retry(f"{self.rerank_url}/v1/rerank", payload, "rerank")
        self.rerank_seconds += time.monotonic() - started
        self.rerank_calls += 1
        scored = _rerank_scores(body, len(documents))
        if scored is None:
            raise BenchError("rerank response had no usable scores (expected "
                             "results[].index/relevance_score)")
        order = sorted(range(len(documents)), key=lambda index: -scored[index])
        return order


def _embedding_vectors(body: Any, expected: int) -> list[list[float]]:
    """Pull the vectors out of an OpenAI-shaped embeddings response."""
    if not isinstance(body, dict):
        raise BenchError("embeddings response was not a JSON object")
    data = body.get("data")
    if not isinstance(data, list):
        raise BenchError("embeddings response had no 'data' list")
    vectors: list[list[float]] = []
    for index, entry in enumerate(data):
        raw = entry.get("embedding") if isinstance(entry, dict) else None
        if not isinstance(raw, list) or not all(isinstance(x, (int, float)) for x in raw):
            raise BenchError(f"embeddings response item {index} had no numeric 'embedding'")
        vectors.append([float(x) for x in raw])
    if len(vectors) != expected:
        raise BenchError(f"embeddings response returned {len(vectors)} vectors for "
                         f"{expected} inputs")
    return vectors


def _rerank_scores(body: Any, count: int) -> list[float] | None:
    """Scores per candidate from a rerank response, or None if unusable."""
    if not isinstance(body, dict):
        return None
    results = body.get("results")
    scores: list[float] = [float("-inf")] * count
    if isinstance(results, list):
        seen = 0
        for position, entry in enumerate(results):
            if isinstance(entry, dict):
                index = entry.get("index", position)
                score = entry.get("relevance_score", entry.get("score"))
            else:
                index, score = position, entry
            if not isinstance(index, int) or not (0 <= index < count):
                continue
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                continue
            scores[index] = float(score)
            seen += 1
        return scores if seen else None
    if isinstance(body.get("scores"), list):
        values = [value for value in body["scores"] if isinstance(value, (int, float))]
        if len(values) == count:
            return [float(value) for value in values]
    return None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ── inputs ──────────────────────────────────────────────────────────────────

def load_corpus(paths_file: Path) -> list[str]:
    """Read corpus.json (a list of file paths, the same shape the loop uses) and the text.

    File NAMES never reach a report: documents are addressed by position. A missing or
    unreadable file is an error naming the position, not the path.
    """
    try:
        entries = json.loads(Path(paths_file).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchError(f"corpus file not found: {paths_file}") from exc
    except json.JSONDecodeError as exc:
        raise BenchError(f"corpus file is not valid JSON: {paths_file}: {exc}") from exc
    if not isinstance(entries, list) or not entries:
        raise BenchError("corpus file must be a non-empty JSON list of file paths")
    texts: list[str] = []
    for position, entry in enumerate(entries, start=1):
        if not isinstance(entry, str) or not entry.strip():
            raise BenchError(f"corpus entry {position}/{len(entries)} is not a file path")
        try:
            raw = Path(entry).read_bytes()
        except OSError:
            raise BenchError(f"cannot read corpus doc {position}/{len(entries)}") from None
        if len(raw) == 0:
            raise BenchError(f"corpus doc {position}/{len(entries)} is empty")
        texts.append(raw.decode("utf-8", errors="replace"))
    return texts


def load_probes(files: Sequence[Path]) -> tuple[list[dict[str, Any]], int]:
    """Probe items from one or more JSONL files. Items without gold evidence are skipped."""
    items: list[dict[str, Any]] = []
    skipped = 0
    seen_ids: set[str] = set()
    for path in files:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise BenchError(f"cannot read probe file: {exc}") from None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if not isinstance(row, dict) or not str(row.get("question") or "").strip():
                skipped += 1
                continue
            if not gold_evidence_of(row):
                skipped += 1          # nothing to look for: this probe cannot measure recall
                continue
            ident = str(row.get("id") or f"probe-{len(items) + 1}")
            if ident in seen_ids:
                ident = f"{ident}#{len(items) + 1}"
            seen_ids.add(ident)
            items.append({**row, "id": ident})
    if not items:
        raise BenchError("no probe item carries gold evidence, so nothing can be measured")
    return items, skipped


# ── the measurement ─────────────────────────────────────────────────────────

@dataclass
class Chunk:
    position: int          # global index into the config's chunk list
    doc: int               # 1-based document position
    index: int             # 0-based index within that document
    text: str
    tokens: int


def build_chunks(texts: Sequence[str], spec: dict[str, Any], tokenizer: Any) -> list[Chunk]:
    chunks: list[Chunk] = []
    for doc_position, text in enumerate(texts, start=1):
        for index, piece in enumerate(chunk_document(text, spec, tokenizer)):
            chunks.append(Chunk(position=len(chunks), doc=doc_position, index=index,
                                text=piece, tokens=tokenizer.count(piece)))
    return chunks


def quote_location(gold: Sequence[str], normalised: Sequence[str],
                   chunks: Sequence[Chunk]) -> tuple[int | None, tuple[int, int] | None]:
    """Where the gold quote sits: one chunk, or the seam between two adjacent chunks.

    The seam case is what small chunks pay for: a quote that straddles a boundary is only
    recoverable if BOTH sides are retrieved. Only the longest gold string is used for the
    seam (the full quote), because a short gold fragment that happens to sit inside one
    chunk is not a boundary case.
    """
    single: int | None = None
    for needle in gold:
        for chunk_index, haystack in enumerate(normalised):
            if needle in haystack and (single is None or chunk_index < single):
                single = chunk_index
                break
    if single is not None:
        return single, None
    longest = max(gold, key=len) if gold else ""
    if not longest:
        return None, None
    for left, right in zip(chunks, chunks[1:]):
        if right.doc != left.doc or right.index != left.index + 1:
            continue
        if longest in f"{normalised[left.position]} {normalised[right.position]}":
            return None, (left.position, right.position)
    return None, None


def rank_probe(gold: Sequence[str], chunks: Sequence[Chunk], order: Sequence[int],
               normalised: Sequence[str]) -> dict[str, Any]:
    """One probe's outcome under a final chunk ordering (cosine, then rerank if any).

    ``rank`` is the 1-based position at which the gold evidence is fully present: the
    chunk that contains it, or the LATER of the two seam chunks (both must be retrieved).
    """
    single, pair = quote_location(gold, normalised, chunks)
    rank_of = {chunk_index: position + 1 for position, chunk_index in enumerate(order)}
    rank: int | None = None
    split = False
    if single is not None:
        rank = rank_of.get(single)
    elif pair is not None:
        left, right = rank_of.get(pair[0]), rank_of.get(pair[1])
        if left and right:
            rank = max(left, right)
            split = True
    return {"single": single, "pair": pair, "split": split, "rank": rank}


def evaluate_config(config: dict[str, Any], texts: Sequence[str], probes: Sequence[dict[str, Any]],
                    client: ModelClient, tokenizer: Any, top_ks: Sequence[int]) -> dict[str, Any]:
    """Chunk, embed, rank (and optionally rerank) and score one config."""
    chunks = build_chunks(texts, config["chunker"], tokenizer)
    if not chunks:
        raise BenchError(f"config {config['name']}: the corpus produced no chunks")
    normalised = [normalise_quote(chunk.text) for chunk in chunks]
    embedder = config["embedder"]
    started = time.monotonic()
    chunk_vectors = client.embed([chunk.text for chunk in chunks],
                                 prefix=embedder.get("doc_prefix", ""),
                                 model=embedder["model"])
    question_vectors = client.embed([str(probe["question"]) for probe in probes],
                                    prefix=embedder.get("query_prefix", ""),
                                    model=embedder["model"])
    cosine_scores = [[cosine(question, chunk) for chunk in chunk_vectors]
                     for question in question_vectors]

    rows: list[dict[str, Any]] = []
    rerank_model = (config.get("rerank") or {}).get("model")
    top_n_in = int((config.get("rerank") or {}).get("top_n_in", 20))
    for probe_index, probe in enumerate(probes):
        order = sorted(range(len(chunks)), key=lambda index: -cosine_scores[probe_index][index])
        reranked = False
        if rerank_model:
            candidates = order[:max(1, top_n_in)]
            # A dead reranker must not quietly become a "no rerank" number: the arm fails
            # loudly and the operator re-runs with the reranker fixed or the config dropped.
            best = client.rerank_order(str(probe["question"]),
                                       [chunks[index].text for index in candidates],
                                       top_n_in)
            chosen = set(candidates)
            order = [candidates[rank] for rank in best] + [i for i in order if i not in chosen]
            reranked = True
        gold = gold_evidence_of(probe)
        outcome = rank_probe(gold, chunks, order, normalised)
        rank = outcome["rank"]
        rows.append({
            "id": probe["id"],
            "type": str(probe.get("type") or "unknown"),
            "rank": rank,
            "split": outcome["split"],
            "quote_in_seam": outcome["pair"] is not None,
            "reranked": reranked,
            "hits": {str(k): bool(rank is not None and rank <= k) for k in top_ks},
        })
    elapsed = time.monotonic() - started

    token_counts = [chunk.tokens for chunk in chunks]
    per_doc: dict[int, int] = {}
    for chunk in chunks:
        per_doc[chunk.doc] = per_doc.get(chunk.doc, 0) + 1
    max_input = embedder.get("max_input_tokens")
    summary = {
        "config": config["name"],
        "chunker": config["chunker"],
        "embedder": embedder,
        "rerank": config.get("rerank"),
        "probes": len(rows),
        "chunks_total": len(chunks),
        "docs": len(texts),
        "mean_chunks_per_doc": round(sum(per_doc.values()) / max(1, len(per_doc)), 2),
        "chunk_tokens": {
            "p50": percentile(token_counts, 0.50),
            "p90": percentile(token_counts, 0.90),
            "p99": percentile(token_counts, 0.99),
            "max": max(token_counts),
        },
        "over_embedder_window_chunks": (
            sum(1 for value in token_counts if value > int(max_input)) if max_input else None),
        "over_embedder_window_pct": (
            round(100.0 * sum(1 for value in token_counts if value > int(max_input)) / len(token_counts), 2)
            if max_input else None),
        "recall": aggregate_recall(rows, top_ks),
        "by_type": recall_by_type(rows, top_ks),
        "seconds": round(elapsed, 2),
        "embed_calls": client.embed_calls,
        "embed_texts": client.embed_texts,
        "cache_hits": client.cache.hits,
        "rerank_calls": client.rerank_calls,
    }
    return {"summary": summary, "rows": rows}


def aggregate_recall(rows: Sequence[dict[str, Any]], top_ks: Sequence[int]) -> dict[str, Any]:
    scored = [row for row in rows if "rank" in row]
    total = len(scored)
    out: dict[str, Any] = {
        "n": total,
        "seam_quotes": sum(1 for row in scored if row.get("quote_in_seam")),
        "cut_by_boundary_pct": round(100.0 * sum(1 for row in scored if row.get("quote_in_seam"))
                                     / total, 2) if total else 0.0,
    }
    for k in top_ks:
        hits = sum(1 for row in scored if row["hits"][str(k)])
        out[f"recall@{k}"] = round(100.0 * hits / total, 2) if total else 0.0
    reciprocals = [1.0 / row["rank"] for row in scored if row.get("rank")]
    out["mrr"] = round(sum(reciprocals) / total, 4) if total else 0.0
    out["found_at_all"] = round(100.0 * len(reciprocals) / total, 2) if total else 0.0
    return out


def recall_by_type(rows: Sequence[dict[str, Any]], top_ks: Sequence[int]) -> dict[str, dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if "rank" not in row:
            continue
        buckets.setdefault(str(row.get("type") or "unknown"), []).append(row)
    return {name: aggregate_recall(members, top_ks) for name, members in sorted(buckets.items())}


def compare_configs(results: dict[str, dict[str, Any]], left: str, right: str,
                    top_ks: Sequence[int]) -> dict[str, Any]:
    if left not in results:
        raise BenchError(f"--compare: no results for config {left!r}")
    if right not in results:
        raise BenchError(f"--compare: no results for config {right!r}")
    comparison: dict[str, Any] = {"a": left, "b": right, "per_k": {}}
    for k in top_ks:
        a_rows = {row["id"]: 1.0 if row["hits"][str(k)] else 0.0
                  for row in results[left]["rows"] if "rank" in row}
        b_rows = {row["id"]: 1.0 if row["hits"][str(k)] else 0.0
                  for row in results[right]["rows"] if "rank" in row}
        comparison["per_k"][str(k)] = paired_bootstrap_difference(a_rows, b_rows)
    return comparison


# ── reports ─────────────────────────────────────────────────────────────────

def _format_percent(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):g}"


def results_table(summaries: Sequence[dict[str, Any]], top_ks: Sequence[int]) -> str:
    ks = ", ".join(f"recall@{k}%" for k in top_ks)
    header = ("| config | chunks | mean/doc | tok p50 | p99 | max | over window% | "
              f"{ks} | seam% | MRR | found% | rerank |\n"
              + "|" + "---|" * (11 + len(top_ks)))
    lines = [header]
    for summary in summaries:
        recall = summary["recall"]
        row = [
            summary["config"],
            str(summary["chunks_total"]),
            f"{summary['mean_chunks_per_doc']:g}",
            f"{summary['chunk_tokens']['p50']:g}",
            f"{summary['chunk_tokens']['p99']:g}",
            f"{summary['chunk_tokens']['max']:g}",
            _format_percent(summary.get("over_embedder_window_pct")),
        ]
        row += [_format_percent(recall[f"recall@{k}"]) for k in top_ks]
        row += [_format_percent(recall["cut_by_boundary_pct"]), f"{recall['mrr']:g}",
                _format_percent(recall["found_at_all"]),
                (summary.get("rerank") or {}).get("model", "no")]
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def comparison_section(comparison: dict[str, Any]) -> str:
    lines = [f"\n## Paired comparison: {comparison['a']} minus {comparison['b']}\n",
             "| k | probes | A | B | difference | 95% CI | resolved? |",
             "|---|---|---|---|---|---|---|"]
    for k, stats in sorted(comparison["per_k"].items(), key=lambda item: int(item[0])):
        interval = f"[{stats['ci_low']:g}, {stats['ci_high']:g}]"
        lines.append(f"| @{k} | {stats['n']} | {stats['mean_a']:g} | {stats['mean_b']:g} | "
                     f"{stats['difference']:+g} | {interval} | "
                     f"{'yes' if stats['excludes_zero'] else 'not resolved'} |")
    lines.append("")
    lines.append("Differences are per-probe hits resampled by probe with a fixed seed, so the "
                 "same two configs always give the same interval. An interval covering zero is "
                 "not a result.")
    return "\n".join(lines)


# ── run ─────────────────────────────────────────────────────────────────────

def run(args: argparse.Namespace, *, http: Any | None = None,
        sleep: Any = time.sleep, print_fn: Any = print) -> dict[str, Any]:
    for url, what in ((args.embed_url, "embedder"), (args.rerank_url or "", "rerank")):
        problem = endpoint_problem(url, what=what) if url else None
        if problem:
            raise BenchError(problem)
    if args.api_key_env:
        api_key = os.environ.get(args.api_key_env, "")
        if not api_key:
            print_fn(f"warning: ${args.api_key_env} is unset; calling without an Authorization header")
    else:
        api_key = ""

    configs = load_configs(Path(args.configs))
    by_name = {config["name"]: config for config in configs}
    if args.only:
        unknown = [name for name in args.only if name not in by_name]
        if unknown:
            raise BenchError(f"--only names unknown configs: {', '.join(unknown)}")
        configs = [config for config in configs if config["name"] in args.only]
    top_ks = sorted({int(value) for value in args.top_k.split(",") if value.strip()})
    if not top_ks:
        raise BenchError("--top-k needs at least one number")

    texts = load_corpus(Path(args.corpus))
    probes, skipped_probes = load_probes([Path(path) for path in args.probes])
    tokenizer = build_tokenizer(args.tokenizer)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = EmbedCache(Path(args.cache_dir) if args.cache_dir else out_dir / "embed-cache")
    log_lines: list[str] = []

    def log(line: str) -> None:
        log_lines.append(line)
        print_fn(line)

    client = ModelClient(http=http or Http(), embed_url=args.embed_url,
                         embed_model=args.embed_model, rerank_url=args.rerank_url or "",
                         rerank_model=args.rerank_model or "", api_key=api_key, cache=cache,
                         sleep=sleep, log=log, timeout=args.timeout)

    rerank_configs = [config for config in configs if config.get("rerank")]
    if rerank_configs and not args.rerank_url:
        raise BenchError(f"configs {', '.join(c['name'] for c in rerank_configs)} ask for a "
                         "reranker but --rerank-url was not given")

    log(f"tokenizer: {tokenizer.name}; docs: {len(texts)}; probes: {len(probes)} "
        f"(skipped without gold evidence: {skipped_probes})")
    results: dict[str, dict[str, Any]] = {}
    summaries: list[dict[str, Any]] = []
    for config in configs:
        before_hits = cache.hits
        bundle = evaluate_config(config, texts, probes, client, tokenizer, top_ks)
        results[config["name"]] = bundle
        summaries.append(bundle["summary"])
        recall = bundle["summary"]["recall"]
        log(f"{config['name']}: chunks {bundle['summary']['chunks_total']} "
            f"({bundle['summary']['mean_chunks_per_doc']}/doc), "
            + ", ".join(f"@{k} {recall[f'recall@{k}']:g}%" for k in top_ks)
            + f", seam {recall['cut_by_boundary_pct']:g}%, MRR {recall['mrr']:g}, "
              f"cache +{cache.hits - before_hits} hits, {bundle['summary']['seconds']}s")

    comparison = None
    if args.compare:
        comparison = compare_configs(results, args.compare[0], args.compare[1], top_ks)

    report: dict[str, Any] = {
        "tokenizer": tokenizer.name,
        "top_k": top_ks,
        "docs": len(texts),
        "probes": len(probes),
        "probes_skipped_no_evidence": skipped_probes,
        "embed_endpoint_path": "/v1/embeddings",
        "embed_model_default": args.embed_model,
        "cache": {"hits": cache.hits, "misses": cache.misses},
        "http": {"embed_calls": client.embed_calls, "embed_texts": client.embed_texts,
                 "max_batch": client.max_batch, "embed_seconds": round(client.embed_seconds, 2),
                 "rerank_calls": client.rerank_calls,
                 "rerank_seconds": round(client.rerank_seconds, 2)},
        "configs": summaries,
        "per_probe": {name: [row for row in bundle["rows"]] for name, bundle in results.items()},
        "comparison": comparison,
        "log": log_lines,
    }
    (out_dir / "results.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                                          encoding="utf-8")
    table = ["# Chunk x embedder x reranker benchmark", "",
             f"Tokenizer: `{tokenizer.name}`. Docs: {len(texts)}. Probes: {len(probes)} "
             f"({skipped_probes} skipped for carrying no gold evidence).", "",
             results_table(summaries, top_ks)]
    if comparison:
        table.append(comparison_section(comparison))
    table.append("\nCounts and ids only: no chunk, question, evidence or file text.")
    (out_dir / "results.md").write_text("\n".join(table) + "\n", encoding="utf-8")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="chunk_embed_bench.py",
        description="Offline chunk x embedder x reranker recall benchmark (no LightRAG, no ingest).")
    parser.add_argument("--corpus", required=True, help="JSON list of corpus file paths (loop shape)")
    parser.add_argument("--probes", required=True, nargs="+",
                        help="one or two JSONL probe files (items need question + evidence)")
    parser.add_argument("--out", required=True, help="output directory for results.json/.md")
    parser.add_argument("--configs", default=str(Path(__file__).resolve().parent.parent
                                                 / "docs" / "ops" / "loop"
                                                 / "chunk-embed-bench-configs.json"))
    parser.add_argument("--embed-url", required=True, help="model server base URL")
    parser.add_argument("--embed-model", default="mxbai-embed-large-v1")
    parser.add_argument("--rerank-url", default="", help="rerank server base URL (optional)")
    parser.add_argument("--rerank-model", default="")
    parser.add_argument("--api-key-env", default="",
                        help="NAME of the environment variable holding the bearer key")
    parser.add_argument("--cache-dir", default="", help="default: <out>/embed-cache")
    parser.add_argument("--top-k", default=",".join(str(k) for k in TOP_K_DEFAULTS))
    parser.add_argument("--only", nargs="*", default=[], help="run just these config names")
    parser.add_argument("--compare", nargs=2, metavar=("CONFIG_A", "CONFIG_B"),
                        help="paired bootstrap CI for A minus B, per k")
    parser.add_argument("--tokenizer", choices=("auto", "tiktoken", "whitespace"), default="auto")
    parser.add_argument("--timeout", type=float, default=60.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        run(args)
    except BenchError as exc:
        print(f"refusing to run: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
