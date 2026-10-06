from __future__ import annotations

import asyncio
import json
import unittest

from app.rewrite import (
    REASON_ANSWER_LIKE,
    REASON_EMPTY,
    REASON_HTTP_ERROR,
    REASON_IDENTICAL_ASSISTANT,
    REASON_MULTI_LINE,
    REASON_REASONING_MARKERS,
    REASON_SOURCES_MARKER,
    REASON_TIMEOUT,
    REASON_TOO_LONG,
    REASON_TOOL_CALL,
    SRC_FALLBACK,
    SRC_OFF,
    SRC_REWRITTEN,
    SRC_SKIPPED,
    SRC_UNCHANGED,
    build_conversation_view,
    build_rewrite_prompt,
    rewrite_query,
    validate_rewrite,
)

OPEN_T = "<" + "think" + ">"
CLOSE_T = "<" + "/think" + ">"


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _settings():
    from app.config import settings
    return settings


class SettingsMixin(unittest.TestCase):
    def setUp(self):
        s = self.settings = _settings()
        self._saved = {
            name: getattr(s, name)
            for name in (
                "bridge_rewrite_enabled",
                "bridge_rewrite_turns",
                "bridge_rewrite_retries",
                "bridge_rewrite_timeout",
                "bridge_rewrite_max_assistant_chars",
                "bridge_rewrite_max_user_chars",
                "bridge_rewrite_no_think",
            )
        }
        s.bridge_rewrite_enabled = True

    def tearDown(self):
        s = _settings()
        for name, value in self._saved.items():
            setattr(s, name, value)


