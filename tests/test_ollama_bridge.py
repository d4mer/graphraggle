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
                "bridge_rewrite_enabled",
                "bridge_rewrite_turns",
                "bridge_rewrite_retries",
                "bridge_empty_retries",
            )
        }
        # Existing bridge tests predate the keyword-supply step; pin it off so
        # they exercise exactly the pre-fix path. New keyword tests opt in.
        self.settings.bridge_keyword_supply = False
        # Same for the GRAG-41 rewrite step: existing tests exercise the
        # pre-rewrite payload exactly. New rewrite tests opt in.
        self.settings.bridge_rewrite_enabled = False

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



class TestStandaloneRewriteBridge(SettingsBackupMixin):
    """Bridge integration for the GRAG-41 standalone-query rewrite step."""

    def _payload(self, followup="and for the other site?"):
        return {"model": "lightrag:latest", "messages": [
            {"role": "user", "content": "what was said about forecast accuracy?"},
            {"role": "assistant", "content": "Forecast accuracy reached 92% at the workshop."},
            {"role": "user", "content": followup},
        ]}

    def _run(self, rewrite_text, stream_chunks, *, rewrite_raises=False, enabled=True):
        import app.api as api
        from app.rewrite import RewriteResult

        self.settings.bridge_rewrite_enabled = enabled
        proxy = AsyncMock(return_value=_Resp(_stream_body(stream_chunks)))
        captured: list[dict] = []
        rw_mock = AsyncMock(side_effect=RuntimeError("rw boom") if rewrite_raises
                            else None)
        if not rewrite_raises:
            rw_mock.return_value = RewriteResult(
                rewrite_text, "rewritten" if rewrite_text != "and for the other site?" else "unchanged",
                1, [], 2.0)
        with patch.object(api, "client") as client, \
             patch.object(api, "rewrite_query", rw_mock), \
             patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = lambda fields, **kw: captured.append(kw)
            answer = _run_async(api.answer_ollama_bridge_direct(self._payload()))
        return answer, proxy, rw_mock, captured

    def test_rewritten_text_reaches_query_and_user_prompt(self):
        chunks = [{"references": [{"file_path": "workshop.docx"}]},
                  {"response": "Real answer about the other site."}]
        answer, proxy, rw_mock, captured = self._run(
            "What was said about forecast accuracy at the other site?", chunks)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertEqual(body["query"],
                         "What was said about forecast accuracy at the other site?")
        # A3: the user's actual wording rides along so the answer step sees it.
        self.assertEqual(body["user_prompt"], "and for the other site?")
        self.assertIn("Real answer", answer)
        self.assertEqual(captured[-1]["rewrite_source"], "rewritten")
        self.assertTrue(captured[-1]["rewrite_changed"])

    def test_keyword_step_sees_rewritten_query(self):
        import app.api as api
        from app.keywords import KeywordResult
        from app.rewrite import RewriteResult
        self.settings.bridge_rewrite_enabled = True
        self.settings.bridge_keyword_supply = True
        seen: list[str] = []
        async def kw_side(query):
            seen.append(query)
            return KeywordResult(["forecast accuracy"], ["other site"], "llm", 1, [])
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "a"}])))
        with patch.object(api, "client") as client, \
             patch.object(api, "rewrite_query",
                          AsyncMock(return_value=RewriteResult(
                              "What was said about forecast accuracy at the other site?",
                              "rewritten", 1, [], 2.0))), \
             patch.object(api, "extract_keywords", side_effect=kw_side), \
             patch.object(api, "emit_bridge_log"):
            _run_async(api.answer_ollama_bridge_direct(self._payload()))
        self.assertEqual(seen, ["What was said about forecast accuracy at the other site?"])

    def test_unchanged_keeps_today_payload(self):
        chunks = [{"response": "answer"}]
        answer, proxy, rw_mock, captured = self._run("and for the other site?", chunks)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertEqual(body["query"], "and for the other site?")
        self.assertEqual(body["user_prompt"], "")  # byte-identical to pre-GRAG-41
        self.assertEqual(captured[-1]["rewrite_source"], "unchanged")
        self.assertFalse(captured[-1]["rewrite_changed"])

    def test_switch_off_payload_identical(self):
        chunks = [{"response": "answer"}]
        answer, proxy, rw_mock, captured = self._run("ignored", chunks, enabled=False)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertEqual(body["query"], "and for the other site?")
        self.assertEqual(body["user_prompt"], "")
        rw_mock.assert_not_awaited()
        self.assertEqual(captured[-1]["rewrite_source"], "off")

    def test_task_prompt_never_rewrites(self):
        import app.api as api
        self.settings.bridge_task_shortcircuit = True
        self.settings.bridge_rewrite_enabled = True
        captured: list[dict] = []
        rw_mock = AsyncMock(return_value=None)
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "Title."}])))
        with patch.object(api, "client") as client, \
             patch.object(api, "rewrite_query", rw_mock), \
             patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = lambda fields, **kw: captured.append(kw)
            answer = _run_async(api.answer_ollama_bridge_direct(
                self._payload("### Task:\nGenerate a concise title.\n"
                              "### Chat History:\n<chat_history>\nUSER: hi\n</chat_history>")))
        self.assertEqual(answer, "Title.")
        rw_mock.assert_not_awaited()
        self.assertEqual(captured[-1]["rewrite_source"], "off")

    def test_rewrite_exception_still_queries_normally(self):
        chunks = [{"response": "normal answer"}]
        answer, proxy, rw_mock, captured = self._run("x", chunks, rewrite_raises=True)
        body = json.loads(proxy.call_args.kwargs["body"])
        self.assertEqual(body["query"], "and for the other site?")
        self.assertIn("normal answer", answer)
        self.assertEqual(captured[-1]["rewrite_source"], "error")

    def test_no_history_skips_rewrite_call(self):
        import app.api as api
        self.settings.bridge_rewrite_enabled = True
        payload = {"model": "lightrag:latest",
                   "messages": [{"role": "user", "content": "what is the vx planning process"}]}
        rw_mock = AsyncMock()
        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "a"}])))
        with patch.object(api, "client") as client, \
             patch.object(api, "rewrite_query", rw_mock), \
             patch.object(api, "emit_bridge_log"):
            _run_async(api.answer_ollama_bridge_direct(payload))
        # rewrite_query is still invoked (it owns the skip rule) but must not
        # hit the network; the real function returns skipped_no_history.
        self.assertEqual(rw_mock.await_count, 1)

    def test_real_rewrite_skips_without_network(self):
        # No fake post: with no endpoint configured the real rewrite_query
        # must return skipped_no_history for a single-turn chat, never raise.
        import app.api as api
        from app.rewrite import rewrite_query
        r = _run_async(rewrite_query("what is the vx planning process",
                                     [{"role": "user", "content": "what is the vx planning process"}]))
        self.assertEqual(r.source, "skipped_no_history")

