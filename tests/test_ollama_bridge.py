from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import app.api as api

# Existing test files importlib.reload(app.config), which replaces the
# module-level settings object. Always reach settings through app.api so we
# mutate the instance app.api actually reads.


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
    def __init__(self, content: bytes):
        self.content = content


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
                "multi_query_enabled",
                "rerank_enabled",
                "graph_expansion_enabled",
                "graph_native_enabled",
                "bridge_keyword_supply",
                "bridge_keyword_retries",
            )
        }
        # Existing bridge tests predate the keyword-supply step; pin it off so
        # they exercise exactly the pre-fix path. New keyword tests opt in.
        self.settings.bridge_keyword_supply = False

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.settings, name, value)


class TestTaskPromptDetection(SettingsBackupMixin):
    def test_detects_openwebui_task_prompts(self):
        for prompt in (
            "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history.",
            "### Task:\nGenerate 1-3 broad tags categorizing the main themes of the chat history.",
            "### Task:\nSuggest 3-5 relevant follow-up questions or topics that the user might be interested in.",
            "   \n  ### Task:\nGenerate a concise title.",
        ):
            self.assertTrue(
                __import__("app.api", fromlist=["x"]).is_openwebui_task_prompt(prompt),
                prompt,
            )

    def test_rejects_normal_prompts(self):
        for prompt in (
            "What is the firm horizon task for CMO?",
            "Task: summarise",
            "",
            "## Task: not the OpenWebUI marker",
        ):
            self.assertFalse(
                __import__("app.api", fromlist=["x"]).is_openwebui_task_prompt(prompt),
                prompt,
            )


class TestDirectBridgeTaskShortCircuit(SettingsBackupMixin):
    def test_task_prompt_makes_one_bypass_call_and_no_pipeline(self):
        import app.api as api

        self.settings.bridge_task_shortcircuit = True
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "Chat about eCommit."}])))
        with patch.object(api, "client") as client, patch.object(api, "query", new=AsyncMock()) as query_mock:
            client.proxy = proxy
            answer = _run_async(
                api.answer_ollama_bridge_direct(
                    {
                        "model": "lightrag:latest",
                        "messages": [
                            {
                                "role": "user",
                                "content": (
                                    "### Task:\nGenerate a concise, 3-5 word title with an emoji summarizing the chat history.\n"
                                    "### Chat History:\n<chat_history>\nUSER: What is eCommit?\n"
                                    "ASSISTANT: eCommit is a manual trigger.\n</chat_history>"
                                ),
                            }
                        ],
                    }
                )
            )
        self.assertEqual(answer, "Chat about eCommit.")
        self.assertEqual(proxy.await_count, 1)
        path = proxy.await_args.args[1]
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(path, "/query/stream")
        self.assertEqual(body["mode"], "bypass")
        self.assertFalse(body["include_references"])
        query_mock.assert_not_awaited()

    def test_task_prompt_with_setting_off_uses_normal_path(self):
        import app.api as api

        self.settings.bridge_task_shortcircuit = False
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "full answer"}])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            answer = _run_async(
                api.answer_ollama_bridge_direct(
                    {"messages": [{"role": "user", "content": "### Task:\nGenerate a title."}]}
                )
            )
        self.assertEqual(answer, "full answer")
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(body["mode"], "mix")

    def test_failing_bypass_returns_empty_and_never_falls_through(self):
        import app.api as api

        self.settings.bridge_task_shortcircuit = True
        proxy = AsyncMock(side_effect=RuntimeError("upstream down"))
        with patch.object(api, "client") as client, patch.object(api, "query", new=AsyncMock()) as query_mock:
            client.proxy = proxy
            answer = _run_async(
                api.answer_ollama_bridge_direct(
                    {"messages": [{"role": "user", "content": "### Task:\nGenerate a title."}]}
                )
            )
        self.assertEqual(answer, "")
        self.assertEqual(proxy.await_count, 1)
        query_mock.assert_not_awaited()


