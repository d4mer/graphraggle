"""Parity with the REAL ``lightrag.utils.sanitize_text_for_encoding``.

The fixture in ``tests/fixtures/`` is not synthetic: Hermes ran the real function inside the
lightrag-server2 container over 62 fixed strings and recorded the output as hex. Attempt 2 of
the ingest brief stopped at this gate with 18 of 62 differing, exactly as designed, and
nothing was ingested. These are the only ground truth for what the image does to the text we
send it, so the golden test is a straight equality over the fixture - no hand-derived
expectations anywhere in this file.

Decoding note: the inputs include lone surrogates, which ordinary UTF-8 cannot carry, so both
directions use ``surrogatepass`` (the same WTF-8 shape the hex shows: ``61 eda080 62``).
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.lightrag_text import sanitize_like_lightrag

_FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load(name: str) -> list[dict]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _decode(hex_text: str) -> str:
    return bytes.fromhex(hex_text).decode("utf-8", "surrogatepass")


def _encode(text: str) -> str:
    return text.encode("utf-8", "surrogatepass").hex()


CASES = {case["idx"]: case for case in _load("sancmp_tests.json")}
REAL = {row["idx"]: row for row in _load("sancmp_real.json")}
INDICES = sorted(CASES)


class TestGoldenFixtureIsUsable(unittest.TestCase):
    def test_sixty_two_inputs_and_sixty_two_real_outputs(self):
        self.assertEqual(len(CASES), 62)
        self.assertEqual(len(REAL), 62)
        self.assertEqual(INDICES, list(range(62)))

    def test_the_two_files_describe_the_same_input(self):
        for index in INDICES:
            self.assertEqual(REAL[index]["hex_in"], CASES[index]["hex"],
                             f"idx {index}: the fixture pair disagrees about its input")

    def test_the_real_runs_produced_no_errors(self):
        errored = {index: REAL[index]["err"] for index in INDICES if REAL[index]["err"]}
        self.assertEqual(errored, {}, "a failed real run is not an oracle")


class TestParityWithTheRealFunction(unittest.TestCase):
    def test_all_sixty_two_inputs_reproduce_the_real_output(self):
        mismatched = []
        for index in INDICES:
            got = _encode(sanitize_like_lightrag(_decode(REAL[index]["hex_in"])))
            if got != REAL[index]["hex_out"]:
                mismatched.append((index, CASES[index]["label"], REAL[index]["hex_in"],
                                   REAL[index]["hex_out"], got))
        self.assertEqual(mismatched, [],
                         f"{len(mismatched)} of 62 differ from the real function; first "
                         f"five: {mismatched[:5]}")

    def test_every_label_is_checked_by_a_subtest(self):
        # A subTest per case so a regression names the character, not just the count.
        for index in INDICES:
            with self.subTest(idx=index, label=CASES[index]["label"]):
                self.assertEqual(_encode(sanitize_like_lightrag(_decode(REAL[index]["hex_in"]))),
                                 REAL[index]["hex_out"])

    def test_the_shapes_the_imagination_got_wrong_are_the_ones_the_fixture_pins(self):
        # Not hand-derived expectations - the fixture's own labels, grouped so a reader can
        # see what the earlier imitation got wrong and why order matters.
        groups = {}
        for index in INDICES:
            kind = CASES[index]["label"].split()[0]
            groups.setdefault(kind, []).append(index)
        for kind in ("format", "CRLF", "lone", "entity"):
            self.assertIn(kind, groups, f"fixture lost its {kind} cases")
        for index in groups["format"]:
            self.assertEqual(REAL[index]["hex_out"], REAL[index]["hex_in"],
                             "format characters survive; the image keeps them")
        for index in groups["CRLF"]:
            self.assertIn("0d", REAL[index]["hex_out"], "CR is kept, never folded")

    def test_parity_survives_the_shape_our_payload_is(self):
        # The property the pipeline depends on: a payload made of ordinary printable text and
        # a printable marker comes back unchanged, so planned piece == stored piece.
        payload = ("[Doc: order book | Part 1/2]\nvendor depot \u200b tariff\r\nline\tvalue"
                   "\n\n<<<CHUNK-BREAK>>>\n\n"
                   "[Doc: order book | Part 2/2]\nsecond \xa9 piece")
        marker = "\n\n<<<CHUNK-BREAK>>>\n\n"
        self.assertEqual(sanitize_like_lightrag(payload), payload.strip())
        self.assertEqual(len(sanitize_like_lightrag(payload).split(marker)), 2)


if __name__ == "__main__":
    unittest.main()