class TestEmptyAnswerRetry(SettingsBackupMixin):
    """oMLX early stop: HTTP 200 with zero answer words gets one retry."""

    EMPTY = _stream_body([{"response": ""}])
    ANSWER = _stream_body([{"response": "The vx planning process runs weekly."}])
    CANNED = _stream_body(
        [{"response": "Sorry, I'm not able to provide an answer to that question.[no-context]"}])

    def _payload(self):
        return {"model": "lightrag:latest",
                "messages": [{"role": "user", "content": "what is the vx planning process"}]}

    def _run(self, responses, *, empty_retries=1, kw=None):
        import app.api as api

        self.settings.bridge_empty_retries = empty_retries
        self.settings.bridge_keyword_supply = kw is not None
        self.settings.bridge_rewrite_enabled = False
        proxy = AsyncMock(side_effect=responses)
        captured: list[dict] = []
        with patch.object(api, "client") as client, \
             patch.object(api, "extract_keywords", AsyncMock(return_value=kw)), \
             patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = lambda fields, **kwarg: captured.append(kwarg)
            answer = _run_async(api.answer_ollama_bridge_direct(self._payload()))
        return answer, proxy, captured[-1]

    def test_empty_then_answer_retries_once_with_identical_payload(self):
        answer, proxy, line = self._run([_Resp(self.EMPTY), _Resp(self.ANSWER)])
        self.assertIn("vx planning process runs weekly", answer)
        self.assertEqual(proxy.await_count, 2)
        first = proxy.call_args_list[0].kwargs
        second = proxy.call_args_list[1].kwargs
        self.assertEqual(first, second)  # same body, same headers, same path
        self.assertEqual(line["empty_retries_used"], 1)

    def test_still_empty_after_retry_keeps_todays_result(self):
        answer, proxy, line = self._run([_Resp(self.EMPTY), _Resp(self.EMPTY)])
        self.assertEqual(answer, "")
        self.assertEqual(proxy.await_count, 2)
        self.assertEqual(line["empty_retries_used"], 1)

    def test_still_empty_with_keyword_fallback_keeps_honest_message(self):
        from app.keywords import KeywordResult
        from app.api import KEYWORD_HONEST_FAILURE_MESSAGE
        kw = KeywordResult(["vx planning"], ["vx"], "fallback", 1, ["no_json_found"])
        answer, proxy, line = self._run([_Resp(self.EMPTY), _Resp(self.EMPTY)], kw=kw)
        self.assertEqual(answer, KEYWORD_HONEST_FAILURE_MESSAGE)
        self.assertEqual(proxy.await_count, 2)
        self.assertTrue(line["honest_failure"])
        self.assertEqual(line["empty_retries_used"], 1)

    def test_retries_zero_makes_one_call(self):
        answer, proxy, line = self._run([_Resp(self.EMPTY)], empty_retries=0)
        self.assertEqual(answer, "")
        self.assertEqual(proxy.await_count, 1)
        self.assertEqual(line["empty_retries_used"], 0)

    def test_non_empty_first_answer_is_not_retried(self):
        answer, proxy, line = self._run([_Resp(self.ANSWER)])
        self.assertIn("vx planning process runs weekly", answer)
        self.assertEqual(proxy.await_count, 1)
        self.assertEqual(line["empty_retries_used"], 0)

    def test_exception_is_not_retried(self):
        answer, proxy, line = self._run([RuntimeError("proxy boom")])
        self.assertEqual(answer, "")
        self.assertEqual(proxy.await_count, 1)
        self.assertEqual(type(line.get("exception")).__name__, "RuntimeError")
        self.assertEqual(line["empty_retries_used"], 0)

    def test_canned_answer_is_not_retried(self):
        answer, proxy, line = self._run([_Resp(self.CANNED)])
        self.assertEqual(proxy.await_count, 1)
        self.assertTrue(line["canned_failure"])
        self.assertEqual(line["empty_retries_used"], 0)

    def test_retry_that_raises_keeps_last_empty_response(self):
        answer, proxy, line = self._run([_Resp(self.EMPTY), RuntimeError("boom")])
        self.assertEqual(answer, "")
        self.assertEqual(proxy.await_count, 2)
        self.assertEqual(line["empty_retries_used"], 1)
        self.assertNotIn("exception", line)

    def test_two_retries_when_configured(self):
        answer, proxy, line = self._run(
            [_Resp(self.EMPTY), _Resp(self.EMPTY), _Resp(self.ANSWER)], empty_retries=2)
        self.assertIn("vx planning process runs weekly", answer)
        self.assertEqual(proxy.await_count, 3)
        self.assertEqual(line["empty_retries_used"], 2)

    def test_log_field_is_an_int_and_carries_no_text(self):
        _, _, line = self._run([_Resp(self.EMPTY), _Resp(self.ANSWER)])
        self.assertIsInstance(line["empty_retries_used"], int)
        self.assertNotIsInstance(line["empty_retries_used"], bool)
        dumped = json.dumps(line)
        for text in ("vx planning", "weekly", "Sorry, I'm not able"):
            self.assertNotIn(text, dumped)


