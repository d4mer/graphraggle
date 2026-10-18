"""Offline tests for scripts/chunk_embed_bench.py.

No network: a fake HTTP object answers /v1/embeddings and /v1/rerank with deterministic
hash-bag vectors, so cosine ranking is predictable. The filesystem is a real temp dir
because the corpus/cache/report behaviour is part of what is under test. Synthetic text
only — the brief forbids reading the real corpus or probes.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import chunk_embed_bench as cb  # noqa: E402
import overnight_loop as ol  # noqa: E402

CONFIGS_PATH = ROOT_DIR / "docs" / "ops" / "loop" / "chunk-embed-bench-configs.json"
DIM = 48


def bag_vector(text: str) -> list[float]:
    """Deterministic bag-of-words vector: shared words rank high, unrelated text low."""
    bag = [0.0] * DIM
    for word in ol.normalise_quote(text).replace("\n", " ").split():
        bag[sum(ord(char) for char in word) % DIM] += 1.0
    return bag


class FakeHttp:
    """Answers the two endpoints, records everything it was asked, can fail on cue."""

    def __init__(self, *, fail_first: int = 0, always_fail: bool = False,
                 rerank_body: Any | None = None, rerank_reverses: bool = False) -> None:
        self.embed_calls = 0
        self.embed_batches: list[int] = []
        self.embedded: list[str] = []
        self.headers_seen: list[dict[str, str]] = []
        self.rerank_calls = 0
        self.rerank_queries: list[str] = []
        self.rerank_candidates: list[int] = []
        self.rerank_bodies: list[dict[str, Any]] = []
        self.fail_first = fail_first
        self.always_fail = always_fail
        self.rerank_body = rerank_body
        self.rerank_reverses = rerank_reverses

    def http_json(self, method: str, url: str, *, headers: dict[str, str] | None = None,
                  payload: dict[str, Any] | None = None, timeout: float = 30.0) -> tuple[int, Any]:
        assert method == "POST"
        self.headers_seen.append(dict(headers or {}))
        if url.endswith("/v1/embeddings"):
            inputs = list(payload.get("input") or [])
            self.embed_batches.append(len(inputs))
            self.embedded.extend(inputs)
            self.embed_calls += 1
            if self.always_fail or self.embed_calls <= self.fail_first:
                return 500, {"error": "embedder is asleep"}
            return 200, {"data": [{"embedding": bag_vector(text), "index": index}
                                  for index, text in enumerate(inputs)]}
        if url.endswith("/v1/rerank"):
            self.rerank_calls += 1
            self.rerank_queries.append(str(payload.get("query")))
            self.rerank_bodies.append(dict(payload))
            docs = list(payload.get("documents") or [])
            if not docs:
                docs = [entry["text"] for entry in payload.get("results") or []]
            self.rerank_candidates.append(len(docs))
            if self.rerank_body is not None:
                return 200, self.rerank_body
            order = list(range(len(docs)))
            if self.rerank_reverses:
                order = list(reversed(order))
            return 200, {"results": [{"index": index, "relevance_score": float(len(docs) - rank)}
                                     for rank, index in enumerate(order)]}
        raise AssertionError(f"unexpected URL {url}")


DOC_TRANSCRIPT = (
    "(Ana) The board approved the north budget for the acme contract last Tuesday.\n"
    "(Ben) The rollback path needs a rehearsal before the freeze window opens.\n"
    "(Cara) Legal wants every vendor schedule signed by the end of the month.\n"
    "(Dan) North of the estimate, the acme contract still needs two signatures here.\n"
)
DOC_OTHER = (
    "(Eve) Lunar schedule review: the sample return slips by six weeks in March.\n"
    "(Eve) The drill array ships in two crates, and the lander needs a new heat shield.\n"
)


class _Fixture(unittest.TestCase):
    """Temp root with a corpus.json, two probe files and a small configs file."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "doc1.txt").write_text(DOC_TRANSCRIPT, encoding="utf-8")
        (self.root / "doc2.txt").write_text(DOC_OTHER, encoding="utf-8")
        (self.root / "corpus.json").write_text(
            json.dumps([str(self.root / "doc1.txt"), str(self.root / "doc2.txt")]), encoding="utf-8")
        probes = [
            {"id": "p1", "question": "Which budget did the board approve?",
             "evidence": "The board approved the north budget", "type": "single-fact"},
            {"id": "p2", "question": "What still needs two signatures?",
             "evidence": "the acme contract still needs two signatures", "type": "multi-fact"},
            {"id": "p3", "question": "When does the sample return slip?",
             "evidence": "the sample return slips by six weeks in March", "type": "single-fact"},
            {"id": "p4", "question": "Anything about the weather?", "type": "unanswerable"},
        ]
        (self.root / "probes.jsonl").write_text(
            "\n".join(json.dumps(item) for item in probes) + "\n", encoding="utf-8")

    def configs(self, entries: list[dict[str, Any]]) -> Path:
        path = self.root / "configs.json"
        path.write_text(json.dumps({"configs": entries}), encoding="utf-8")
        return path

    def mxbai(self, name: str, strategy: str, size: int, overlap: int, **extra: Any) -> dict[str, Any]:
        return {"name": name,
                "chunker": {"strategy": strategy, "chunk_token_size": size, "overlap": overlap,
                            **extra},
                "embedder": {"model": "mxbai-embed-large-v1", "max_input_tokens": 512},
                "rerank": None}

    def args(self, *, configs: Path, out: str = "out", extra: list[str] | None = None):
        parser = cb.build_parser()
        argv = ["--corpus", str(self.root / "corpus.json"), "--probes", str(self.root / "probes.jsonl"),
                "--out", out if Path(out).is_absolute() else str(self.root / out),
                "--configs", str(configs), "--embed-url", "http://127.0.0.1:8080",
                "--embed-model", "mxbai-embed-large-v1"]
        argv += extra or []
        return parser.parse_args(argv)

    def run_bench(self, *, configs: Path, http: FakeHttp | None = None,
                  extra: list[str] | None = None) -> tuple[dict[str, Any], FakeHttp]:
        fake = http or FakeHttp()
        args = self.args(configs=configs, extra=extra)
        report = cb.run(args, http=fake, sleep=lambda _seconds: None, print_fn=lambda _line: None)
        return report, fake


