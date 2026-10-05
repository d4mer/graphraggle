from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

import app.api as api
from app.observability import (
    CANNED_FAILURE_RESPONSE,
    is_canned_failure,
    new_request_id,
    prompt_sha256,
)

# Existing test files importlib.reload(app.config), which replaces the
# module-level settings object. Always reach settings through app.api.


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _stream_body(chunks: list[dict]) -> bytes:
    return "\n".join(json.dumps(c) for c in chunks).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code


class SettingsBackupMixin(unittest.TestCase):
    def setUp(self):
        self.settings = api.settings
        self._saved = {
            name: getattr(self.settings, name)
            for name in (
                "bridge_backend",
                "bridge_task_shortcircuit",
                "bridge_history_turns",
                "bridge_sources_enabled",
                "bridge_strip_reasoning",
                "bridge_top_k",
                "bridge_chunk_top_k",
                "bridge_max_entity_tokens",
                "bridge_max_relation_tokens",
                "bridge_max_total_tokens",
                "bridge_enable_rerank",
                "retrieval_mode_default",
                "chunk_top_k",
                "multi_query_enabled",
                "rerank_enabled",
                "graph_expansion_enabled",
                "graph_native_enabled",
            )
        }

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.settings, name, value)


class _LogCapture:
    """Capture observability stdout lines and QUERY_LOG_PATH appends."""

    def __enter__(self):
        self.buffer = io.StringIO()
        self._redirect = redirect_stdout(self.buffer)
        self._redirect.__enter__()
        self._tmp = tempfile.NamedTemporaryFile("r+", suffix=".jsonl", delete=False)
        self._tmp.close()
        self._saved_env = os.environ.get("QUERY_LOG_PATH")
        os.environ["QUERY_LOG_PATH"] = self._tmp.name
        return self

    def __exit__(self, *exc):
        self._redirect.__exit__(*exc)
        if self._saved_env is None:
            os.environ.pop("QUERY_LOG_PATH", None)
        else:
            os.environ["QUERY_LOG_PATH"] = self._saved_env
        with open(self._tmp.name, "r", encoding="utf-8") as handle:
            self.file_lines = [line for line in handle.read().splitlines() if line.strip()]
        os.unlink(self._tmp.name)
        return False

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.buffer.getvalue().splitlines() if line.strip()]


class CannedFailureDetectionTests(unittest.TestCase):
    def test_exact_canned_string_detected(self):
        self.assertTrue(is_canned_failure(CANNED_FAILURE_RESPONSE))
        self.assertTrue(is_canned_failure("  " + CANNED_FAILURE_RESPONSE + " "))

    def test_canned_string_inside_longer_answer_detected(self):
        self.assertTrue(is_canned_failure("prefix " + CANNED_FAILURE_RESPONSE + " suffix"))

    def test_normal_answer_not_detected(self):
        self.assertFalse(is_canned_failure("eCommit is a manual trigger."))
        self.assertFalse(is_canned_failure(""))
        self.assertFalse(is_canned_failure(None))

    def test_canned_constant_matches_lightrag_source_string(self):
        # The exact string from PROMPTS["fail_response"] in LightRAG v1.5.7.
        self.assertEqual(
            CANNED_FAILURE_RESPONSE,
            "Sorry, I'm not able to provide an answer to that question.[no-context]",
        )


class PromptHashTests(unittest.TestCase):
    def test_hash_is_short_and_stable(self):
        h1 = prompt_sha256("What is eCommit?")
        h2 = prompt_sha256("What is eCommit?")
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 16)
        self.assertNotEqual(h1, prompt_sha256("What is firm horizon?"))

    def test_request_id_shape(self):
        rid = new_request_id()
        self.assertEqual(len(rid), 12)
        self.assertNotEqual(rid, new_request_id())


