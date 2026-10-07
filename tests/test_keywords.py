from __future__ import annotations

import asyncio
import json
import unittest

from app.keywords import (
    REASON_ARRAY_MULTI,
    REASON_ARRAY_NON_DICT,
    REASON_BOTH_EMPTY,
    REASON_EMPTY_TEXT,
    REASON_MISSING_KEYS,
    REASON_NO_JSON,
    REASON_THOUGHT_DUMP,
    REASON_TOOL_CALL,
    REASON_WRONG_TYPES,
    build_keyword_prompt,
    extract_keywords,
    fallback_keywords,
    parse_keyword_payload,
    seed_ll_keywords,
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
        s = _settings()
        self._saved = {
            name: getattr(s, name)
            for name in (
                "bridge_keyword_retries",
                "bridge_keyword_max_items",
                "bridge_keyword_no_think",
            )
        }

    def tearDown(self):
        s = _settings()
        for name, value in self._saved.items():
            setattr(s, name, value)


def _chat(text: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": text}}]}


class TestPrompt(SettingsMixin):
    def test_prompt_contains_query_and_no_placeholders_left(self):
        prompt = build_keyword_prompt("What is eCommit?")
        self.assertIn("User Query: What is eCommit?", prompt)
        self.assertIn("<high_level_keyword>", prompt)  # examples template kept
        self.assertIn("English", prompt)

    def test_prompt_matches_lightrag_shape(self):
        # the LightRAG template ends with the Output marker
        self.assertTrue(build_keyword_prompt("x").rstrip().endswith("Output:"))


class TestParseHappyPath(SettingsMixin):
    def test_plain_dict(self):
        r = parse_keyword_payload(
            '{"high_level_keywords": ["logistics"], "low_level_keywords": ["eCommit"]}')
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.hl, ["logistics"])
        self.assertEqual(r.ll, ["eCommit"])

    def test_fenced_dict(self):
        r = parse_keyword_payload(
            '```json\n{"high_level_keywords": ["a"], "low_level_keywords": ["b"]}\n```')
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.hl, ["a"])

    def test_dict_after_prose(self):
        r = parse_keyword_payload(
            'Sure! Here you go: {"high_level_keywords": ["x"], "low_level_keywords": []}')
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.hl, ["x"])

    def test_dict_after_reasoning_block(self):
        r = parse_keyword_payload(
            OPEN_T + "some reasoning here" + CLOSE_T
            + '\n{"high_level_keywords": ["y"], "low_level_keywords": ["z"]}')
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.hl, ["y"])
        self.assertEqual(r.ll, ["z"])

    def test_single_element_array_wrapping_dict_is_unwrapped(self):
        r = parse_keyword_payload(
            '[{"high_level_keywords": ["a"], "low_level_keywords": ["b"]}]')
        self.assertEqual(r.source, "llm_unwrapped")
        self.assertEqual(r.hl, ["a"])
        self.assertEqual(r.ll, ["b"])


class TestParseRejections(SettingsMixin):
    def test_tool_call_array(self):
        r = parse_keyword_payload('[{"name": "default", "arguments": {}}]')
        self.assertEqual(r.source, "rejected")
        self.assertIn(REASON_TOOL_CALL, r.failure_reasons)

    def test_two_element_array(self):
        r = parse_keyword_payload(
            '[{"high_level_keywords": [], "low_level_keywords": ["a"]},'
            ' {"high_level_keywords": [], "low_level_keywords": ["b"]}]')
        self.assertIn(REASON_ARRAY_MULTI, r.failure_reasons)

    def test_number_array(self):
        r = parse_keyword_payload("[1]")
        self.assertIn(REASON_ARRAY_NON_DICT, r.failure_reasons)
        r = parse_keyword_payload("[1.0, 2.0, 3.0]")
        self.assertIn(REASON_ARRAY_NON_DICT, r.failure_reasons)

    def test_thought_dump_array(self):
        prose = "The user wants me to extract keywords from the query and I think " * 5
        r = parse_keyword_payload(f'[{{"key": "thought", "value": "{prose}"}}]')
        # single dict element without the two expected keys -> missing_keys,
        # and if it did carry them the long prose would trip thought_dump
        self.assertIn(r.failure_reasons[0], (REASON_MISSING_KEYS, REASON_THOUGHT_DUMP))
        r = parse_keyword_payload(
            '{"high_level_keywords": ["' + prose + '"], "low_level_keywords": []}')
        self.assertIn(REASON_THOUGHT_DUMP, r.failure_reasons)

    def test_both_lists_empty(self):
        r = parse_keyword_payload('{"high_level_keywords": [], "low_level_keywords": []}')
        self.assertIn(REASON_BOTH_EMPTY, r.failure_reasons)

    def test_missing_key(self):
        r = parse_keyword_payload('{"high_level_keywords": ["a"]}')
        self.assertIn(REASON_MISSING_KEYS, r.failure_reasons)

    def test_wrong_types(self):
        r = parse_keyword_payload('{"high_level_keywords": "a", "low_level_keywords": ["b"]}')
        self.assertIn(REASON_WRONG_TYPES, r.failure_reasons)

    def test_empty_text(self):
        r = parse_keyword_payload("")
        self.assertIn(REASON_EMPTY_TEXT, r.failure_reasons)
        r = parse_keyword_payload(None)
        self.assertIn(REASON_EMPTY_TEXT, r.failure_reasons)

    def test_non_json(self):
        r = parse_keyword_payload("I cannot extract keywords from that.")
        self.assertIn(REASON_NO_JSON, r.failure_reasons)