class TestEndpointGuard(unittest.TestCase):
    def test_production_ports_are_refused_before_any_request(self):
        for port in (9621, 8020):
            for label in ("http://localhost", "http://127.0.0.1", "http://thinkpad.local"):
                with self.subTest(url=f"{label}:{port}"):
                    reason = cb.endpoint_problem(f"{label}:{port}/v1", what="embedder")
                    self.assertIsNotNone(reason)
                    self.assertIn(f"({port})", reason or "")

    def test_ipv6_and_trailing_path_forms_are_still_caught(self):
        self.assertIsNotNone(cb.endpoint_problem("http://[::1]:8020", what="rerank"))
        self.assertIsNotNone(cb.endpoint_problem("https://user@host:9621/embed", what="embedder"))

    def test_ordinary_model_server_urls_pass(self):
        for url in ("http://127.0.0.1:8080", "http://localhost:8801/v1", "http://thinkpad:62836"):
            with self.subTest(url=url):
                self.assertIsNone(cb.endpoint_problem(url, what="embedder"))

    def test_malformed_urls_are_refused(self):
        for url in ("", "localhost:8080", "ftp://host/x"):
            with self.subTest(url=url):
                self.assertIsNotNone(cb.endpoint_problem(url, what="embedder"))

    def test_main_exits_3_and_makes_no_request_for_a_production_url(self):
        fake = FakeHttp()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "corpus.json").write_text(json.dumps([str(root / "missing.txt")]), encoding="utf-8")
            (root / "probes.jsonl").write_text(json.dumps({"id": "p", "question": "q",
                                                           "evidence": "e"}) + "\n", encoding="utf-8")
            code = cb.main(["--corpus", str(root / "corpus.json"), "--probes", str(root / "probes.jsonl"),
                            "--out", str(root / "out"), "--embed-url", "http://localhost:9621"])
        self.assertEqual(code, 3)
        self.assertEqual(fake.embed_calls, 0)


class TestTokenizers(unittest.TestCase):
    def test_whitespace_fallback_counts_words_and_slices_words(self):
        tokenizer = cb.WhitespaceTokenizer()
        text = "one two three four five"
        self.assertEqual(tokenizer.count(text), 5)
        self.assertEqual(tokenizer.decode_slice(text, 1, 3), "two three")
        self.assertEqual(tokenizer.name, "whitespace-fallback")

    def test_auto_selection_reports_what_it_actually_picked(self):
        tokenizer = cb.build_tokenizer("auto")
        self.assertTrue(tokenizer.name.startswith(("tiktoken:", "whitespace")))

    def test_forcing_tiktoken_fails_loudly_when_it_is_not_usable(self):
        import importlib.util

        if importlib.util.find_spec("tiktoken") is not None:
            self.skipTest("tiktoken is installed here; the fallback path is not exercised")
        with self.assertRaises(cb.BenchError):
            cb.build_tokenizer("tiktoken")


class TestChunkers(unittest.TestCase):
    def setUp(self) -> None:
        self.tokenizer = cb.WhitespaceTokenizer()

    def fixed(self, text: str, size: int, overlap: int) -> list[str]:
        return cb.fixed_token_chunks(text, size, overlap, self.tokenizer)

    def recursive(self, text: str, size: int, overlap: int,
                  separators: tuple[str, ...] = cb.DEFAULT_SEPARATORS) -> list[str]:
        return cb.recursive_chunks(text, size, overlap, separators, self.tokenizer)

    def test_fixed_token_windows_stride_by_size_minus_overlap(self):
        text = " ".join(f"w{index}" for index in range(30))
        chunks = self.fixed(text, 10, 0)
        self.assertEqual([self.tokenizer.count(chunk) for chunk in chunks], [10, 10, 10])
        self.assertEqual(chunks[1].split()[0], "w10")
        overlapped = self.fixed(text, 10, 4)
        self.assertEqual([self.tokenizer.count(chunk) for chunk in overlapped], [10, 10, 10, 10, 6])
        self.assertEqual(overlapped[1].split()[:4], overlapped[0].split()[-4:],
                         "the overlap window must be the tail of the previous chunk")

    def test_recursive_keeps_speaker_turns_whole_and_packs_them(self):
        chunks = self.recursive(DOC_TRANSCRIPT, 24, 0)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            for turn in DOC_TRANSCRIPT.splitlines():
                speaker = turn.split(")")[0] + ")"
                if speaker in chunk:
                    self.assertIn(turn.strip(), " ".join(chunk.split()),
                                  "a turn was split even though a smaller unit was available")
                    break
        words_in_chunks = " ".join(chunks).lower().split()
        self.assertEqual(sorted(words_in_chunks), sorted(DOC_TRANSCRIPT.lower().split()),
                         "every word appears exactly once with overlap 0")

    def test_recursive_never_exceeds_the_token_size_when_pieces_fit(self):
        for size in (16, 24, 40):
            with self.subTest(size=size):
                for chunk in self.recursive(DOC_TRANSCRIPT, size, 6):
                    self.assertLessEqual(self.tokenizer.count(chunk), size)

    def test_recursive_overlap_repeats_whole_pieces_and_never_cuts_a_word(self):
        chunks = self.recursive(DOC_TRANSCRIPT, 40, 13)   # 13-token turns, 3 per chunk
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(self.tokenizer.count(chunk), 40)
            for word in chunk.lower().replace(".", " ").split():
                self.assertIn(word, set(DOC_TRANSCRIPT.lower().replace(".", " ").split()),
                              f"{word!r} is a word fragment")
        shared = [left for left, right in zip(chunks, chunks[1:])
                  if any(turn.strip() and turn.strip() in right for turn in left.splitlines())]
        self.assertGreaterEqual(len(shared), 1,
                                "with overlap 13 at least one whole turn must be repeated")

    def test_recursive_falls_through_the_separator_list_in_order(self):
        """A custom separator list is honoured: "|" is the first boundary tried."""
        text = "alpha | beta | gamma | delta | " * 4
        chunks = self.recursive(text, 5, 0, ("|", " ", ""))
        self.assertGreater(len(chunks), 1)
        allowed = {"alpha", "beta", "gamma", "delta"}
        for chunk in chunks:
            self.assertLessEqual(self.tokenizer.count(chunk), 5)
            self.assertTrue(set(chunk.replace("|", " ").split()) <= allowed, chunk)
        # With " " as the only separator the same text breaks mid-"|" group instead.
        loose = self.recursive(text, 5, 0, (" ", ""))
        self.assertTrue(any("|" in chunk.strip() and chunk.strip().endswith("|") is False
                            for chunk in loose),
                        "a word-level split should leave an unbalanced bar inside a chunk")

    def test_unpunctuated_run_splits_between_words_not_inside_them(self):
        text = " ".join(f"word{index}" for index in range(60))
        chunks = self.recursive(text, 10, 0, cb.DEFAULT_SEPARATORS)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(self.tokenizer.count(chunk), 10)
            for word in chunk.split():
                self.assertIn(word, {f"word{index}" for index in range(60)})

    def test_a_single_oversized_word_stands_alone_instead_of_being_carved(self):
        long_word = "x" * 900
        chunks = self.recursive(f"start {long_word} end", 10, cb.DEFAULT_SEPARATORS)
        self.assertTrue(any(long_word in chunk for chunk in chunks))

    def test_empty_document_produces_no_chunks(self):
        self.assertEqual(self.recursive("   \n\n  ", 10, 0), [])
        self.assertEqual(self.fixed("", 10, 0), [])


