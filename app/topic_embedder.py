"""The embedder call topic segmentation needs - batched, cached, bounded, and never to the stack.

Why this is a separate module
-----------------------------
Topic cuts ("where does the subject change") are the one place in ingest where our own
code has to ask the model server something. That makes it the place where a mistake is
expensive in a new way: an unbounded embedding loop over a 600-page transcript, an
embedding call that silently points at LightRAG's own port, or a document that fails to
ingest because the model server blinked. So the call lives here, away from the chunker
(which stays pure and takes an injected ``embed``), with the limits written down:

* one batched POST per ``batch`` texts, at most ``max_windows`` texts per document;
* a process-lifetime cache keyed by sha256(model + text), so a re-ingest of an unchanged
  document embeds nothing;
* an origin guard: the LightRAG and gateway ports are refused, always - embedding into
  the stack under production use is how a benchmark ends up changing production;
* any failure raises, and the caller in ``app.topic_segment`` turns it into a recorded
  deterministic fallback. A model server that is down changes HOW we cut, not WHETHER the
  document is ingested.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Sequence
from urllib.parse import urlsplit

import httpx

CACHE_LIMIT = 8192
DEFAULT_BATCH = 16
DEFAULT_TIMEOUT_SECONDS = 15.0


class TopicEmbedError(RuntimeError):
    """The embedder could not answer. The caller falls back and records why."""


def cache_key(model: str, text: str) -> str:
    return hashlib.sha256(f"{model}\x1f{text}".encode("utf-8", "replace")).hexdigest()


class TopicEmbedder:
    """POST {base}/v1/embeddings, batched and cached. Raises TopicEmbedError on any failure."""

    def __init__(self, *, base_url: str, model: str, api_key: str = "",
                 timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS, batch: int = DEFAULT_BATCH,
                 transport: httpx.BaseTransport | None = None,
                 cache: dict[str, list[float]] | None = None) -> None:
        base = (base_url or "").strip().rstrip("/")
        if not base:
            raise TopicEmbedError("no embedder url configured")
        self.base_url = base
        self.model = model
        self.api_key = api_key
        self.batch = max(1, int(batch))
        self.timeout_seconds = float(timeout_seconds)
        self._transport = transport
        self.cache = {} if cache is None else cache
        self.embed_calls = 0
        self.cache_hits = 0

    def __call__(self, texts: Sequence[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            window = texts[start:start + self.batch]
            missing = [text for text in window if cache_key(self.model, text) not in self.cache]
            if missing:
                self._fetch(missing)
            else:
                self.cache_hits += len(window)
            vectors.extend(self.cache[cache_key(self.model, text)] for text in window)
        return vectors

    def _fetch(self, texts: Sequence[str]) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        payload = {"model": self.model, "input": list(texts)}
        try:
            with httpx.Client(timeout=self.timeout_seconds, transport=self._transport) as client:
                response = client.post(f"{self.base_url}/v1/embeddings", json=payload,
                                       headers=headers)
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # unreachable, 500, malformed body: the caller decides
            raise TopicEmbedError(f"{type(exc).__name__}: {str(exc)[:160]}") from exc
        rows = body.get("data") if isinstance(body, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            raise TopicEmbedError(f"embeddings answered {len(rows) if isinstance(rows, list) else 'nothing'} "
                                  f"vectors for {len(texts)} inputs")
        dim = 0
        for row, text in zip(sorted(rows, key=lambda item: item.get("index", 0)), texts):
            vector = row.get("embedding")
            if not isinstance(vector, list) or not vector or not all(
                    isinstance(value, (int, float)) for value in vector):
                raise TopicEmbedError("embedding vector is missing or malformed")
            if dim and len(vector) != dim:
                raise TopicEmbedError("embedder returned vectors of different dimensions")
            dim = len(vector)
            self.cache[cache_key(self.model, text)] = [float(value) for value in vector]
        self.embed_calls += 1
        if len(self.cache) > CACHE_LIMIT:
            # Coarse on purpose: a re-ingest passes over the same texts, and the cost of a
            # proper LRU here is bigger than the cost of re-embedding once.
            self.cache.clear()


def refuse_stack_origin(base_url: str, *, lightrag_url: str, gateway_host_port: str = "",
                        forbidden_ports: Sequence[int] = (9621, 8020)) -> str | None:
    """Return a reason when ``base_url`` points at the stack instead of the model server.

    Embedding through LightRAG or the gateway is not a slow path, it is a different
    system: it costs production capacity, and a benchmark that did it by accident has
    measured the stack rather than the model. Refusing is cheap and says so.
    """
    try:
        target = urlsplit(base_url)
    except ValueError:
        return "embedder url is not parseable"
    if not target.scheme or not target.hostname:
        return "embedder url is missing a scheme or host"
    if target.port and target.port in set(forbidden_ports):
        return (f"embedder url points at port {target.port}, which belongs to the stack "
                "(LightRAG 9621 / gateway 8020), not the model server")
    if lightrag_url:
        compare = urlsplit(lightrag_url)
        if (compare.hostname, compare.port) == (target.hostname, target.port):
            return "embedder url is the LightRAG base url"
    if gateway_host_port:
        gateway = urlsplit(f"//{gateway_host_port}")
        if (gateway.hostname, gateway.port) == (target.hostname, target.port):
            return "embedder url is the gateway"
    return None


def embedder_from_settings(settings: object,
                           transport: httpx.BaseTransport | None = None,
                           cache: dict[str, list[float]] | None = None
                           ) -> tuple[Callable[[Sequence[str]], list[list[float]]] | None,
                                      str | None]:
    """Build the worker's embedder from settings, or explain why there isn't one."""
    base_url = str(getattr(settings, "structure_chunking_embed_url", "") or "").strip()
    if not base_url:
        return None, ("no STRUCTURE_CHUNKING_EMBED_URL: topic cuts use the deterministic "
                      "fallback")
    reason = refuse_stack_origin(base_url,
                                 lightrag_url=str(getattr(settings, "lightrag_base_url", "") or ""))
    if reason:
        return None, reason
    import os
    env_name = str(getattr(settings, "structure_chunking_embed_api_key_env", "") or "")
    api_key = os.environ.get(env_name, "") if env_name else ""
    embedder = TopicEmbedder(
        base_url=base_url,
        model=str(getattr(settings, "structure_chunking_embed_model", "") or "bge-m3-mlx-fp16"),
        api_key=api_key,
        timeout_seconds=float(getattr(settings, "structure_chunking_embed_timeout_seconds",
                                      DEFAULT_TIMEOUT_SECONDS)),
        batch=int(getattr(settings, "structure_chunking_embed_batch", DEFAULT_BATCH)),
        transport=transport, cache=cache)
    return embedder, None