class TestExtractOllamaHistory(SettingsBackupMixin):
    def test_excludes_final_user_message_and_system_and_respects_turns(self):
        import app.api as api

        payload = {
            "messages": [
                {"role": "system", "content": "ignore me"},
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
                {"role": "user", "content": "follow-up question"},
            ]
        }
        history = api.extract_ollama_history(payload, turns=3)
        self.assertEqual(
            history,
            [
                {"role": "user", "content": "first question"},
                {"role": "assistant", "content": "first answer"},
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
            ],
        )
        capped = api.extract_ollama_history(payload, turns=1)
        self.assertEqual(
            capped,
            [
                {"role": "user", "content": "second question"},
                {"role": "assistant", "content": "second answer"},
            ],
        )

    def test_strips_sources_blocks(self):
        import app.api as api

        marker = api.SOURCES_MARKER
        payload = {
            "messages": [
                {"role": "user", "content": "q1"},
                {
                    "role": "assistant",
                    "content": "answer body" + marker + "1. doc.pdf — GSK\n2. other.pdf",
                },
                {"role": "user", "content": "q2"},
            ]
        }
        history = api.extract_ollama_history(payload, turns=3)
        self.assertEqual(history[1]["content"], "answer body")

    def test_strips_sources_marker_mid_message(self):
        import app.api as api

        marker = api.SOURCES_MARKER
        payload = {
            "messages": [
                {"role": "assistant", "content": "keep" + marker + "cut" + marker + "cut too"},
                {"role": "user", "content": "prompt"},
            ]
        }
        history = api.extract_ollama_history(payload, turns=3)
        self.assertEqual(history[0]["content"], "keep")

    def test_drops_empty_and_non_string_caps_long(self):
        import app.api as api

        payload = {
            "messages": [
                {"role": "user", "content": ""},
                {"role": "assistant", "content": None},
                {"role": "user", "content": 42},
                {"role": "assistant", "content": "x" * 5000},
                {"role": "user", "content": "prompt"},
            ]
        }
        history = api.extract_ollama_history(payload, turns=5)
        self.assertEqual(len(history), 1)
        self.assertEqual(len(history[0]["content"]), 2000)

    def test_turns_zero_returns_empty(self):
        import app.api as api

        payload = {"messages": [{"role": "user", "content": "a"}, {"role": "user", "content": "b"}]}
        self.assertEqual(api.extract_ollama_history(payload, turns=0), [])
        self.assertEqual(api.extract_ollama_history(payload, turns=-1), [])


class TestDirectBridgePayloadParity(SettingsBackupMixin):
    def test_all_additions_off_sends_hot_patch_payload(self):
        import app.api as api

        self.settings.bridge_task_shortcircuit = False
        self.settings.bridge_history_turns = 0
        self.settings.bridge_sources_enabled = False
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "answer"}])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            _run_async(
                api.answer_ollama_bridge_direct(
                    {
                        "messages": [
                            {"role": "user", "content": "What is eCommit?"},
                        ]
                    }
                )
            )
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(
            body,
            {
                "query": "What is eCommit?",
                "mode": "mix",
                "top_k": 40,
                "chunk_top_k": 20,
                "max_entity_tokens": 10000,
                "max_relation_tokens": 10000,
                "max_total_tokens": 32000,
                "response_type": "Multiple Paragraphs",
                "only_need_context": False,
                "only_need_prompt": False,
                "stream": True,
                "conversation_history": [],
                "user_prompt": "",
                "enable_rerank": True,
                "include_references": True,
                "include_chunk_content": False,
            },
        )

    def test_history_filled_into_direct_payload(self):
        import app.api as api

        self.settings.bridge_history_turns = 2
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "answer"}])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            _run_async(
                api.answer_ollama_bridge_direct(
                    {
                        "messages": [
                            {"role": "user", "content": "What is eCommit?"},
                            {"role": "assistant", "content": "eCommit is a trigger."},
                            {"role": "user", "content": "and who owns that step?"},
                        ]
                    }
                )
            )
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(
            body["conversation_history"],
            [
                {"role": "user", "content": "What is eCommit?"},
                {"role": "assistant", "content": "eCommit is a trigger."},
            ],
        )

    def test_sources_block_appended_from_stream_references(self):
        import app.api as api

        self.settings.bridge_sources_enabled = True
        body = _stream_body(
            [
                {"references": [{"reference_id": "1", "file_path": "docs/ecommit.pdf"}], "response": ""},
                {"response": "eCommit is a manual trigger."},
            ]
        )
        proxy = AsyncMock(return_value=_Resp(body))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            answer = _run_async(
                api.answer_ollama_bridge_direct(
                    {"messages": [{"role": "user", "content": "What is eCommit?"}]}
                )
            )
        self.assertIn("eCommit is a manual trigger.", answer)
        self.assertIn("**Sources**", answer)
        self.assertIn("1. ecommit.pdf", answer)

    def test_sources_off_means_no_block(self):
        import app.api as api

        self.settings.bridge_sources_enabled = False
        body = _stream_body(
            [
                {"references": [{"reference_id": "1", "file_path": "docs/ecommit.pdf"}], "response": ""},
                {"response": "answer"},
            ]
        )
        proxy = AsyncMock(return_value=_Resp(body))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            answer = _run_async(
                api.answer_ollama_bridge_direct(
                    {"messages": [{"role": "user", "content": "What is eCommit?"}]}
                )
            )
        self.assertNotIn("**Sources**", answer)