class TestCapTaskPromptPure(unittest.TestCase):
    """packet-24: cap_task_prompt is pure and never exceeds the budget."""

    MARKER = api.TASK_PROMPT_TRUNCATION_MARKER

    def _prompt(self, n: int) -> str:
        return "### Task:\nGenerate a title.\n" + "".join(
            f"turn{i} " for i in range(n // 7 + 1))

    def test_below_limit_returned_unchanged(self):
        prompt = self._prompt(1000)
        self.assertEqual(api.cap_task_prompt(prompt, 24000), prompt)

    def test_exactly_at_limit_returned_unchanged(self):
        prompt = self._prompt(1000)
        self.assertEqual(api.cap_task_prompt(prompt, len(prompt)), prompt)

    def test_above_limit_keeps_25pct_head_and_75pct_tail(self):
        prompt, cap = self._prompt(100_000), 24000
        capped = api.cap_task_prompt(prompt, cap)
        self.assertLessEqual(len(capped), cap)
        self.assertTrue(capped.startswith(prompt[: cap // 4]))
        tail_len = cap - cap // 4 - len(self.MARKER)
        self.assertTrue(capped.endswith(prompt[-tail_len:]))
        self.assertEqual(capped.count(self.MARKER), 1)

    def test_max_zero_disables_capping(self):
        prompt = self._prompt(100_000)
        self.assertEqual(api.cap_task_prompt(prompt, 0), prompt)

    def test_negative_max_disables_capping(self):
        prompt = self._prompt(5000)
        self.assertEqual(api.cap_task_prompt(prompt, -1), prompt)

    def test_degenerate_tiny_budget_still_within_budget(self):
        capped = api.cap_task_prompt(self._prompt(1000), 10)
        self.assertLessEqual(len(capped), 10)

    def test_empty_prompt_unchanged(self):
        self.assertEqual(api.cap_task_prompt("", 24000), "")


class TestTaskPromptCap(SettingsBackupMixin):
    """packet-24: BRIDGE_TASK_MAX_CHARS bounds the task prompt forwarded upstream."""

    def setUp(self):
        super().setUp()
        self._saved_task_max = self.settings.bridge_task_max_chars
        self.settings.bridge_task_shortcircuit = True

    def tearDown(self):
        self.settings.bridge_task_max_chars = self._saved_task_max
        super().tearDown()

    def _task_prompt(self, approx_chars: int) -> str:
        header = "### Task:\nGenerate a concise title.\n### Chat History:\n<chat_history>\n"
        tail = "\n</chat_history>"
        turn = "USER: what changed in the eCommit rollout? ASSISTANT: the schedule moved."
        repeats = max(1, (approx_chars - len(header) - len(tail)) // (len(turn) + 1))
        return header + "".join(turn + " " for _ in range(repeats)) + tail

    def _run(self, content: str):
        """Return (answer, query sent upstream, log line) for one task request."""
        import app.api as api

        proxy = AsyncMock(return_value=_Resp(_stream_body([{"response": "Title: rollout"}])))
        captured: list[dict] = []

        def _capture(fields, **kwargs):
            line = {k: v for k, v in fields.items() if not k.startswith("_")}
            line.update(kwargs)
            captured.append(line)

        payload = {"model": "lightrag:latest",
                   "messages": [{"role": "user", "content": content}]}
        with patch.object(api, "client") as client, patch.object(api, "emit_bridge_log") as emit:
            client.proxy = proxy
            emit.side_effect = _capture
            answer = _run_async(api.answer_ollama_bridge_direct(payload))
        self.assertEqual(proxy.await_count, 1)
        body = json.loads(proxy.await_args.kwargs["body"])
        return answer, body["query"], captured[-1]

    def test_short_task_prompt_is_forwarded_verbatim(self):
        self.settings.bridge_task_max_chars = 24000
        prompt = self._task_prompt(1000)
        answer, query, line = self._run(prompt)
        self.assertEqual(query, prompt)
        self.assertEqual(answer, "Title: rollout")

    def test_100k_task_prompt_is_capped_in_the_forwarded_body(self):
        self.settings.bridge_task_max_chars = 24000
        prompt = self._task_prompt(100_000)
        _, query, _ = self._run(prompt)
        self.assertLessEqual(len(query), 24000)
        self.assertLess(len(query), len(prompt))
        self.assertIn(api.TASK_PROMPT_TRUNCATION_MARKER, query)

    def test_cap_zero_forwards_the_whole_prompt(self):
        self.settings.bridge_task_max_chars = 0
        prompt = self._task_prompt(30_000)
        _, query, _ = self._run(prompt)
        self.assertEqual(query, prompt)

    def test_shipped_default_of_the_setting_is_off(self):
        from app.config import Settings

        self.assertEqual(Settings.model_fields["bridge_task_max_chars"].default, 0)

    def test_default_settings_forward_a_100k_task_prompt_unchanged(self):
        from app.config import Settings

        self.settings.bridge_task_max_chars = Settings.model_fields[
            "bridge_task_max_chars"].default
        prompt = self._task_prompt(100_000)
        _, query, line = self._run(prompt)
        self.assertEqual(query, prompt)
        self.assertEqual(line["forwarded_chars"], line["prompt_chars"])
        self.assertGreater(line["prompt_chars"], 100_000 - 100)

    def test_log_records_prompt_chars_and_forwarded_chars(self):
        self.settings.bridge_task_max_chars = 24000
        prompt = self._task_prompt(100_000)
        _, query, line = self._run(prompt)
        self.assertEqual(line["prompt_chars"], len(prompt))
        self.assertEqual(line["forwarded_chars"], len(query))
        self.assertLessEqual(line["forwarded_chars"], self.settings.bridge_task_max_chars)
        self.assertTrue(line["task_prompt"])

    def test_log_carries_no_prompt_text(self):
        self.settings.bridge_task_max_chars = 24000
        line = self._run(self._task_prompt(100_000))[2]
        dumped = json.dumps(line)
        for text in ("eCommit", "rollout", "chat_history", "### Task"):
            self.assertNotIn(text, dumped)

    def test_non_task_request_payload_and_log_unchanged_apart_from_length(self):
        import app.api as api

        self.settings.bridge_task_shortcircuit = True
        self.settings.bridge_task_max_chars = 24000
        answer, query, line = self._run("What is the eCommit manual trigger?")
        self.assertEqual(query, "What is the eCommit manual trigger?")
        self.assertFalse(line["task_prompt"])
        self.assertNotIn("forwarded_chars", line)
        self.assertEqual(line["prompt_chars"], len("What is the eCommit manual trigger?"))


if __name__ == "__main__":
    unittest.main()
