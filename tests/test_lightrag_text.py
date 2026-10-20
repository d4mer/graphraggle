"""The text LightRAG is handed is not the text we planned, unless we say so.

``sanitize_text_for_encoding`` runs on the whole document field BEFORE any splitting: it
strips the field, expands HTML entities once, deletes lone surrogates/``U+FFFE``/``U+FFFF``,
then deletes the C0 range apart from tab, newline and CR, then strips again. It keeps every
zero-width and format character and every carriage return. The structure chunker's first
delimiter (``\\x1e``) was in that deleted range: the marker vanished, the document arrived as
one segment, and nothing in the request said so.

Byte-level parity with the image is pinned by ``tests/test_sanitiser_parity.py`` against a
golden fixture produced inside lightrag-server2. This file pins the CONSEQUENCES: what the
plan-time normaliser may and may not do, what a delimiter may be built from, and what the
pre-submit replay refuses.
"""
from __future__ import annotations

import unittest

from app.lightrag_text import (delimiter_unescape_hazard, entity_expansions,
                               is_conservative_marker, is_survivable_delimiter,
                               normalise_for_lightrag, pieces_survive,
                               sanitize_like_lightrag, stripped_characters,
                               unescape_like_lightrag)

MARKER = "\n\n<<<CHUNK-BREAK>>>\n\n"