class TestGatewayPipelineHistoryPayload(SettingsBackupMixin):
    """QueryRequest.conversation_history reaches every LightRAG pass."""

    def _run_query(self, conversation_history):
        import app.api as api
        from app.models import QueryRequest

        self.settings.multi_query_enabled = True
        self.settings.rerank_enabled = False
        self.settings.graph_expansion_enabled = False
        self.settings.graph_native_enabled = False

        payloads: list[dict] = []

        async def fake_post_json(path, payload):
            payloads.append(payload)
            return {"response": "", "references": []}

        client = MagicMock()
        client.post_json = AsyncMock(side_effect=fake_post_json)
        with (
            patch.object(api, "client", client),
            patch.object(api, "get_documents_by_paths", new=AsyncMock(return_value=[])),
            patch.object(api, "generate_rewrites_via_bypass", new=AsyncMock(return_value=["rewrite one"])),
        ):
            envelope = _run_async(api.query(QueryRequest(query="what about the second one?", conversation_history=conversation_history)))
        return payloads, envelope

    def test_no_history_keys_when_empty(self):
        payloads, _ = self._run_query(None)
        self.assertTrue(payloads)
        for payload in payloads:
            self.assertNotIn("conversation_history", payload)
            self.assertNotIn("history_turns", payload)

    def test_history_reaches_primary_multi_query_and_fallback_passes(self):
        history = [{"role": "user", "content": "first"}, {"role": "assistant", "content": "second"}]
        payloads, _ = self._run_query(history)
        # primary + rewrite + naive fallback (weak signal: 0 citations)
        self.assertGreaterEqual(len(payloads), 3)
        modes = [p["mode"] for p in payloads]
        self.assertIn("naive", modes)
        for payload in payloads:
            self.assertEqual(payload["conversation_history"], history)
            self.assertNotIn("history_turns", payload)


class TestGatewayPipelineBridge(SettingsBackupMixin):
    def test_pipeline_backend_uses_query_and_sources_block(self):
        import app.api as api
        from app.models import Envelope

        self.settings.bridge_backend = "gateway_pipeline"
        envelope = Envelope(
            ok=True,
            data={"answer": "pipeline answer", "citations": [{"file_path": "docs/a.pdf"}]},
            meta={},
            error=None,
        )
        with patch.object(api, "query", new=AsyncMock(return_value=envelope)) as query_mock:
            answer = _run_async(
                api.answer_ollama_bridge_prompt(
                    {"messages": [{"role": "user", "content": "normal question"}]}
                )
            )
        query_mock.assert_awaited_once()
        self.assertIn("pipeline answer", answer)
        self.assertIn("**Sources**", answer)
        self.assertIn("1. a.pdf", answer)


class TestAliasCollisions(unittest.TestCase):
    LIGHTRAG_NAMES = {
        "TOP_K",
        "CHUNK_TOP_K",
        "MAX_ENTITY_TOKENS",
        "MAX_RELATION_TOKENS",
        "MAX_TOTAL_TOKENS",
        "RERANK_BY_DEFAULT",
        "COSINE_THRESHOLD",
        "MAX_GLEANING",
        "ENTITY_TYPES",
        "CHUNK_SIZE",
        "CHUNK_OVERLAP_SIZE",
        "WORKERS",
        "TIMEOUT",
        "PORT",
        "MAX_ASYNC",
        "MAX_PARALLEL_INSERT",
    }

    def test_gateway_aliases_never_equal_lightrag_names(self):
        from app.config import Settings

        aliases = {field.alias for field in Settings.model_fields.values() if field.alias}
        collisions = aliases & self.LIGHTRAG_NAMES
        self.assertEqual(collisions, set(), f"gateway aliases collide with LightRAG env names: {collisions}")

    def test_chunk_setting_uses_gateway_prefix(self):
        from app.config import Settings

        self.assertEqual(Settings.model_fields["chunk_top_k"].alias, "GATEWAY_CHUNK_TOP_K")