class BridgeLoggingTests(SettingsBackupMixin):
    def _chat_payload(self, content: str):
        return {"model": "lightrag:latest", "messages": [{"role": "user", "content": content}]}

    def test_one_json_line_per_bridge_request_with_required_fields(self):
        self.settings.bridge_task_shortcircuit = True
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "The answer is 42."}])))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            answer = _run_async(api.answer_ollama_bridge_direct(self._chat_payload("what is 42?"), request_id="abc123def456"))
        self.assertEqual(answer, "The answer is 42.")
        lines = cap.lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["request_id"], "abc123def456")
        self.assertEqual(line["endpoint"], "bridge")
        self.assertEqual(line["status"], 200)
        for field in ("timestamp", "task_prompt", "history_messages", "prompt_sha256",
                      "payload_params", "duration_ms", "answer_chars_raw",
                      "reasoning_chars_removed", "answer_chars_final",
                      "answer_words_final", "reference_count", "canned_failure"):
            self.assertIn(field, line)
        self.assertFalse(line["task_prompt"])
        self.assertFalse(line["canned_failure"])
        self.assertEqual(line["answer_chars_final"], len("The answer is 42."))
        # stdout and QUERY_LOG_PATH must carry the same line
        self.assertEqual(len(cap.file_lines), 1)
        self.assertEqual(json.loads(cap.file_lines[0]), line)

    def test_prompt_text_never_logged(self):
        secret_prompt = "What does the AcmeCorp merger clause say about indemnity?"
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "answer body"}])))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            _run_async(api.answer_ollama_bridge_direct(self._chat_payload(secret_prompt)))
        lines = cap.lines()
        self.assertEqual(len(lines), 1)
        dumped = json.dumps(lines[0])
        self.assertNotIn("merger clause", dumped)
        self.assertNotIn("indemnity", dumped)
        self.assertNotIn("AcmeCorp", dumped)
        self.assertEqual(lines[0]["prompt_sha256"], prompt_sha256(secret_prompt))
        # payload_params must not smuggle the query either
        self.assertNotIn("query", lines[0]["payload_params"])

    def test_reasoning_strip_chars_reported(self):
        self.settings.bridge_strip_reasoning = True
        open_t = "<" + "think" + ">"
        close_t = "<" + "/think" + ">"
        raw_answer = open_t + "long reasoning here" + close_t + " real answer"
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": raw_answer}])))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            answer = _run_async(api.answer_ollama_bridge_direct(self._chat_payload("q")))
        self.assertEqual(answer, " real answer".strip())
        line = cap.lines()[0]
        self.assertEqual(line["answer_chars_raw"], len(raw_answer))
        self.assertEqual(line["reasoning_chars_removed"], len(raw_answer) - len(answer))
        self.assertEqual(line["answer_chars_final"], len(answer))

    def test_canned_failure_flagged_in_bridge_log(self):
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": CANNED_FAILURE_RESPONSE}])))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            answer = _run_async(api.answer_ollama_bridge_direct(self._chat_payload("q")))
        self.assertEqual(answer, CANNED_FAILURE_RESPONSE)
        line = cap.lines()[0]
        self.assertTrue(line["canned_failure"])

    def test_upstream_exception_logs_error_line_and_still_returns_empty(self):
        proxy = AsyncMock(side_effect=RuntimeError("upstream down"))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            answer = _run_async(api.answer_ollama_bridge_direct(self._chat_payload("q")))
        self.assertEqual(answer, "")
        lines = cap.lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], "exception")
        self.assertEqual(lines[0]["exception_class"], "RuntimeError")

    def test_task_prompt_short_circuit_logged_as_task(self):
        self.settings.bridge_task_shortcircuit = True
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "Title: eCommit Chat"}])))
        payload = {
            "model": "lightrag:latest",
            "messages": [{"role": "user", "content": "### Task:\nGenerate a concise title.\n### Chat History:\n<chat_history>\nUSER: hi\n</chat_history>"}],
        }
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            _run_async(api.answer_ollama_bridge_direct(payload))
        line = cap.lines()[0]
        self.assertTrue(line["task_prompt"])
        self.assertEqual(line["payload_params"]["mode"], "bypass")
        self.assertEqual(line["history_messages"], 0)

    def test_broken_logger_still_returns_answer(self):
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "answer survives"}])))
        with patch.object(api, "client") as client, patch.object(api, "emit_bridge_log", side_effect=RuntimeError("logger exploded")):
            client.proxy = proxy
            answer = _run_async(api.answer_ollama_bridge_direct(self._chat_payload("q")))
        self.assertEqual(answer, "answer survives")

    def test_emit_swallows_internal_failures(self):
        from app.observability import emit_bridge_log, start_bridge_log

        fields = start_bridge_log("rid", prompt="p", task_prompt=False, history_count=0, payload_params={})
        with patch("app.observability._emit", side_effect=RuntimeError("boom")):
            emit_bridge_log(fields, status=200)  # must not raise

    def test_bridge_request_id_generated_when_absent(self):
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "x"}])))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.proxy = proxy
            _run_async(api.answer_ollama_bridge_direct(self._chat_payload("q")))
        self.assertEqual(len(cap.lines()[0]["request_id"]), 12)