class TestWhatTheRealFunctionTakes(unittest.TestCase):
    def test_c0_controls_go_tab_newline_and_cr_stay(self):
        for code in list(range(0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) + [0x7F]:
            self.assertEqual(sanitize_like_lightrag(f"a{chr(code)}b"), "ab",
                             f"U+{code:02X} goes, and a delimiter made of it arrives as nothing")
        for kept in ("\n", "\t", "\r"):
            self.assertEqual(sanitize_like_lightrag(f"a{kept}b"), f"a{kept}b")

    def test_carriage_returns_are_kept_not_folded(self):
        # The earlier imitation folded CRLF and lone CR to LF. The image does not: it never
        # touches U+000D. Planning must not pretend otherwise.
        self.assertEqual(sanitize_like_lightrag("a\r\nb"), "a\r\nb")
        self.assertEqual(sanitize_like_lightrag("a\rb"), "a\rb")
        self.assertEqual(normalise_for_lightrag("a\r\nb\rc"), "a\r\nb\rc")

    def test_lone_surrogates_and_two_noncharacters_are_deleted(self):
        for ch in ("\ud800", "\udfff", "\ufffe", "\uffff"):
            self.assertEqual(sanitize_like_lightrag(f"a{ch}b"), "ab")

    def test_zero_width_and_format_characters_survive(self):
        # OrderBook.docx's 46 U+200B stay in the stored text, so they stay in the plan. The
        # extractor must not be asked to compensate.
        for ch in ("\u00ad", "\u200b", "\u200d", "\u202a", "\u2060", "\ufeef", "\ufeff", "\ufff9"):
            self.assertEqual(sanitize_like_lightrag(f"a{ch}b"), f"a{ch}b", f"U+{ord(ch):04X}")
            self.assertEqual(normalise_for_lightrag(f"a{ch}b"), f"a{ch}b")

    def test_entities_expand_once_in_the_image_and_never_here(self):
        self.assertEqual(unescape_like_lightrag("&amp;lt;"), "&lt;",
                         "one pass: the reader sees &lt;, not <")
        self.assertEqual(normalise_for_lightrag("x &amp;lt; y &#x1e; z"), "x &amp;lt; y &#x1e; z",
                         "plan time must not expand: the image would then expand again")
        self.assertEqual(sanitize_like_lightrag("a &#x1e; b"), "a  b",
                         "an entity can synthesise a character the next step deletes")
        self.assertEqual(entity_expansions("a &amp;lt; y &#x1e; z"), 2)

    def test_normalising_deletes_characters_and_leaves_words_alone(self):
        text = "vendor\x00 depot\t tariff\x0c customs\u200b eta\r\ndeck\r\n"
        out = normalise_for_lightrag(text)
        self.assertEqual(out, "vendor depot\t tariff customs\u200b eta\r\ndeck\r\n")
        self.assertEqual(out.replace("\u200b", "").split(),
                         ["vendor", "depot", "tariff", "customs", "eta", "deck"],
                         "characters go; words never do, so the coverage contract still holds")

    def test_the_strip_runs_on_the_whole_field_twice(self):
        self.assertEqual(sanitize_like_lightrag("   hello world   "), "hello world")
        self.assertEqual(sanitize_like_lightrag("   \t\n "), "", "whitespace only collapses to nothing")
        self.assertEqual(sanitize_like_lightrag("x&nbsp;"), "x",
                         "the strip runs AFTER unescaping, so an entity can be stripped away")

    def test_the_strip_runs_before_unescaping_too(self):
        # Order is observable: the leading entity padding is stripped, then what remains of
        # the entity is unescaped. Whatever the reading, the golden fixture owns it.
        self.assertEqual(sanitize_like_lightrag("&#32;x&#32;"), "x")


class TestTheStripReport(unittest.TestCase):
    def test_only_what_the_image_deletes_is_reported(self):
        report = stripped_characters("alpha\x00\x01\x01\x1e\x0c beta")
        # Codepoints, not the characters: a report carrying raw control characters cannot be
        # read in a log line, which is the whole point of the report.
        self.assertEqual(report, {"U+0000": 1, "U+0001": 2, "U+001E": 1, "U+000C": 1})

    def test_kept_characters_are_not_reported(self):
        self.assertEqual(stripped_characters("a\r\nb\tc\u200b\ufeff\ufff9"), {})

    def test_a_lone_surrogate_is_reported_as_what_it_is(self):
        report = stripped_characters("a\ud800b\ufffe")
        self.assertEqual(sorted(report), ["U+D800 lone surrogate or non-character",
                                          "U+FFFE lone surrogate or non-character"])


class TestDelimitersThatSurvive(unittest.TestCase):
    def test_the_default_marker_is_printable_and_conservative(self):
        self.assertTrue(is_survivable_delimiter(MARKER))
        self.assertTrue(is_conservative_marker(MARKER))

    def test_a_control_character_marker_is_not_survivable(self):
        for marker in ("\n\n\x1e\n\n", "\x1e", "\x00x", "\u00ad\x0bx"):
            self.assertFalse(is_survivable_delimiter(marker), marker)

    def test_a_format_character_marker_survives_but_is_not_ours(self):
        # The image keeps format characters, so refusing them "because the sanitiser strips
        # them" would be a lie. They are refused because they are invisible: OUR candidates
        # stay inside the conservative alphabet.
        for marker in ("\n\n\ufeff\n\n", "\n\n\u200b\n\n"):
            self.assertTrue(is_survivable_delimiter(marker), marker)
            self.assertFalse(is_conservative_marker(marker), marker)

    def test_parens_are_survivable_but_not_conservative(self):
        # An explicit delimiter a caller chose is judged on survivability; OUR candidates are
        # held to the narrower alphabet.
        self.assertTrue(is_survivable_delimiter("(Alice Chen)"))
        self.assertFalse(is_conservative_marker("(Alice Chen)"))

    def test_whitespace_only_delimiters_are_never_survivable(self):
        for marker in ("", "   ", "\n\n", "\x1c"):
            self.assertFalse(is_survivable_delimiter(marker), marker)


class TestEntitiesCannotInventACut(unittest.TestCase):
    def test_an_entity_encoded_marker_is_a_hazard(self):
        # The marker needs its newlines, and entities supply those too: this is what a
        # document of escaped markup looks like to the image.
        text = ("&#10;&#10;&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;&#10;&#10;")
        self.assertIn(MARKER, unescape_like_lightrag(text))
        before, after = delimiter_unescape_hazard(text, MARKER)
        self.assertEqual((before, after), (0, 1))

    def test_a_literal_marker_is_not_a_hazard(self):
        self.assertEqual(delimiter_unescape_hazard("a" + MARKER + "b", MARKER), (1, 1))

    def test_the_replay_refuses_a_payload_an_entity_would_recut(self):
        pieces = ["para one",
                  "middle&#10;&#10;&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;&#10;&#10;tail",
                  "para two"]
        payload = MARKER.join(pieces)
        ok, why = pieces_survive(payload, MARKER, pieces)
        self.assertFalse(ok)
        self.assertIn("html entities", why)
        self.assertIn("payload", why)

    def test_a_hazard_held_by_a_piece_alone_is_named_by_index(self):
        # Defence in depth: a caller could hand the replay a payload and piece texts that were
        # not built from each other, and the reason should still say where the problem is.
        pieces = ["para one",
                  "middle&#10;&#10;&#60;&#60;&#60;CHUNK-BREAK&#62;&#62;&#62;&#10;&#10;tail",
                  "two"]
        payload = "para one" + MARKER + "two"
        ok, why = pieces_survive(payload, MARKER, pieces)
        self.assertFalse(ok)
        self.assertIn("piece 1", why)

    def test_a_clean_payload_of_the_same_shape_passes(self):
        pieces = ["para one", "middle tail", "para two"]
        self.assertEqual(pieces_survive(MARKER.join(pieces), MARKER, pieces), (True, ""))


class TestSurvivalReplay(unittest.TestCase):
    def test_pieces_that_round_trip_pass(self):
        pieces = ["[Doc: a | Part 1/2]\nalpha beta", "[Doc: a | Part 2/2]\ngamma delta"]
        self.assertEqual(pieces_survive(MARKER.join(pieces), MARKER, pieces), (True, ""))

    def test_a_delimiter_the_function_deletes_is_refused_by_name(self):
        ok, why = pieces_survive("a\x1eb", "\x1e", ["a", "b"])
        self.assertFalse(ok)
        self.assertIn("deletes", why)

    def test_a_payload_whose_marker_lost_its_edges_rejects(self):
        # What a trailing delimiter becomes once the whole field is stripped: the word survives,
        # the newlines do not, so the exact delimiter is no longer in the payload.
        pieces = ["alpha", "beta"]
        ok, why = pieces_survive(MARKER.join(pieces) + MARKER, MARKER, pieces)
        self.assertFalse(ok)
        self.assertIn("piece 1", why)
        self.assertIn("differ", why)

    def test_payload_text_that_is_not_the_plan_is_named_by_index(self):
        pieces = ["alpha", "beta"]
        ok, why = pieces_survive("alpha" + MARKER + "beta extra", MARKER, pieces)
        self.assertFalse(ok)
        self.assertIn("piece 1", why)

    def test_a_deleted_character_does_not_count_as_a_change(self):
        # The plan normalises these away, so a payload that is already clean compares exactly;
        # a NUL removed by the image is not a surprise and must not be reported as one.
        pieces = ["alpha", "beta"]
        payload = "alpha" + MARKER + "bet\x00a"
        self.assertEqual(pieces_survive(payload, MARKER, pieces), (True, ""),
                         "the expectation is the STORED form of the piece, not its raw text")

    def test_the_field_strip_bites_only_at_the_payload_edges(self):
        # What the real function does to padded pieces: the payload's leading whitespace is
        # stripped, an interior piece's padding is kept, the trailing padding is stripped.
        pieces = ["  alpha  ", "  beta  ", "  gamma  "]
        payload = MARKER.join(pieces)
        ok, why = pieces_survive(payload, MARKER, pieces)
        self.assertTrue(ok, why)
        stored = sanitize_like_lightrag(payload)
        self.assertEqual(stored.split(MARKER), ["alpha  ", "  beta  ", "  gamma"])

    def test_an_empty_piece_set_is_not_a_match(self):
        ok, why = pieces_survive("alpha", MARKER, [])
        self.assertFalse(ok)
        self.assertIn("1 segments for 0 planned pieces", why)


if __name__ == "__main__":
    unittest.main()