class TestNormalization(SettingsMixin):
    def test_dedupe_case_insensitive_and_cap(self):
        s = _settings()
        s.bridge_keyword_max_items = 3
        r = parse_keyword_payload(
            '{"high_level_keywords": ["Alpha", "alpha", "beta", "gamma", "delta"],'
            ' "low_level_keywords": []}')
        self.assertEqual(r.hl, ["Alpha", "beta", "gamma"])

    def test_strip_and_drop_empty_nonstring(self):
        r = parse_keyword_payload(
            '{"high_level_keywords": [" a ", "", 5, null, "b"], "low_level_keywords": []}')
        self.assertEqual(r.hl, ["a", "b"])


class TestRetryAndFallback(SettingsMixin):
    def _poster(self, texts):
        it = iter(texts)

        async def post(body, timeout):
            item = next(it)
            if isinstance(item, Exception):
                raise item
            return _chat(item)
        return post

    def test_bad_bad_good(self):
        s = _settings()
        s.bridge_keyword_retries = 2
        good = '{"high_level_keywords": ["ok"], "low_level_keywords": []}'
        r = _run_async(extract_keywords(
            "what is the firm horizon",
            post=self._poster(["[1]", "not json", good])))
        self.assertEqual(r.source, "llm")
        self.assertEqual(r.attempts, 3)
        self.assertIn(REASON_ARRAY_NON_DICT, r.failure_reasons)

    def test_all_bad_falls_back(self):
        s = _settings()
        s.bridge_keyword_retries = 2
        r = _run_async(extract_keywords(
            "What did the workshop say about CMO scope?",
            post=self._poster(["[1]", "[1]", "[1]"])))
        self.assertEqual(r.source, "fallback")
        self.assertEqual(r.attempts, 3)
        self.assertTrue(r.hl or r.ll)

    def test_http_error_falls_back_never_raises(self):
        s = _settings()
        s.bridge_keyword_retries = 1
        r = _run_async(extract_keywords(
            "hello there", post=self._poster([RuntimeError("boom"), RuntimeError("boom")])))
        self.assertEqual(r.source, "fallback")
        self.assertIn("http_error", r.failure_reasons)

    def test_zero_retries_single_attempt(self):
        s = _settings()
        s.bridge_keyword_retries = 0
        r = _run_async(extract_keywords("hello there", post=self._poster(["[1]"])))
        self.assertEqual(r.source, "fallback")
        self.assertEqual(r.attempts, 1)


class TestFallbackExtractor(SettingsMixin):
    def test_stopwords_dropped_proper_nouns_kept(self):
        hl, ll = fallback_keywords("What did the workshop say about Code Orange?")
        self.assertIn("workshop", hl)
        self.assertIn("Code Orange", ll)
        self.assertNotIn("what", hl)
        self.assertNotIn("the", hl)

    def test_numbers_kept(self):
        hl, ll = fallback_keywords("Summarize the 8-week shipment planning plan")
        self.assertIn("8-week", ll)
        self.assertIn("shipment", hl)

    def test_short_query_whole_phrase(self):
        hl, ll = fallback_keywords("What is eCommit?")
        self.assertIn("What is eCommit?", ll)

    def test_quoted_phrase_kept(self):
        hl, ll = fallback_keywords('Tell me about the "firm horizon" task')
        self.assertIn("firm horizon", ll)

    def test_very_short_query(self):
        hl, ll = fallback_keywords("eCommit")
        self.assertTrue(hl or ll)

    def test_empty_query(self):
        hl, ll = fallback_keywords("")
        self.assertEqual(hl, [])
        self.assertEqual(ll, [])


class TestSeedLlKeywords(SettingsMixin):
    """seed_ll_keywords: pure helper keeping the user's wording in ll (GRAG-41)."""

    def test_appends_last(self):
        out = seed_ll_keywords(["other site"], "and for the other site?", 10)
        self.assertEqual(out, ["other site", "and for the other site?"])

    def test_empty_original_returns_copy_unchanged(self):
        ll = ["other site"]
        out = seed_ll_keywords(ll, "   \n\t ", 10)
        self.assertEqual(out, ["other site"])
        self.assertIsNot(out, ll)

    def test_whitespace_collapsed(self):
        out = seed_ll_keywords([], "and   for\n the  other\r\nsite?", 10)
        self.assertEqual(out, ["and for the other site?"])

    def test_duplicate_case_insensitive_not_added(self):
        out = seed_ll_keywords(["Other Site?"], "other site?", 10)
        self.assertEqual(out, ["Other Site?"])

    def test_cap_respected_when_full(self):
        # Brief rule: keep the first max_items-1 items, seed last.
        out = seed_ll_keywords(["a", "b", "c", "d"], "the original wording", 4)
        self.assertEqual(out, ["a", "b", "c", "the original wording"])
        self.assertEqual(len(out), 4)

    def test_cap_of_one_keeps_only_the_seed(self):
        self.assertEqual(seed_ll_keywords(["a"], "the original wording", 1),
                         ["the original wording"])

    def test_long_original_truncated_to_200(self):
        out = seed_ll_keywords([], "x" * 500, 10)
        self.assertEqual(out, ["x" * 200])

    def test_input_list_not_mutated(self):
        ll = ["a", "b"]
        seed_ll_keywords(ll, "the original wording", 2)
        self.assertEqual(ll, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