class QueryLoggingTests(SettingsBackupMixin):
    """The /query route emits exactly one line, id in meta, even on failure."""

    def setUp(self):
        super().setUp()
        self.settings.multi_query_enabled = False
        self.settings.rerank_enabled = False
        self.settings.graph_expansion_enabled = False
        self.settings.graph_native_enabled = False

    def _payload(self, references=None):
        body = {"response": "an answer"}
        if references is not None:
            body["references"] = references
        return {"ok": True, "data": body, "meta": {}, "error": None}

    def test_query_route_logs_one_line_and_meta_carries_request_id(self):
        from app.models import QueryRequest

        post_json = AsyncMock(return_value={"response": "an answer", "references": [{"file_path": "docs/a.pdf"}]})
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.post_json = post_json
            client.proxy = AsyncMock(return_value=_Resp(b'{"documents": []}'))
            with patch.object(api, "get_documents_by_paths", new=AsyncMock(return_value=[])):
                envelope = _run_async(api.query(QueryRequest(query="what is eCommit", top_k=12)))
        lines = cap.lines()
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertEqual(line["endpoint"], "query")
        self.assertEqual(line["query"], "what is eCommit")
        self.assertEqual(line["status"], 200)
        self.assertEqual(line["citations_raw"], 1)
        self.assertEqual(line["citations_final"], 1)
        self.assertFalse(line["canned_failure"])
        self.assertIn("latency_ms_by_stage", line)
        self.assertIn("first_pass", line["latency_ms_by_stage"])
        self.assertEqual(envelope.meta["request_id"], line["request_id"])
        self.assertEqual(len(line["request_id"]), 12)

    def test_query_route_logs_failure_line_then_reraises(self):
        from app.models import QueryRequest

        post_json = AsyncMock(side_effect=RuntimeError("lightrag unreachable"))
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.post_json = post_json
            with self.assertRaises(RuntimeError):
                _run_async(api.query(QueryRequest(query="boom", top_k=12)))
        lines = cap.lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["status"], "exception")
        self.assertEqual(lines[0]["exception_class"], "RuntimeError")
        self.assertEqual(lines[0]["query"], "boom")

    def test_query_canned_failure_flagged(self):
        from app.models import QueryRequest

        post_json = AsyncMock(return_value={"response": CANNED_FAILURE_RESPONSE, "references": []})
        with patch.object(api, "client") as client, _LogCapture() as cap:
            client.post_json = post_json
            with patch.object(api, "get_documents_by_paths", new=AsyncMock(return_value=[])):
                _run_async(api.query(QueryRequest(query="q", top_k=12)))
        line = cap.lines()[0]
        self.assertTrue(line["canned_failure"])