class StripReasoningTests(SettingsBackupMixin):
    """BRIDGE_STRIP_REASONING: only complete, recognized boundary pairs are
    stripped; orphan markers are left alone."""

    OPEN = "<" + "think" + ">"
    CLOSE = "<" + "/think" + ">"

    def test_complete_pair_stripped(self):
        text = self.OPEN + "\nHere's a thinking process:\n1. blah [1]\n" + self.CLOSE + "\nThe answer is 42 [3]."
        self.assertEqual(api.strip_reasoning_regions(text), "The answer is 42 [3].")

    def test_orphan_open_not_stripped(self):
        text = self.OPEN + "\nthe model rambles but never closes the region"
        self.assertEqual(api.strip_reasoning_regions(text), text)

    def test_orphan_close_not_stripped(self):
        text = "answer text [2]" + self.CLOSE + " trailing"
        self.assertEqual(api.strip_reasoning_regions(text), text)

    def test_multiple_complete_regions_stripped(self):
        text = (self.OPEN + "r1" + self.CLOSE + " A1 " + self.OPEN + "r2" + self.CLOSE + " A2")
        # surrounding whitespace is answer text and is preserved verbatim
        self.assertEqual(api.strip_reasoning_regions(text), "A1  A2")

    def test_answer_markers_untouched(self):
        text = "The answer is 42 [3][8][10]."
        self.assertEqual(api.strip_reasoning_regions(text), text)

    def test_extract_stream_applies_strip_when_enabled(self):
        self.settings.bridge_strip_reasoning = True
        raw = (
            json.dumps({"references": []}).encode() + b"\n"
            + json.dumps({"response": self.OPEN + "thinking [1]" + self.CLOSE}).encode() + b"\n"
            + json.dumps({"response": " real answer [2]."}).encode() + b"\n"
        )
        self.assertEqual(api.extract_stream_response_text(raw), "real answer [2].")

    def test_extract_stream_no_strip_when_disabled(self):
        self.settings.bridge_strip_reasoning = False
        raw = (
            json.dumps({"response": self.OPEN + "thinking [1]" + self.CLOSE}).encode() + b"\n"
            + json.dumps({"response": " real answer [2]."}).encode() + b"\n"
        )
        out = api.extract_stream_response_text(raw)
        self.assertIn(self.OPEN, out)
        self.assertIn("real answer [2].", out)


CANNED_ANSWER = "Sorry, I'm not able to provide an answer to that question.[no-context]"