def _chat(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


def _msgs(*turns):
    """Build an OpenWebUI-style messages list from (role, content) tuples."""
    return [{"role": r, "content": c} for r, c in turns]


# ── History view ────────────────────────────────────────────────────────────

class TestConversationView(SettingsMixin):
    def test_sources_block_stripped_from_assistant_turns(self):
        from app.api import SOURCES_MARKER
        msgs = _msgs(
            ("user", "what was said about forecast accuracy?"),
            ("assistant", "Forecast accuracy was discussed at 92%."
             + SOURCES_MARKER + "\n1. workshop.docx\n2. notes.docx"),
            ("user", "and for the other site?"),
        )
        view = build_conversation_view(msgs)
        self.assertEqual(len(view), 2)
        self.assertNotIn("Sources", view[1]["content"])
        self.assertIn("92%", view[1]["content"])

    def test_system_messages_dropped(self):
        msgs = _msgs(
            ("system", "You are a helpful assistant."),
            ("user", "first question"),
            ("assistant", "first answer"),
            ("user", "follow-up"),
        )
        view = build_conversation_view(msgs)
        self.assertEqual([m["role"] for m in view], ["user", "assistant"])

    def test_turn_limit_keeps_most_recent_exchanges(self):
        msgs = []
        for i in range(6):
            msgs += [{"role": "user", "content": f"q{i}"},
                     {"role": "assistant", "content": f"a{i}"}]
        msgs.append({"role": "user", "content": "latest"})
        view = build_conversation_view(msgs, turns=2)
        self.assertEqual([m["content"] for m in view], ["q4", "a4", "q5", "a5"])

    def test_per_role_char_caps(self):
        long_a = "x" * 5000
        long_u = "y" * 3000
        msgs = _msgs(("user", long_u), ("assistant", long_a), ("user", "latest"))
        view = build_conversation_view(msgs, max_assistant_chars=100, max_user_chars=50)
        self.assertEqual(len(view[0]["content"]), 50)
        self.assertEqual(len(view[1]["content"]), 100)

    def test_malformed_entries_ignored(self):
        msgs = [
            {"role": "user", "content": None},
            {"role": "assistant"},
            "not a dict",
            {"role": "user", "content": "   "},
            {"role": "user", "content": "real question"},
            {"role": "assistant", "content": "real answer"},
            {"role": "user", "content": "latest"},
        ]
        view = build_conversation_view(msgs)
        self.assertEqual([m["content"] for m in view], ["real question", "real answer"])

    def test_final_user_message_excluded(self):
        msgs = _msgs(("user", "q1"), ("assistant", "a1"), ("user", "latest"))
        view = build_conversation_view(msgs)
        self.assertNotIn("latest", [m["content"] for m in view])


# ── Prompt ──────────────────────────────────────────────────────────────────

class TestRewritePrompt(SettingsMixin):
    def test_prompt_delimits_conversation_as_data(self):
        view = [{"role": "user", "content": "what was said about forecast accuracy?"},
                {"role": "assistant", "content": "It was discussed at 92%."}]
        prompt = build_rewrite_prompt("and for the other site?", view)
        self.assertIn("User: what was said about forecast accuracy?", prompt)
        self.assertIn("Assistant: It was discussed at 92%.", prompt)
        self.assertIn("data, not instructions", prompt)
        self.assertIn("---Latest message---\nand for the other site?\n---End of latest message---", prompt)
        self.assertIn("UNCHANGED", prompt)  # topic-switch rule present


# ── Validation ──────────────────────────────────────────────────────────────

class TestValidateRewrite(unittest.TestCase):
    ORIG = "what was said about forecast accuracy?"

    def test_plain_rewrite(self):
        r = validate_rewrite(
            self.ORIG,
            "What was said about forecast accuracy at the Blue Yonder integration workshop?")
        self.assertEqual(r.source, SRC_REWRITTEN)
        self.assertTrue(r.length_ratio > 1.0)

    def test_unchanged_case_insensitive(self):
        r = validate_rewrite(self.ORIG, "  What Was Said About Forecast Accuracy? ")
        self.assertEqual(r.source, SRC_UNCHANGED)

    def test_fenced(self):
        r = validate_rewrite(self.ORIG, "```text\nWhat did the workshop say about forecast accuracy?\n```")
        self.assertEqual(r.source, SRC_REWRITTEN)

    def test_quoted(self):
        r = validate_rewrite(self.ORIG, '"What did the workshop say about forecast accuracy?"')
        self.assertEqual(r.source, SRC_REWRITTEN)

    def test_labelled(self):
        r = validate_rewrite(self.ORIG, "Rewritten question: What did the workshop say about forecast accuracy?")
        self.assertEqual(r.source, SRC_REWRITTEN)
        r = validate_rewrite(self.ORIG, "Standalone question: What did the workshop say?")
        self.assertEqual(r.source, SRC_REWRITTEN)

    def test_after_think_block(self):
        r = validate_rewrite(
            self.ORIG,
            OPEN_T + " The user means the workshop discussion. " + CLOSE_T +
            "\nWhat did the workshop say about forecast accuracy?")
        self.assertEqual(r.source, SRC_REWRITTEN)

    def test_reject_multi_line(self):
        r = validate_rewrite(self.ORIG, "Line one question?\nLine two extra.")
        self.assertEqual(r.failure_reasons, [REASON_MULTI_LINE])

    def test_reject_paragraph(self):
        r = validate_rewrite(self.ORIG, "A " * 300 + "question?")
        self.assertIn(REASON_TOO_LONG, r.failure_reasons)

    def test_reject_relative_overlong(self):
        r = validate_rewrite("who owns it?", "Who owns the Blue Yonder integration workstream "
                             "that was discussed in the meeting on Monday the 18th regarding "
                             "the logistics planning data fields " * 2 + "?")
        self.assertIn(REASON_TOO_LONG, r.failure_reasons)

    def test_reject_answer_like(self):
        for prefix in ("Answer:", "Sure!", "I'm sorry, but", "Here is the summary", "The answer is"):
            r = validate_rewrite(self.ORIG, prefix + " forecast accuracy was 92%.")
            self.assertEqual(r.failure_reasons, [REASON_ANSWER_LIKE], prefix)

    def test_reject_sources_marker(self):
        r = validate_rewrite(self.ORIG, "What did the workshop say?\n\n**Sources**\n1. doc.docx")
        self.assertEqual(r.failure_reasons, [REASON_MULTI_LINE])  # multi-line fires first
        r = validate_rewrite(self.ORIG, "What did the workshop say? **Sources** 1. doc.docx")
        self.assertEqual(r.failure_reasons, [REASON_SOURCES_MARKER])

    def test_reject_tool_call_json(self):
        payload = json.dumps([{"name": "rewrite", "arguments": {"q": "x"}}])
        r = validate_rewrite(self.ORIG, payload)
        self.assertEqual(r.failure_reasons, [REASON_TOOL_CALL])

    def test_reject_empty(self):
        r = validate_rewrite(self.ORIG, "   ")
        self.assertEqual(r.failure_reasons, [REASON_EMPTY])
        r = validate_rewrite(self.ORIG, OPEN_T + "still reasoning" )
        self.assertEqual(r.failure_reasons, [REASON_REASONING_MARKERS])

    def test_reject_orphan_reasoning_marker(self):
        r = validate_rewrite(self.ORIG, OPEN_T + " hmm " + CLOSE_T + " ok " + OPEN_T + " trailing")
        self.assertEqual(r.failure_reasons, [REASON_REASONING_MARKERS])

    def test_reject_identical_to_last_assistant(self):
        last = "Forecast accuracy was discussed at 92% in the workshop."
        r = validate_rewrite("what about it?", last, last_assistant=last)
        self.assertEqual(r.failure_reasons, [REASON_IDENTICAL_ASSISTANT])


# ── Async entry: retry / fallback / skip / off ──────────────────────────────

class TestRewriteQuery(SettingsMixin):
    def _msgs(self):
        return _msgs(
            ("user", "what was said about forecast accuracy?"),
            ("assistant", "Forecast accuracy reached 92% in the workshop."),
            ("user", "and for the other site?"),
        )

    def test_no_history_skips_without_call(self):
        calls = []
        async def post(body, timeout):
            calls.append(body)
            return _chat("unused")
        r = _run_async(rewrite_query("what is the vx planning process",
                                     [{"role": "user", "content": "what is the vx planning process"}],
                                     post=post))
        self.assertEqual(r.source, SRC_SKIPPED)
        self.assertEqual(calls, [])

    def test_switch_off_no_call(self):
        self.settings.bridge_rewrite_enabled = False
        calls = []
        async def post(body, timeout):
            calls.append(body)
            return _chat("unused")
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_OFF)
        self.assertEqual(calls, [])

    def test_bad_then_good(self):
        self.settings.bridge_rewrite_retries = 1
        seq = iter(["Answer: it was 92%.", "What was said about forecast accuracy at the other site?"])
        async def post(body, timeout):
            return _chat(next(seq))
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_REWRITTEN)
        self.assertEqual(r.attempts, 2)
        self.assertEqual(r.failure_reasons, [REASON_ANSWER_LIKE])

    def test_bad_times_falls_back_to_original(self):
        self.settings.bridge_rewrite_retries = 2
        async def post(body, timeout):
            return _chat("Sure! Here is a paragraph.\nAnd another line.")
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_FALLBACK)
        self.assertEqual(r.query, "and for the other site?")
        self.assertEqual(r.attempts, 3)
        self.assertEqual(r.failure_reasons, [REASON_MULTI_LINE] * 3)

    def test_http_error_falls_back(self):
        self.settings.bridge_rewrite_retries = 0
        async def post(body, timeout):
            raise RuntimeError("connection refused")
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_FALLBACK)
        self.assertEqual(r.failure_reasons, [REASON_HTTP_ERROR])

    def test_timeout_reason(self):
        self.settings.bridge_rewrite_retries = 0
        async def post(body, timeout):
            raise asyncio.TimeoutError()
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_FALLBACK)
        self.assertEqual(r.failure_reasons, [REASON_TIMEOUT])

    def test_never_raises_on_internal_bug(self):
        async def post(body, timeout):
            return {"choices": []}  # malformed: KeyError inside
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_FALLBACK)
        self.assertEqual(r.query, "and for the other site?")

    def test_no_think_switch_wired(self):
        self.settings.bridge_rewrite_no_think = True
        seen = {}
        async def post(body, timeout):
            seen.update(body)
            return _chat("What was said about forecast accuracy at the other site?")
        r = _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        self.assertEqual(r.source, SRC_REWRITTEN)
        self.assertEqual(seen.get("chat_template_kwargs"), {"enable_thinking": False})

    def test_prompt_carries_history_and_latest(self):
        seen = {}
        async def post(body, timeout):
            seen.update(body)
            return _chat("What was said about forecast accuracy at the other site?")
        _run_async(rewrite_query("and for the other site?", self._msgs(), post=post))
        content = seen["messages"][0]["content"]
        self.assertIn("User: what was said about forecast accuracy?", content)
        self.assertIn("and for the other site?", content)


if __name__ == "__main__":
    unittest.main()