class LogStagesScriptTests(unittest.TestCase):
    """scripts/lightrag_log_stages.py — real production lines as fixtures."""

    SAMPLE = [
        '2026-10-05T06:54:25.945989790Z INFO:  == LLM cache == saving: mix:keywords:8e43ec2a17989a7b4fb1db61a85ee4f3',
        '2026-10-05T06:54:26.295581094Z INFO: Query nodes: logistics workshop transcripts, firm horizon (top_k:40, cosine:0.2)',
        '2026-10-05T06:54:26.513000721Z INFO: Local query: 40 entites, 269 relations',
        '2026-10-05T06:54:26.513638060Z INFO: Query edges: key points, firm horizon (top_k:40, cosine:0.2)',
        '2026-10-05T06:54:26.545315868Z INFO: Global query: 49 entites, 40 relations',
        '2026-10-05T06:54:26.549407646Z INFO: Naive query: 20 chunks (chunk_top_k:20 cosine:0.2)',
        '2026-10-05T06:54:26.551411647Z INFO: Raw search results: 83 entities, 303 relations, 20 vector chunks',
        '2026-10-05T06:54:27.010059690Z INFO: After truncation: 55 entities, 223 relations',
        '2026-10-05T06:54:27.411567741Z INFO: Round-robin merged chunks: 245 -> 225 (deduplicated 20)',
        '2026-10-05T06:55:27.510350448Z WARNING: Rerank func: Worker timeout for task 140616318417792_907885.433148487 after 60s',
        '2026-10-05T06:55:27.510408401Z ERROR: Error during reranking: Rerank func: Worker execution timeout after 60s, using original chunks',
        '2026-10-05T06:55:28.394542532Z INFO: Final context: 55 entities, 223 relations, 11 chunks',
        '2026-10-05T06:55:31.947042944Z INFO: 172.18.0.1:48726 - "POST /query/stream HTTP/1.1" 200',
    ]

    RERANK_OK_SAMPLE = [
        '2026-10-04T19:05:52.311732500Z INFO: Round-robin merged chunks: 173 -> 158 (deduplicated 15)',
        '2026-10-04T19:06:47.727359487Z INFO: Successfully reranked: 20 chunks from 158 original chunks',
        '2026-10-04T19:06:48.309457557Z INFO: Final context: 41 entities, 105 relations, 15 chunks',
        '2026-10-04T19:06:48.313102101Z INFO:  == LLM cache == Query cache hit, using cached response as query result',
        '2026-10-04T19:06:48.314864230Z INFO: 172.18.0.1:54978 - "POST /query HTTP/1.1" 200',
    ]

    EMPTY_KEYWORD_SAMPLE = [
        '2026-10-04T18:00:00.000000000Z WARNING: low_level_keywords is empty',
        '2026-10-04T18:00:00.001000000Z WARNING: high_level_keywords is empty',
        '2026-10-04T18:00:00.002000000Z INFO: Forced low_level_keywords to origin query: What did the workshop say about CMOs being in or out of scope?',
        '2026-10-04T18:00:00.003000000Z INFO: 172.18.0.1:1 - "POST /query/stream HTTP/1.1" 200',
    ]

    def _script(self):
        import importlib.util
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "lightrag_log_stages.py")
        spec = importlib.util.spec_from_file_location("lightrag_log_stages", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_timeout_block_parsed_with_candidates_and_duration(self):
        mod = self._script()
        blocks = mod.parse_lines(self.SAMPLE)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["access_status"], "200")
        self.assertEqual(b["candidates_before_rerank"], 225)
        self.assertIn("timeout after 60s", b["rerank"])
        self.assertEqual(b["rerank_duration_ms"], 60000)
        self.assertEqual(b["chunks_after_rerank"], 11)
        self.assertFalse(b["answer_cache_hit"])
        self.assertIn("non-empty", b["keyword_extraction"])
        # stages the log cannot show must be reported, not guessed
        self.assertEqual(b["embedding_duration_ms"], "not in log")
        self.assertEqual(b["answer_generation_duration_ms"], "not in log")
        self.assertEqual(b["keyword_extraction_duration_ms"], "not in log")

    def test_rerank_ok_and_cache_hit_parsed(self):
        mod = self._script()
        blocks = mod.parse_lines(self.RERANK_OK_SAMPLE)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertEqual(b["candidates_before_rerank"], 158)
        self.assertEqual(b["rerank"], "ok: 158 -> 20 chunks")
        self.assertEqual(b["rerank_duration_ms"], 55416)
        self.assertTrue(b["answer_cache_hit"])
        self.assertEqual(b["chunks_after_rerank"], 15)

    def test_empty_keywords_reported(self):
        mod = self._script()
        blocks = mod.parse_lines(self.EMPTY_KEYWORD_SAMPLE)
        self.assertEqual(len(blocks), 1)
        b = blocks[0]
        self.assertIn("low_level_keywords empty", b["keyword_extraction"])
        self.assertIn("high_level_keywords empty", b["keyword_extraction"])
        self.assertIn("forced to origin query", b["keyword_extraction"])

    def test_main_reads_file_and_prints_table(self):
        mod = self._script()
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
            handle.write("\n".join(self.SAMPLE) + "\n")
            path = handle.name
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = mod.main([path])
            self.assertEqual(rc, 0)
            out = buf.getvalue()
            self.assertIn("query 1", out)
            self.assertIn("not in log", out)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