class TestSeamAccounting(unittest.TestCase):
    """The cost of small chunks is a gold quote cut in half; that must be visible."""

    def setUp(self) -> None:
        self.tokenizer = cb.WhitespaceTokenizer()
        self.chunks = [
            cb.Chunk(0, 1, 0, "The board approved", 3),
            cb.Chunk(1, 1, 1, "the north budget last Tuesday", 5),
            cb.Chunk(2, 2, 0, "the lunar sample return slips", 5),
        ]
        self.normalised = [ol.normalise_quote(chunk.text) for chunk in self.chunks]
        self.gold = [ol.normalise_quote("The board approved the north budget")]

    def test_a_quote_spanning_two_chunks_is_reported_as_a_seam(self):
        single, pair = cb.quote_location(self.gold, self.normalised, self.chunks)
        self.assertIsNone(single)
        self.assertEqual(pair, (0, 1))

    def test_a_seam_quote_only_counts_when_both_halves_are_retrieved(self):
        both = cb.rank_probe(self.gold, self.chunks, [1, 0, 2], self.normalised)
        self.assertEqual(both["rank"], 2, "the later of the two halves sets the rank")
        self.assertTrue(both["split"])
        separated = cb.rank_probe(self.gold, self.chunks, [0, 2, 1], self.normalised)
        self.assertEqual(separated["rank"], 3, "both halves retrieved, twelve chunks apart")
        hit_at = {k: separated["rank"] <= k for k in (1, 2, 3)}
        self.assertEqual(hit_at, {1: False, 2: False, 3: True},
                         "one half at rank 1 and the other at rank 3 is not a hit@2")

    def test_adjacency_is_per_document(self):
        gold = [ol.normalise_quote("approved the lunar")]
        single, pair = cb.quote_location(gold, self.normalised, self.chunks)
        self.assertIsNone(single)
        self.assertIsNone(pair, "chunks 0 and 2 are not adjacent in the same document")

    def test_a_quote_inside_one_chunk_is_not_a_seam(self):
        gold = [ol.normalise_quote("the north budget")]
        single, pair = cb.quote_location(gold, self.normalised, self.chunks)
        self.assertEqual(single, 1)
        self.assertIsNone(pair)

    def test_aggregate_reports_seam_share_and_recall(self):
        rows = [
            {"id": "a", "type": "single-fact", "rank": 2, "split": False, "quote_in_seam": False,
             "hits": {"5": True, "10": True}},
            {"id": "b", "type": "multi-fact", "rank": 6, "split": True, "quote_in_seam": True,
             "hits": {"5": False, "10": True}},
            {"id": "c", "type": "single-fact", "rank": None, "split": False, "quote_in_seam": False,
             "hits": {"5": False, "10": False}},
        ]
        summary = cb.aggregate_recall(rows, (5, 10))
        self.assertEqual(summary["n"], 3)
        self.assertEqual(summary["recall@5"], 33.33)
        self.assertEqual(summary["recall@10"], 66.67)
        self.assertEqual(summary["seam_quotes"], 1)
        self.assertEqual(summary["cut_by_boundary_pct"], 33.33)
        self.assertAlmostEqual(summary["mrr"], (0.5 + 1 / 6) / 3, places=4)


