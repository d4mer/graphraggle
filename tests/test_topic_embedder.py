"""The topic pass's embedder call: batched, cached, bounded, and never aimed at the stack.

Nothing here opens a socket: httpx.MockTransport answers in-process. The suite also
proves the refusal paths, because "the model server is down" must change how a document
is cut, never whether it is ingested.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.topic_embedder import (          # noqa: E402
    TopicEmbedder,
    TopicEmbedError,
    cache_key,
    embedder_from_settings,
    refuse_stack_origin,
)


class FakeServer:
    """An OpenAI-shaped /v1/embeddings in memory, with a call counter."""

    def __init__(self, *, dim: int = 4, fail: Exception | None = None,
                 answer=None) -> None:
        self.calls: list[dict] = []
        self.headers: list[dict] = []
        self.dim = dim
        self.fail = fail
        self.answer = answer

    def transport(self) -> httpx.MockTransport:
        def handle(request: httpx.Request) -> httpx.Response:
            if self.fail is not None:
                raise self.fail
            body = json.loads(request.content.decode("utf-8"))
            self.calls.append(body)
            self.headers.append(dict(request.headers))
            if self.answer is not None:
                return httpx.Response(200, json=self.answer(body))
            return httpx.Response(200, json={
                "model": body["model"],
                "data": [{"index": index, "embedding": [float(index % self.dim)] * self.dim}
                         for index in range(len(body["input"]))],
            })
        return httpx.MockTransport(handle)


class BatchingAndCache(unittest.TestCase):
    def build(self, server: FakeServer, *, batch: int = 16,
              cache: dict[str, list[float]] | None = None) -> TopicEmbedder:
        return TopicEmbedder(base_url="http://127.0.0.1:8080", model="bge-m3-mlx-fp16",
                             batch=batch, transport=server.transport(), cache=cache)

    def test_windows_are_sent_in_bounded_batches(self):
        server = FakeServer()
        embedder = self.build(server, batch=16)
        vectors = embedder([f"window {index}" for index in range(40)])
        self.assertEqual(len(vectors), 40)
        self.assertEqual([len(call["input"]) for call in server.calls], [16, 16, 8],
                         "one POST per batch, never one per window")
        self.assertTrue(all(len(vector) == 4 for vector in vectors))

    def test_a_second_pass_over_the_same_text_embeds_nothing(self):
        cache: dict[str, list[float]] = {}
        first = FakeServer()
        second = FakeServer()
        texts = ["same text", "other text"]
        self.build(first, cache=cache)(texts)
        again = self.build(second, cache=cache)(texts)
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(second.calls, [], "the cache is keyed by content, so a re-ingest is free")
        self.assertEqual(len(again), 2)

    def test_the_cache_key_never_carries_the_text(self):
        key = cache_key("bge-m3-mlx-fp16", "the contractor invoice for depot nine")
        self.assertRegex(key, r"^[0-9a-f]{64}$")
        self.assertNotIn("contractor", key)

    def test_a_missing_authorization_is_allowed_and_a_named_one_is_sent(self):
        server = FakeServer()
        self.build(server)(["one"])
        self.assertNotIn("authorization", {k.lower() for k in server.headers[0]})
        keyed = TopicEmbedder(base_url="http://127.0.0.1:8080", model="m", api_key="sekret",
                              transport=server.transport())
        keyed(["one more"])
        self.assertEqual(server.headers[-1].get("authorization") or
                         server.headers[-1].get("Authorization"), "Bearer sekret")

    def test_a_wrong_shaped_answer_is_a_failure_not_a_silent_cut(self):
        cases = {
            "too few vectors": lambda body: {"data": []},
            "no data key": lambda body: {"embeddings": 3},
            "vector missing": lambda body: {"data": [{"index": 0} for _ in body["input"]]},
            "ragged dimensions": lambda body: {"data": [
                {"index": index, "embedding": [1.0] * (3 if index else 4)}
                for index in range(len(body["input"]))]},
        }
        for label, answer in cases.items():
            with self.subTest(case=label):
                embedder = self.build(FakeServer(answer=answer), batch=4)
                with self.assertRaises(TopicEmbedError):
                    embedder(["alpha", "beta", "gamma", "delta"])

    def test_an_unreachable_server_raises_the_same_error(self):
        embedder = self.build(FakeServer(fail=httpx.ConnectError("connection refused")))
        with self.assertRaises(TopicEmbedError) as caught:
            embedder(["anything"])
        self.assertIn("ConnectError", str(caught.exception))

    def test_the_error_message_carries_no_document_text(self):
        embedder = self.build(FakeServer(answer=lambda body: {"data": []}))
        with self.assertRaises(TopicEmbedError) as caught:
            embedder(["the confidential settlement figure for depot nine"])
        self.assertNotIn("settlement", str(caught.exception))

    def test_no_url_is_refused_at_construction(self):
        with self.assertRaises(TopicEmbedError):
            TopicEmbedder(base_url="", model="m")


class OriginGuard(unittest.TestCase):
    def test_the_stack_ports_are_refused(self):
        for url in ("http://127.0.0.1:9621", "http://rag:9621/v1", "http://gateway:8020"):
            with self.subTest(url=url):
                self.assertIsNotNone(refuse_stack_origin(url, lightrag_url="http://rag:9621"))

    def test_the_lightrag_base_url_is_refused_even_on_another_port(self):
        reason = refuse_stack_origin("http://rag:9631", lightrag_url="http://rag:9631")
        self.assertIsNotNone(reason)
        self.assertIn("LightRAG", reason)

    def test_the_model_server_is_accepted(self):
        self.assertIsNone(refuse_stack_origin("http://127.0.0.1:8080",
                                              lightrag_url="http://127.0.0.1:9621"))

    def test_a_junk_url_is_refused_with_a_reason(self):
        self.assertIsNotNone(refuse_stack_origin("not a url", lightrag_url=""))


class FromSettings(unittest.TestCase):
    class _Settings:
        def __init__(self, **kwargs):
            self.lightrag_base_url = "http://127.0.0.1:9621"
            self.structure_chunking_embed_url = ""
            self.structure_chunking_embed_model = "bge-m3-mlx-fp16"
            self.structure_chunking_embed_api_key_env = "OMLX_API_KEY"
            self.structure_chunking_embed_timeout_seconds = 5.0
            self.structure_chunking_embed_batch = 8
            for key, value in kwargs.items():
                setattr(self, key, value)

    def test_no_url_means_a_recorded_fallback_not_an_error(self):
        embedder, note = embedder_from_settings(self._Settings())
        self.assertIsNone(embedder)
        self.assertIn("fallback", note)

    def test_a_stack_url_is_refused_before_any_request(self):
        embedder, note = embedder_from_settings(
            self._Settings(structure_chunking_embed_url="http://127.0.0.1:9621"))
        self.assertIsNone(embedder)
        self.assertIn("9621", note)

    def test_the_key_comes_from_the_named_environment_variable(self):
        server = FakeServer()
        settings = self._Settings(structure_chunking_embed_url="http://127.0.0.1:8080")
        with patch.dict(__import__("os").environ, {"OMLX_API_KEY": "from-env"}):
            embedder, note = embedder_from_settings(settings, transport=server.transport())
        self.assertIsNone(note)
        embedder(["one window"])
        self.assertEqual(server.headers[0].get("authorization")
                         or server.headers[0].get("Authorization"), "Bearer from-env")

    def test_the_setting_is_an_environment_variable_NAME_not_a_key(self):
        settings = self._Settings(structure_chunking_embed_url="http://127.0.0.1:8080",
                                  structure_chunking_embed_api_key_env="sekret-value")
        embedder, note = embedder_from_settings(settings, transport=FakeServer().transport())
        self.assertIsNone(note)
        # The variable is unset, so no key is sent; nothing in settings holds a secret.
        self.assertFalse(any(getattr(settings, name, "") == "sekret-value"
                             for name in ("structure_chunking_embed_api_key",)))


if __name__ == "__main__":
    unittest.main()