class TestKeywordSupplyBridge(SettingsBackupMixin):
    """Bridge integration for the issue-08 keyword-supply step."""

    def _payload(self, content="What is the firm horizon task for CMO?"):
        return {"model": "lightrag:latest",
                "messages": [{"role": "user", "content": content}]}

    def _run(self, kw_result, stream_chunks, *, kw_raises=False, supply=True):
        import app.api as api
        from app.keywords import KeywordResult

        self.settings.bridge_keyword_supply = supply
        proxy = AsyncMock(return_value=_Resp(_stream_body(stream_chunks)))
        captured: list[dict] = []
        kw_mock = AsyncMock(side_effect=RuntimeError("kw boom") if kw_raises
                            else None, return_value=kw_result)
        with patch.object(api, "client") as client, \
             patch.object(api, "extract_keywords", kw_mock), \
             patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = lambda fields, **kw: captured.append(kw)
            answer = _run_async(api.answer_ollama_bridge_direct(self._payload()))
        return answer, proxy, kw_mock, captured

    def test_supply_on_adds_keywords_to_payload(self):
        from app.keywords import KeywordResult
        kw = KeywordResult(["firm horizon"], ["CMO"], "llm", 1, [])
        chunks = [{"references": [{"file_path": "a.txt"}]},
                  {"response": "Real answer about the firm horizon."}]
        answer, proxy, kw_mock, captured = self._run(kw, chunks)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertEqual(body["hl_keywords"], ["firm horizon"])
        self.assertEqual(body["ll_keywords"], ["CMO"])
        self.assertIn("Real answer", answer)
        self.assertIn(api.SOURCES_MARKER, answer)
        self.assertEqual(captured[-1]["keyword_source"], "llm")
        self.assertEqual(captured[-1]["keyword_hl_count"], 1)
        self.assertFalse(captured[-1]["honest_failure"])

    def test_supply_off_no_keywords(self):
        chunks = [{"response": "answer"}]
        answer, proxy, kw_mock, captured = self._run(None, chunks, supply=False)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertNotIn("hl_keywords", body)
        kw_mock.assert_not_awaited()
        self.assertEqual(captured[-1]["keyword_source"], "off")

    def test_task_prompt_gets_no_keywords(self):
        import app.api as api
        from app.keywords import KeywordResult
        self.settings.bridge_task_shortcircuit = True
        self.settings.bridge_keyword_supply = True
        captured: list[dict] = []
        kw_mock = AsyncMock(return_value=KeywordResult(["x"], ["y"], "llm", 1, []))
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "Title."}])))
        with patch.object(api, "client") as client, \
             patch.object(api, "extract_keywords", kw_mock), \
             patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = lambda fields, **kw: captured.append(kw)
            answer = _run_async(api.answer_ollama_bridge_direct(
                self._payload("### Task:\nGenerate a concise title.\n"
                              "### Chat History:\n<chat_history>\nUSER: hi\n</chat_history>")))
        self.assertEqual(answer, "Title.")
        kw_mock.assert_not_awaited()
        self.assertEqual(captured[-1]["keyword_source"], "off")

    def test_keyword_step_exception_still_queries_normally(self):
        chunks = [{"response": "normal answer"}]
        answer, proxy, kw_mock, captured = self._run(None, chunks, kw_raises=True)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertNotIn("hl_keywords", body)
        self.assertIn("normal answer", answer)
        self.assertEqual(captured[-1]["keyword_source"], "error")

    def test_fallback_plus_canned_returns_honest_message(self):
        from app.keywords import KeywordResult
        kw = KeywordResult(["cmo"], ["scope"], "fallback", 3, ["array_non_dict_element"])
        chunks = [{"response": CANNED_ANSWER}]
        answer, proxy, kw_mock, captured = self._run(kw, chunks)
        self.assertIn("keyword step failed", answer)
        self.assertNotIn(CANNED_ANSWER, answer)
        self.assertTrue(captured[-1]["honest_failure"])
        self.assertFalse(captured[-1]["canned_failure"])

    def test_fallback_plus_good_answer_is_normal(self):
        from app.keywords import KeywordResult
        kw = KeywordResult(["cmo"], ["scope"], "fallback", 3, ["array_non_dict_element"])
        chunks = [{"references": [{"file_path": "a.txt"}]},
                  {"response": "A good grounded answer."}]
        answer, proxy, kw_mock, captured = self._run(kw, chunks)
        self.assertIn("A good grounded answer.", answer)
        self.assertFalse(captured[-1]["honest_failure"])

    def test_llm_keywords_plus_canned_keeps_canned(self):
        from app.keywords import KeywordResult
        kw = KeywordResult(["cmo"], ["scope"], "llm", 1, [])
        chunks = [{"response": CANNED_ANSWER}]
        answer, proxy, kw_mock, captured = self._run(kw, chunks)
        self.assertIn(CANNED_ANSWER, answer)
        self.assertFalse(captured[-1]["honest_failure"])
        self.assertTrue(captured[-1]["canned_failure"])

    def test_log_line_has_keyword_fields_and_no_keyword_text(self):
        from app.keywords import KeywordResult
        kw = KeywordResult(["eCommit workflow"], ["eCommit"], "llm", 1, [])
        chunks = [{"response": "answer about eCommit"}]
        answer, proxy, kw_mock, captured = self._run(kw, chunks)
        line = captured[-1]
        for field in ("keyword_source", "keyword_attempts", "keyword_failure_reasons",
                      "keyword_hl_count", "keyword_ll_count", "keyword_step_ms",
                      "honest_failure"):
            self.assertIn(field, line)
        dumped = json.dumps(line)
        self.assertNotIn("eCommit", dumped)
        self.assertNotIn("firm horizon", dumped)


if __name__ == "__main__":
    unittest.main()