class TestEmbeddingClient(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.cache = cb.EmbedCache(self.tmp / "cache")

    def client(self, http: FakeHttp, *, sleep: Any = None, cache: cb.EmbedCache | None = None,
               retries: int = cb.HTTP_RETRIES) -> cb.ModelClient:
        return cb.ModelClient(http=http, embed_url="http://127.0.0.1:8080",
                              embed_model="mxbai", rerank_url="http://127.0.0.1:8080",
                              rerank_model="jina", api_key="s3cret",
                              cache=cache or self.cache, sleep=sleep or (lambda _s: None),
                              retries=retries)

    def test_batches_stay_at_sixteen_inputs_or_fewer(self):
        http = FakeHttp()
        vectors = self.client(http).embed([f"chunk number {index}" for index in range(40)])
        self.assertEqual(len(vectors), 40)
        self.assertEqual(http.embed_batches, [16, 16, 8])

    def test_the_cache_key_is_model_plus_prefix_plus_text(self):
        texts = ["same text"]
        http = FakeHttp()
        client = self.client(http)
        client.embed(texts, model="mxbai")
        self.assertEqual(http.embed_calls, 1)
        client.embed(texts, model="mxbai")
        self.assertEqual(http.embed_calls, 1, "a rerun must not re-embed")
        self.assertEqual(self.cache.hits, 1)
        client.embed(texts, model="bge-m3")
        self.assertEqual(http.embed_calls, 2, "a different model is a different vector")
        client.embed(texts, model="mxbai", prefix="query: ")
        self.assertEqual(http.embed_calls, 3, "a different prefix is a different input")
        for name in (self.cache.path_for(self.cache.key("mxbai", "", "same text")).name,):
            self.assertNotIn("same", name)
        body = json.loads(self.cache.path_for(self.cache.key("mxbai", "", "same text"))
                          .read_text(encoding="utf-8"))
        self.assertEqual(set(body), {"model", "dim", "vector"})

    def test_a_second_process_reuses_the_same_cache_directory(self):
        self.client(FakeHttp()).embed(["persisted chunk"])
        http2 = FakeHttp()
        cache2 = cb.EmbedCache(self.tmp / "cache")
        self.client(http2, cache=cache2).embed(["persisted chunk"])
        self.assertEqual(http2.embed_calls, 0)
        self.assertEqual(cache2.hits, 1)

    def test_transient_failures_are_retried_with_backoff(self):
        http = FakeHttp(fail_first=2)
        slept: list[float] = []
        vectors = self.client(http, sleep=slept.append).embed(["need the retry"])
        self.assertEqual(len(vectors), 1)
        self.assertEqual(len(slept), 2, "two waits between three attempts")
        self.assertGreater(slept[1], slept[0], "backoff must grow")

    def test_a_dead_embedder_is_an_error_not_an_empty_result(self):
        http = FakeHttp(always_fail=True)
        with self.assertRaises(cb.BenchError) as caught:
            self.client(http).embed(["nope"])
        self.assertIn("after 3 attempts", str(caught.exception))

    def test_a_malformed_embeddings_body_is_refused(self):
        class Bad(FakeHttp):
            def http_json(self, method, url, **kwargs):
                return 200, {"data": [{"index": 0}]}

        with self.assertRaises(cb.BenchError):
            self.client(Bad()).embed(["one text"])

    def test_the_api_key_is_sent_but_never_named_in_an_error(self):
        http = FakeHttp(always_fail=True)
        with self.assertRaises(cb.BenchError) as caught:
            self.client(http).embed(["one text"])
        for header in http.headers_seen:
            self.assertEqual(header.get("Authorization"), "Bearer s3cret")
        self.assertNotIn("s3cret", str(caught.exception))


class TestRerankClient(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def client(self, http: FakeHttp) -> cb.ModelClient:
        return cb.ModelClient(http=http, embed_url="http://127.0.0.1:8080", embed_model="mxbai",
                              rerank_url="http://127.0.0.1:8080", rerank_model="jina", api_key="",
                              cache=cb.EmbedCache(self.tmp / "cache"), sleep=lambda _s: None)

    def test_the_request_is_the_cohere_shape_o_m_l_x_accepts(self):
        """The Jina body (results of {"text": ...}) gets HTTP 422 from oMLX."""
        http = FakeHttp(rerank_body={"results": [{"index": 0, "relevance_score": 1.0},
                                                 {"index": 1, "relevance_score": 0.2}]})
        self.client(http).rerank_order("which budget", ["first doc", "second doc"], 20)
        self.assertEqual(len(http.rerank_bodies), 1)
        body = http.rerank_bodies[0]
        self.assertEqual(body["documents"], ["first doc", "second doc"])
        self.assertEqual(body["query"], "which budget")
        self.assertEqual(body["top_n"], 2)
        self.assertNotIn("results", body)

    def test_a_data_envelope_response_is_understood(self):
        http = FakeHttp(rerank_body={"data": [{"index": 2, "score": 0.9},
                                              {"index": 0, "score": 0.4}]})
        self.assertEqual(self.client(http).rerank_order("q", ["a", "b", "c"], 3), [2, 0, 1])

    def test_an_unknown_response_envelope_is_named_not_guessed(self):
        http = FakeHttp(rerank_body={"matches": [{"doc": 0}]})
        with self.assertRaises(cb.BenchError) as caught:
            self.client(http).rerank_order("q", ["a", "b"], 2)
        self.assertIn("results[] or data[]", str(caught.exception))

    def test_the_request_is_the_documented_shape(self):
        http = FakeHttp(rerank_body={"results": [{"index": 1, "relevance_score": 0.9},
                                                 {"index": 0, "relevance_score": 0.1}]})
        order = self.client(http).rerank_order("which budget", ["first doc", "second doc"], 20)
        self.assertEqual(order, [1, 0])
        self.assertEqual(http.rerank_candidates, [2])

    def test_a_bare_scores_list_is_also_understood(self):
        http = FakeHttp(rerank_body={"scores": [0.1, 0.7, 0.4]})
        self.assertEqual(self.client(http).rerank_order("q", ["a", "b", "c"], 3), [1, 2, 0])

    def test_an_unusable_rerank_body_fails_loudly(self):
        for body in ({}, {"results": []}, {"results": [{"nonsense": 1}]}):
            with self.subTest(body=body):
                http = FakeHttp(rerank_body=body)
                with self.assertRaises(cb.BenchError):
                    self.client(http).rerank_order("q", ["a", "b"], 2)

class TestConfigFile(unittest.TestCase):
    def test_the_shipped_default_configs_file_is_valid_and_covers_the_arms(self):
        configs = cb.load_configs(CONFIGS_PATH)
        names = [config["name"] for config in configs]
        self.assertEqual(len(names), len(set(names)))
        by_name = {config["name"]: config for config in configs}
        current = next(config for name, config in by_name.items() if "1200" in name and "mxbai" in name)
        self.assertEqual(current["chunker"]["strategy"], "fixed_token")
        self.assertEqual(current["chunker"]["chunk_token_size"], 1200)
        self.assertEqual(current["chunker"]["overlap"], 100)
        sizes = {(config["chunker"]["strategy"], config["chunker"]["chunk_token_size"],
                  config["chunker"]["overlap"], config["embedder"]["model"])
                 for config in configs}
        self.assertIn(("recursive", 350, 50, "mxbai-embed-large-v1"), sizes)
        for size, overlap in ((350, 50), (600, 80), (800, 100), (1200, 100)):
            self.assertIn(("recursive", size, overlap, "bge-m3-mlx-fp16"), sizes)
        self.assertTrue(any(config["rerank"] for config in configs),
                        "the file must include rerank variants")
        self.assertGreaterEqual(sum(1 for config in configs if config["rerank"]), 2)

    def test_the_separator_list_is_the_one_the_decision_memo_asks_for(self):
        configs = cb.load_configs(CONFIGS_PATH)
        separators = [config["chunker"].get("separators") for config in configs
                      if config["chunker"].get("separators")]
        self.assertTrue(separators, "no config overrides the separators")
        expected = ["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""]
        self.assertIn(expected, separators)

    def test_embedder_windows_are_declared(self):
        by_model: dict[str, Any] = {}
        for config in cb.load_configs(CONFIGS_PATH):
            by_model[config["embedder"]["model"]] = config["embedder"].get("max_input_tokens")
        self.assertEqual(by_model.get("mxbai-embed-large-v1"), 512)
        self.assertGreaterEqual(by_model.get("bge-m3-mlx-fp16"), 8192)

    def test_the_shipped_model_ids_are_the_ones_o_m_l_x_serves(self):
        """Hermes' run proved which ids answer; a wrong one silently re-embeds everything."""
        models = {config["embedder"]["model"] for config in cb.load_configs(CONFIGS_PATH)}
        rerankers = {config["rerank"]["model"] for config in cb.load_configs(CONFIGS_PATH)
                     if config["rerank"]}
        self.assertEqual(models, {"mxbai-embed-large-v1", "bge-m3-mlx-fp16"})
        self.assertEqual(rerankers, {"jina-reranker-v3-mlx"})

    def test_the_readme_run_command_names_a_model_that_answers(self):
        """The example command is as likely to be run as the arms themselves."""
        readme = " ".join(json.loads(CONFIGS_PATH.read_text(encoding="utf-8"))["_readme"])
        shipped = {config["embedder"]["model"] for config in cb.load_configs(CONFIGS_PATH)}
        shipped |= {config["rerank"]["model"] for config in cb.load_configs(CONFIGS_PATH)
                    if config["rerank"]}
        named = re.findall(r"--(?:embed|rerank)-model\s+(\S+)", readme)
        self.assertTrue(named, "the runbook shows no model id at all")
        for model in named:
            self.assertIn(model, shipped, f"{model} is not one of the shipped arms")

    def test_bad_configs_are_refused(self):
        cases = {
            "no name": [{"chunker": {"strategy": "recursive"}, "embedder": {"model": "m"}}],
            "unknown strategy": [{"name": "a", "chunker": {"strategy": "semantic_vector"},
                                   "embedder": {"model": "m"}}],
            "overlap not smaller than size": [{"name": "a", "chunker": {"strategy": "recursive",
                                                                        "chunk_token_size": 10,
                                                                        "overlap": 10},
                                               "embedder": {"model": "m"}}],
            "no embedder model": [{"name": "a", "chunker": {"strategy": "recursive"},
                                   "embedder": {}}],
            "rerank without a model": [{"name": "a", "chunker": {"strategy": "recursive"},
                                        "embedder": {"model": "m"}, "rerank": {"top_n_in": 5}}],
            "separators not strings": [{"name": "a", "chunker": {"strategy": "recursive",
                                                                 "separators": [1, 2]},
                                        "embedder": {"model": "m"}}],
        }
        for why, entries in cases.items():
            with self.subTest(why):
                path = Path(tempfile.mkdtemp()) / "configs.json"
                path.write_text(json.dumps({"configs": entries}), encoding="utf-8")
                with self.assertRaises(cb.BenchError):
                    cb.load_configs(path)


class TestEndToEndRun(_Fixture):
    def two_configs(self) -> Path:
        return self.configs([
            self.mxbai("fixed-1200", "fixed_token", 1200, 100),
            self.mxbai("recursive-12", "recursive", 12, 4),
        ])

    def test_a_run_scores_every_config_and_reports_what_it_used(self):
        report, http = self.run_bench(configs=self.two_configs())
        self.assertEqual(report["probes"], 3, "the probe with no evidence is not scored")
        self.assertEqual(report["probes_skipped_no_evidence"], 1)
        self.assertEqual(report["tokenizer"], "whitespace-fallback")
        self.assertEqual([summary["config"] for summary in report["configs"]],
                         ["fixed-1200", "recursive-12"])
        self.assertEqual(report["top_k"], [5, 10, 20, 40])
        self.assertLess(report["http"]["max_batch"], 17)
        for summary in report["configs"]:
            self.assertGreaterEqual(summary["chunks_total"], 1)
            self.assertEqual(summary["probes"], 3)
            self.assertIn("recall@10", summary["recall"])
        fixed = next(s for s in report["configs"] if s["config"] == "fixed-1200")
        self.assertEqual(fixed["chunks_total"], 2, "both docs fit inside 1200 tokens")
        recursive = next(s for s in report["configs"] if s["config"] == "recursive-12")
        self.assertGreater(recursive["chunks_total"], 4)
        self.assertLessEqual(recursive["chunk_tokens"]["max"], 12)

    def test_small_chunks_pay_for_themselves_in_seam_counts(self):
        tiny = self.configs([self.mxbai("tiny", "recursive", 6, 0)])
        report, _http = self.run_bench(configs=tiny)
        summary = report["configs"][0]
        self.assertGreater(summary["chunks_total"], 8)
        self.assertGreater(summary["recall"]["seam_quotes"], 0,
                           "a 6-token window must slice at least one of these quotes")
        self.assertGreater(summary["recall"]["cut_by_boundary_pct"], 0.0)

    def test_reranking_is_a_separate_pass_and_changes_the_order(self):
        configs = self.configs([
            {"name": "rr", "chunker": {"strategy": "recursive", "chunk_token_size": 12,
                                       "overlap": 0},
             "embedder": {"model": "mxbai-embed-large-v1", "max_input_tokens": 512},
             "rerank": {"model": "jina-reranker-v3", "top_n_in": 4}}])
        report, http = self.run_bench(
            configs=configs, http=FakeHttp(rerank_reverses=True),
            extra=["--rerank-url", "http://127.0.0.1:8080", "--rerank-model", "jina-reranker-v3"])
        self.assertEqual(http.rerank_calls, 3, "one rerank call per probe")
        self.assertEqual(set(http.rerank_candidates), {4},
                         "exactly top_n_in=4 candidates go to the reranker per probe")
        summary = report["configs"][0]
        self.assertEqual(summary["rerank"]["model"], "jina-reranker-v3")
        self.assertTrue(all(row["reranked"] for row in report["per_probe"]["rr"]))

    def test_reports_carry_no_question_chunk_or_file_text(self):
        configs = self.configs([self.mxbai("one", "recursive", 12, 4)])
        out = self.root / "out"
        self.run_bench(configs=configs)
        report_text = (out / "results.json").read_text(encoding="utf-8")
        table_text = (out / "results.md").read_text(encoding="utf-8")
        secrets = ["board approved", "north budget", "lunar", "acme contract",
                   "Which budget did the board approve", "doc1.txt", "doc2.txt"]
        for needle in secrets:
            self.assertNotIn(needle, report_text, f"results.json leaked {needle!r}")
            self.assertNotIn(needle, table_text, f"results.md leaked {needle!r}")
        self.assertIn("Counts and ids only", table_text)
        self.assertIn("| config | chunks |", table_text)

    def test_chunks_bigger_than_the_embedder_window_are_counted(self):
        configs = self.configs([self.mxbai("over", "fixed_token", 20, 0)])
        report, _http = self.run_bench(configs=configs)
        summary = report["configs"][0]
        self.assertEqual(summary["chunk_tokens"]["max"], 20)
        self.assertEqual(summary["over_embedder_window_chunks"], 0)
        self.assertEqual(summary["over_embedder_window_pct"], 0.0)
        tight = self.mxbai("tight", "fixed_token", 20, 0)
        tight["embedder"]["max_input_tokens"] = 12
        configs = self.configs([tight])
        report, _http = self.run_bench(configs=configs)
        self.assertGreater(report["configs"][0]["over_embedder_window_chunks"], 0)

    def test_a_second_run_is_all_cache_hits_and_adds_a_config_for_free(self):
        configs = self.configs([self.mxbai("first", "recursive", 12, 0)])
        _report, http = self.run_bench(configs=configs)
        first_calls = http.embed_calls
        configs2 = self.configs([self.mxbai("first", "recursive", 12, 0),
                                 self.mxbai("same-chunker", "recursive", 12, 0)])
        report, http2 = self.run_bench(configs=configs2)
        self.assertEqual(http2.embed_calls, 0, "identical inputs are already cached")
        self.assertGreater(first_calls, 0)
        self.assertTrue(report["cache"]["hits"] > 0)
        self.assertEqual([s["config"] for s in report["configs"]], ["first", "same-chunker"])

    def test_compare_writes_a_paired_section_that_is_reproducible(self):
        configs = self.two_configs()
        first, _http = self.run_bench(configs=configs, extra=["--compare", "fixed-1200", "recursive-12"])
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("## Paired comparison: fixed-1200 minus recursive-12", table)
        self.assertIn("not resolved", table)
        self.assertEqual(first["comparison"]["per_k"]["5"]["n"], 3)
        second, _http2 = self.run_bench(configs=configs, extra=["--compare", "fixed-1200", "recursive-12"])
        self.assertEqual(first["comparison"], second["comparison"],
                         "the same two configs must give the same interval")

    def test_compare_names_unknown_configs_rather_than_guessing(self):
        configs = self.two_configs()
        with self.assertRaises(cb.BenchError):
            self.run_bench(configs=configs, extra=["--compare", "fixed-1200", "nope"])

    def test_a_rerank_config_without_a_url_is_refused_before_any_embedding(self):
        configs = self.configs([{"name": "rr",
                                 "chunker": {"strategy": "recursive", "chunk_token_size": 20,
                                             "overlap": 0},
                                 "embedder": {"model": "mxbai"},
                                 "rerank": {"model": "jina", "top_n_in": 5}}])
        http = FakeHttp()
        args = self.args(configs=configs)
        with self.assertRaises(cb.BenchError) as caught:
            cb.run(args, http=http, sleep=lambda _s: None, print_fn=lambda _line: None)
        self.assertIn("--rerank-url", str(caught.exception))
        self.assertEqual(http.embed_calls, 0)

    def test_only_selects_configs_and_refuses_unknown_names(self):
        configs = self.two_configs()
        report, _http = self.run_bench(configs=configs, extra=["--only", "recursive-12"])
        self.assertEqual([summary["config"] for summary in report["configs"]], ["recursive-12"])
        with self.assertRaises(cb.BenchError):
            self.run_bench(configs=configs, extra=["--only", "ghost"])

    def test_the_api_key_comes_from_the_named_environment_variable(self):
        import os

        os.environ["BENCH_TEST_KEY"] = "env-held-key"
        try:
            _report, http = self.run_bench(configs=self.two_configs(),
                                           extra=["--api-key-env", "BENCH_TEST_KEY"])
        finally:
            del os.environ["BENCH_TEST_KEY"]
        self.assertTrue(http.headers_seen)
        self.assertEqual(http.headers_seen[0].get("Authorization"), "Bearer env-held-key")
        body = (self.root / "out" / "results.json").read_text(encoding="utf-8")
        self.assertNotIn("env-held-key", body)
        self.assertNotIn("env-held-key", (self.root / "out" / "results.md").read_text(encoding="utf-8"))


class TestInputs(_Fixture):
    def test_a_missing_corpus_file_fails_by_position_never_by_name(self):
        (self.root / "corpus.json").write_text(
            json.dumps([str(self.root / "doc1.txt"), "/nowhere/doc2.txt"]), encoding="utf-8")
        with self.assertRaises(cb.BenchError) as caught:
            cb.load_corpus(self.root / "corpus.json")
        self.assertIn("doc 2/2", str(caught.exception))
        self.assertNotIn("nowhere", str(caught.exception))

    def test_a_binary_office_or_pdf_corpus_entry_is_refused_with_an_action(self):
        for suffix in (".pdf", ".docx", ".pptx", ".xlsx", ".epub"):
            with self.subTest(suffix=suffix):
                (self.root / f"deck{suffix}").write_bytes(b"%PDF-1.7 compressed bytes")
                (self.root / "corpus.json").write_text(
                    json.dumps([str(self.root / "doc1.txt"), str(self.root / f"deck{suffix}")]),
                    encoding="utf-8")
                with self.assertRaises(cb.BenchError) as caught:
                    cb.load_corpus(self.root / "corpus.json")
                message = str(caught.exception)
                self.assertIn(f"doc 2/2 is a binary document ({suffix})", message)
                self.assertIn("extract it to .txt first", message)
                self.assertNotIn("deck", message, "the file name must not reach the report")

    def test_text_siblings_of_the_binary_types_are_accepted(self):
        for suffix in (".txt", ".md", ".csv", ".json", ".vtt"):
            with self.subTest(suffix=suffix):
                path = self.root / f"notes{suffix}"
                path.write_text("a plain text document with words in it", encoding="utf-8")
                (self.root / "corpus.json").write_text(json.dumps([str(path)]), encoding="utf-8")
                self.assertEqual(len(cb.load_corpus(self.root / "corpus.json")), 1)

    def test_probes_without_gold_evidence_are_skipped_not_scored(self):
        items, skipped = cb.load_probes([self.root / "probes.jsonl"])
        self.assertEqual([item["id"] for item in items], ["p1", "p2", "p3"])
        self.assertEqual(skipped, 1)

    def test_a_turn_id_counts_as_gold_evidence(self):
        path = self.root / "turns.jsonl"
        path.write_text(json.dumps({"id": "t1", "question": "which turn?", "turn_id": "T-014"}) + "\n",
                        encoding="utf-8")
        items, skipped = cb.load_probes([path])
        self.assertEqual(len(items), 1)
        self.assertEqual(skipped, 0)

    def test_probe_files_may_be_combined_and_ids_stay_unique(self):
        second = self.root / "probes2.jsonl"
        second.write_text(json.dumps({"id": "p1", "question": "again?", "evidence": "north budget"})
                          + "\n", encoding="utf-8")
        items, _skipped = cb.load_probes([self.root / "probes.jsonl", second])
        self.assertEqual(len(items), 4)
        self.assertEqual(len({item["id"] for item in items}), 4)

    def test_no_scoreable_probe_is_an_error(self):
        empty = self.root / "empty.jsonl"
        empty.write_text(json.dumps({"id": "x", "question": "q"}) + "\n", encoding="utf-8")
        with self.assertRaises(cb.BenchError):
            cb.load_probes([empty])


class TestRealHttpRoundTrip(_Fixture):
    """The urllib path, against a throwaway server on an ephemeral port.

    The fakes above skip the transport; this one proves the OpenAI/Jina shapes go out and
    come back through the real client. It never touches a production port - and
    tests/conftest.py refuses the connection if this ever tries one.
    """

    def setUp(self) -> None:
        super().setUp()
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.requests: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:
                pass

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                self.server.seen.append({  # type: ignore[attr-defined]
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "payload": payload,
                })
                if self.path.endswith("/v1/embeddings"):
                    body = {"data": [{"embedding": bag_vector(text), "index": index}
                                     for index, text in enumerate(payload["input"])]}
                else:
                    docs = payload.get("documents") or payload.get("results") or []
                    body = {"results": [{"index": index, "relevance_score": float(len(docs) - rank)}
                                        for rank, index in enumerate(range(len(docs)))]}
                raw = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.seen = []  # type: ignore[attr-defined]
        self.thread = __import__("threading").Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def test_the_cli_runs_end_to_end_and_sends_the_key(self):
        import os

        configs = self.configs([
            {"name": "rec", "chunker": {"strategy": "recursive", "chunk_token_size": 12, "overlap": 3},
             "embedder": {"model": "mxbai-embed-large-v1", "max_input_tokens": 512},
             "rerank": {"model": "jina-reranker-v3", "top_n_in": 3}}])
        os.environ["BENCH_ROUNDTRIP_KEY"] = "roundtrip-key"
        try:
            code = cb.main([
                "--corpus", str(self.root / "corpus.json"), "--probes", str(self.root / "probes.jsonl"),
                "--out", str(self.root / "out"), "--configs", str(configs),
                "--embed-url", self.base_url, "--embed-model", "mxbai-embed-large-v1",
                "--rerank-url", self.base_url, "--rerank-model", "jina-reranker-v3",
                "--api-key-env", "BENCH_ROUNDTRIP_KEY", "--only", "rec"])
        finally:
            del os.environ["BENCH_ROUNDTRIP_KEY"]
        self.assertEqual(code, 0)
        embed_paths = [entry["path"] for entry in self.server.seen if entry["path"].endswith("/v1/embeddings")]
        rerank_paths = [entry["path"] for entry in self.server.seen if entry["path"].endswith("/v1/rerank")]
        self.assertTrue(embed_paths and rerank_paths)
        self.assertTrue(all(entry["authorization"] == "Bearer roundtrip-key" for entry in self.server.seen))
        self.assertTrue(all(len(entry["payload"]["input"]) <= 16 for entry in self.server.seen
                            if "input" in entry["payload"]))
        rerank_payloads = [entry["payload"] for entry in self.server.seen
                           if entry["path"].endswith("/v1/rerank")]
        self.assertTrue(rerank_payloads)
        self.assertTrue(all(isinstance(payload["documents"], list)
                            and all(isinstance(doc, str) for doc in payload["documents"])
                            for payload in rerank_payloads),
                        "oMLX takes documents as plain strings, not Jina {text: ...} objects")
        report = json.loads((self.root / "out" / "results.json").read_text(encoding="utf-8"))
        self.assertEqual(report["configs"][0]["config"], "rec")
        self.assertNotIn("roundtrip-key", (self.root / "out" / "results.md").read_text(encoding="utf-8"))

    def test_a_production_port_is_refused_by_the_cli_without_connecting(self):
        (self.root / "corpus.json").write_text(json.dumps([str(self.root / "doc1.txt")]),
                                               encoding="utf-8")
        code = cb.main(["--corpus", str(self.root / "corpus.json"),
                        "--probes", str(self.root / "probes.jsonl"), "--out", str(self.root / "out2"),
                        "--configs", str(self.configs([self.mxbai("x", "recursive", 20, 0)])),
                        "--embed-url", "http://127.0.0.1:9621"])
        self.assertEqual(code, 3)
        self.assertFalse((self.root / "out2" / "results.json").exists())


class TestEmbedderWindowMeasurement(_Fixture):
    """The 512-token window belongs to the embedder's tokenizer, not to cl100k."""

    def _tokenizer_dir(self) -> Path:
        directory = self.root / "tok"
        directory.mkdir()
        (directory / "vocab.txt").write_text(
            "[CLS]\n[SEP]\n[UNK]\n[PAD]\n[MASK]\na\nb\nthe\nboard\napproved\nnorth\nbudget\n",
            encoding="utf-8")
        return directory

    def test_the_window_is_counted_with_the_embedders_own_vocabulary(self):
        entry = self.mxbai("one", "recursive", 12, 0)
        entry["embedder"]["max_input_tokens"] = 13   # just over the cl100k-style maximum
        configs = self.configs([entry])
        report, _http = self.run_bench(configs=configs,
                                       extra=["--embed-tokenizer", str(self._tokenizer_dir())])
        self.assertTrue(report["embed_tokenizer"]["loaded"])
        self.assertEqual(report["embed_tokenizer"]["kind"], "wordpiece")
        summary = report["configs"][0]
        self.assertIsNotNone(summary["embedder_tokens"])
        self.assertEqual(summary["embedder_window"], 13,
                         "a declared window wins; the 512 default is only the fallback")
        self.assertEqual(report["embedder_window"], 512, "the run-level guard names the default")
        self.assertIn("wordpiece", summary["over_embedder_window_basis"])
        self.assertGreaterEqual(summary["embedder_tokens"]["max"],
                                summary["chunk_tokens"]["max"],
                                "WordPiece never counts cheaper than a word count here")
        self.assertGreater(summary["over_embedder_window_chunks"], 0,
                           "the same chunk fits a 13-token cl100k window and busts a "
                           "13-token embedder window: that is the whole point")
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("Embedder tokenizer:", table)

    def test_without_the_flag_the_report_says_the_window_test_is_a_stand_in(self):
        configs = self.configs([self.mxbai("one", "recursive", 12, 0)])
        report, _http = self.run_bench(configs=configs)
        self.assertFalse(report["embed_tokenizer"]["loaded"])
        self.assertIsNone(report["configs"][0]["embedder_tokens"])
        self.assertIn("no --embed-tokenizer", report["configs"][0]["over_embedder_window_basis"])
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("NOT LOADED", table, "the report must not let this be missed")

    def test_a_tokenizer_path_that_is_not_local_is_refused_before_any_embedding(self):
        configs = self.configs([self.mxbai("one", "recursive", 12, 0)])
        with self.assertRaises(cb.BenchError) as caught:
            self.run_bench(configs=configs, extra=["--embed-tokenizer", "mixedai/mxbai"])
        self.assertIn("never downloads", str(caught.exception))


class TestCoverageGate(_Fixture):
    """Text the chunker ate must never be counted as a retrieval miss."""

    def long_doc(self, sentences: int = 70) -> str:
        return "\n".join(f"line {index} of the long audit report appendix"
                         for index in range(1, sentences + 1)) + "\n"

    def structure_config(self, budget: int) -> Path:
        return self.configs([{"name": "structure-tiny",
                              "chunker": {"strategy": "structure", "budget_tokens": budget,
                                          "header_tokens": 8},
                              "embedder": {"model": "mxbai-embed-large-v1",
                                           "max_input_tokens": 512},
                              "rerank": None}])

    def probe_file_with_a_wide_quote(self, quote: str) -> Path:
        path = self.root / "wide-probes.jsonl"
        path.write_text(json.dumps({"id": "wide-1", "question": "what did the appendix say?",
                                    "evidence": quote, "type": "multi-fact"}) + "\n",
                        encoding="utf-8")
        return path

    def _run_wide(self, budget: int) -> dict[str, Any]:
        (self.root / "doc1.txt").write_text(self.long_doc(), encoding="utf-8")
        quote = " ".join(f"line {index} of the long audit report appendix"
                         for index in range(11, 31))
        probes = self.probe_file_with_a_wide_quote(quote)
        parser = cb.build_parser()
        args = parser.parse_args(["--corpus", str(self.root / "corpus.json"),
                                  "--probes", str(probes),
                                  "--out", str(self.root / "out"),
                                  "--configs", str(self.structure_config(budget)),
                                  "--embed-url", "http://127.0.0.1:8080"])
        return cb.run(args, http=FakeHttp(), sleep=lambda _s: None, print_fn=lambda _l: None)

    def test_a_quote_no_piece_can_hold_is_reported_as_lost_text_not_as_a_miss(self):
        report = self._run_wide(budget=40)
        summary = report["configs"][0]
        self.assertTrue(summary["recall"]["lost_text_ids"])
        self.assertEqual(summary["recall"]["located_pct"], 0.0)
        self.assertEqual(report["lost_text"]["structure_arms"][0]["ids"], ["wide-1"])
        self.assertTrue(report["lost_text"]["fatal"])

    def test_a_budget_big_enough_to_hold_the_quote_reports_no_loss(self):
        report = self._run_wide(budget=400)
        self.assertEqual(report["configs"][0]["recall"]["lost_text_n"], 0)
        self.assertFalse(report["lost_text"]["fatal"])

    def test_the_lost_text_ids_reach_the_report_without_any_text(self):
        self._run_wide(budget=40)
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("LOST TEXT", table)
        self.assertIn("wide-1", table)
        self.assertNotIn("audit report appendix", table, "ids only, never the quote")

    def test_the_exit_code_fails_the_run_unless_overridden(self):
        argv = ["--corpus", "x", "--probes", "y", "--out", "z", "--embed-url",
                "http://127.0.0.1:8080"]
        canned = {"lost_text": {"fatal": True, "structure_arms": [{"config": "structure-350"}]}}
        import unittest.mock as mock
        with mock.patch.object(cb, "run", lambda args: canned):
            self.assertEqual(cb.main(argv), 4)
            self.assertEqual(cb.main(argv + ["--allow-lost-text"]), 0)
        with mock.patch.object(cb, "run", lambda args: {"lost_text": {"fatal": False}}):
            self.assertEqual(cb.main(argv), 0)


class TestTextCopiesAndBudgets(_Fixture):
    def test_a_pdf_entry_is_read_from_its_pre_extracted_copy(self):
        (self.root / "copies").mkdir()
        (self.root / "copies" / "one.txt").write_text("extracted table text\n", encoding="utf-8")
        (self.root / "copies" / "manifest.json").write_text(json.dumps({"1": "one.txt"}),
                                                            encoding="utf-8")
        corpus = self.root / "mixed-corpus.json"
        corpus.write_text(json.dumps([str(self.root / "deck.pdf"),
                                      str(self.root / "doc2.txt")]), encoding="utf-8")
        texts = cb.load_corpus(corpus, str(self.root / "copies"))
        self.assertEqual(texts[0], "extracted table text\n")

    def test_a_binary_entry_without_a_copy_is_still_refused(self):
        corpus = self.root / "mixed-corpus.json"
        corpus.write_text(json.dumps([str(self.root / "deck.pdf")]), encoding="utf-8")
        with self.assertRaises(cb.BenchError) as caught:
            cb.load_corpus(corpus)
        self.assertIn("--text-copies-dir", str(caught.exception))

    def test_a_manifest_position_with_no_file_names_the_position_not_the_path(self):
        (self.root / "copies").mkdir()
        (self.root / "copies" / "manifest.json").write_text(json.dumps({"1": "gone.txt"}),
                                                            encoding="utf-8")
        corpus = self.root / "mixed-corpus.json"
        corpus.write_text(json.dumps([str(self.root / "deck.pdf")]), encoding="utf-8")
        with self.assertRaises(cb.BenchError) as caught:
            cb.load_corpus(corpus, str(self.root / "copies"))
        self.assertIn("1/1", str(caught.exception))

    def test_recall_at_token_budgets_ranks_by_tokens_not_by_chunk_count(self):
        tiny = self.configs([self.mxbai("tiny", "recursive", 6, 0),
                             self.mxbai("big", "fixed_token", 400, 0)])
        report, _http = self.run_bench(configs=tiny, extra=["--budgets", "20,400"])
        for summary in report["configs"]:
            self.assertIn("recall@20tok", summary["recall"])
            self.assertIn("recall@400tok", summary["recall"])
        by_name = {summary["config"]: summary for summary in report["configs"]}
        self.assertLessEqual(by_name["big"]["recall"]["recall@20tok"],
                             by_name["tiny"]["recall"]["recall@20tok"],
                             "one 400-token chunk fills a 20-token window with nothing")
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("recall@20tok%", table)

    def test_the_segment_table_separates_transcript_from_structure_rich_probes(self):
        probes = self.root / "kinds.jsonl"
        probes.write_text("\n".join([
            json.dumps({"id": "t1", "question": "north budget?",
                        "evidence": "The board approved the north budget",
                        "type": "single-fact", "source_kind": "transcript"}),
            json.dumps({"id": "s1", "question": "sample return?",
                        "evidence": "the sample return slips by six weeks in March",
                        "type": "single-fact", "source_kind": "table"}),
        ]) + "\n", encoding="utf-8")
        parser = cb.build_parser()
        args = parser.parse_args(["--corpus", str(self.root / "corpus.json"),
                                  "--probes", str(probes), "--out", str(self.root / "out"),
                                  "--configs", str(self.configs([self.mxbai("one", "recursive",
                                                                           40, 0)])),
                                  "--embed-url", "http://127.0.0.1:8080"])
        cb.run(args, http=FakeHttp(), sleep=lambda _s: None, print_fn=lambda _l: None)
        table = (self.root / "out" / "results.md").read_text(encoding="utf-8")
        self.assertIn("Recall by source kind", table)
        self.assertIn("transcript", table)
        self.assertIn("table + pdf + docx", table)
        self.assertIn("n too small to compare", table, "n=1 must not read as a result")
