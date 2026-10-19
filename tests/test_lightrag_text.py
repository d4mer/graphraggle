"""The text LightRAG is handed is not the text we planned, unless we say so.

``sanitize_text_for_encoding`` / ``strip_control_characters`` runs on the whole document field
BEFORE any splitting, and deletes the C0 range apart from newline and tab. The structure
chunker's first delimiter (``\\x1e``) was in that range: the marker vanished, the document
arrived as one segment, and nothing in the request said so. These tests pin the offline stand-in
for that sanitiser, the plan-time normalisation that makes planned text equal stored text, and
the survivability replay the worker runs before it submits.
"""
from __future__ import annotations

import unittest

from app.lightrag_text import (is_conservative_marker, is_survivable_delimiter,
                               normalise_for_lightrag, sanitize_like_lightrag,
                               stripped_characters, pieces_survive)


class TestWhatTheSanitiserTakes(unittest.TestCase):
    def test_c0_controls_go_and_only_newline_and_tab_stay(self):
        for code in list(range(0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)) + [0x7F]:
            ch = chr(code)
            self.assertEqual(sanitize_like_lightrag(f"a{ch}b"), "ab",
                             f"U+{code:02X} goes, and a delimiter made of it arrives as nothing")
        for kept in ("\n", "\t"):
            self.assertEqual(sanitize_like_lightrag(f"a{kept}b"), f"a{kept}b")

    def test_the_recorded_strip_set_names_every_control_it_found(self):
        report = stripped_characters("alpha\x00\x01\x01\x1e\x0c beta")
        # Codepoints, not the characters: a report that carries the raw control characters
        # cannot be read in a log line, which is the whole point of the report.
        self.assertEqual(report, {"U+0000": 1, "U+0001": 2, "U+001E": 1, "U+000C": 1})

    def test_a_lone_surrogate_cannot_be_encoded_and_is_dropped(self):
        self.assertEqual(sanitize_like_lightrag("a\ud800b"), "ab")

    def test_crlf_and_a_lone_cr_both_fold_to_a_newline(self):
        self.assertEqual(normalise_for_lightrag("a\r\nb\rc"), "a\nb\nc")

    def test_zero_width_and_bom_do_not_survive_the_pipeline(self):
        # \u200b (zero width space) and \ufeff (bom / zero width no-break space) are not in the
        # C0 range but they break exact-match comparisons for no reason; the plan drops them so
        # what we count is what is stored.
        self.assertEqual(normalise_for_lightrag("a\u200bb"), "ab")
        self.assertEqual(normalise_for_lightrag("\ufeffalpha"), "alpha")

    def test_normalising_loses_characters_but_never_a_word(self):
        text = "vendor\x00 depot\t tariff\x0c customs\u200b eta\r\ndeck\r\n"
        out = normalise_for_lightrag(text)
        self.assertEqual(out, "vendor depot\t tariff customs eta\ndeck\n")
        self.assertEqual(out.split(), ["vendor", "depot", "tariff", "customs", "eta", "deck"],
                         "characters go; words never do, so the coverage contract still holds")


class TestDelimitersThatSurvive(unittest.TestCase):
    def test_the_default_marker_is_printable_and_conservative(self):
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        self.assertTrue(is_survivable_delimiter(marker))
        self.assertTrue(is_conservative_marker(marker))
        self.assertEqual(sanitize_like_lightrag(marker), marker.strip(),
                         "the marker's own body reaches LightRAG; only edge newlines are at risk")

    def test_a_control_character_marker_is_not_survivable(self):
        for marker in ("\n\n\x1e\n\n", "\x1e", "\ufeff", "\t\t", "   ", ""):
            self.assertFalse(is_survivable_delimiter(marker), marker)

    def test_parens_are_survivable_but_not_conservative(self):
        # An explicit delimiter a caller chose is judged on survivability; OUR candidates are
        # held to the narrower alphabet.
        self.assertTrue(is_survivable_delimiter("(Alice Chen)"))
        self.assertFalse(is_conservative_marker("(Alice Chen)"))


class TestSurvivalReplay(unittest.TestCase):
    def test_pieces_that_round_trip_pass(self):
        pieces = ["[Doc: a | Part 1/2]\nalpha beta", "[Doc: a | Part 2/2]\ngamma delta"]
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        payload = marker.join(pieces)
        self.assertEqual(pieces_survive(payload, marker, pieces), (True, ""))

    def test_a_delimiter_the_sanitiser_deletes_is_refused_by_name(self):
        ok, why = pieces_survive("a\x1eb", "\x1e", ["a", "b"])
        self.assertFalse(ok)
        self.assertIn("strips", why)

    def test_a_payload_whose_marker_lost_its_edges_rejects(self):
        # What a trailing delimiter becomes once the whole field is stripped: the word survives,
        # the newlines do not, so the exact delimiter is no longer in the payload.
        pieces = ["alpha", "beta"]
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        payload = marker.join(pieces) + marker
        ok, why = pieces_survive(payload, marker, pieces)
        self.assertFalse(ok)
        self.assertIn("piece 1", why)
        self.assertIn("differ", why)

    def test_a_piece_the_sanitiser_wrecks_is_named_by_index(self):
        pieces = ["alpha", "bet\x00a"]
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        ok, why = pieces_survive(marker.join(pieces), marker, pieces)
        self.assertFalse(ok)
        self.assertIn("piece 1", why)

    def test_the_replay_joins_the_way_the_render_joins(self):
        # A wrap (marker at either end) fails the replay; a join passes it. That is the render
        # contract, and it is enforced before submission rather than after.
        pieces = ["alpha", "beta"]
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        self.assertTrue(pieces_survive(marker.join(pieces), marker, pieces)[0])
        self.assertFalse(pieces_survive(marker + marker.join(pieces), marker, pieces)[0])


if __name__ == "__main__":
    unittest.main()
