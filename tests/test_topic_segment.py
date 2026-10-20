"""Topic-shift segmentation: TextTiling over turn windows, and the fallback that is honest.

Everything here is synthetic. The embedder is a scripted bag-of-words stand-in: it is
deterministic, it has the same shape as the served BGE-M3 call (texts in, vectors out),
and no test in this suite opens a socket.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from app.topic_segment import (          # noqa: E402
    TopicReport,
    group_units,
    segment_bodies,
    _depth_scores,
)
from app.structure_chunker import chunk_document, coverage_gaps, token_count   # noqa: E402


class VocabularyEmbedder:
    """A deterministic stand-in for the served embedder: one dimension per topic word.

    Topics are disjoint vocabularies, so adjacent blocks of the same topic score ~1.0 and
    adjacent blocks across a topic change score ~0.0 - which is exactly the signal
    TextTiling reads. Real BGE-M3 behaviour on real transcripts is NOT measured here.
    """

    def __init__(self, topics: list[list[str]]) -> None:
        self.topics = topics
        self.calls: list[int] = []

    def __call__(self, texts):
        self.calls.append(len(texts))
        vectors = []
        for text in texts:
            words = set(text.lower().split())
            vectors.append([1.0 if words & set(topic) else 0.0 for topic in self.topics])
        return vectors


def turns_for_topic(topic_words: list[str], count: int, *, label: str) -> list[str]:
    """A turn long enough to model a real meeting: three sentences, one per claim."""
    lines = []
    for index in range(count):
        word = topic_words[index % len(topic_words)]
        other = topic_words[(index + 1) % len(topic_words)]
        third = topic_words[(index + 2) % len(topic_words)]
        lines.append(
            f"({label}) The {word} figure sits against {other} for period {index}, and "
            f"the {word} margin moved twice in the same review. The {third} desk flagged "
            f"the same {word} number at the quarterly session, with the {other} note "
            f"attached to the ledger entry for period {index}. Nothing changed on the "
            f"{word} side since the last reading, and the {other} position is unchanged.")
    return lines


FIVE_TOPICS = [
    ["freight", "carrier", "lane"],
    ["tariff", "duty", "customs"],
    ["wage", "shift", "roster"],
    ["depot", "intake", "ramp"],
    ["audit", "ledger", "voucher"],
]


class TopicTiling(unittest.TestCase):
    def units(self) -> list[str]:
        units: list[str] = []
        for index, topic in enumerate(FIVE_TOPICS):
            units.extend(turns_for_topic(topic, 16, label=f"Speaker {index % 3}"))
        return units

    def test_a_flat_transcript_cuts_at_the_topic_changes(self):
        units = self.units()
        embedder = VocabularyEmbedder(FIVE_TOPICS)
        groups, report = group_units(units, tokens=token_count, window=7500, floor=150,
                                     embed=embedder, units_per_block=4)
        self.assertEqual(report.method, "embedder")
        self.assertEqual(len(groups), 5, f"five topics, five pieces: {len(groups)}")
        self.assertTrue(embedder.calls, "the segmentation asked the embedder something")
        # This fixture gives every topic the same length, so the check here is that each
        # boundary sits exactly on a topic edge; unequal lengths are the next test.
        edges = [0]
        for group in groups:
            edges.append(edges[-1] + len(group))
        self.assertEqual(edges[1:-1], [16, 32, 48, 64],
                         "every cut sits exactly on a topic change")
        self.assertEqual(sum(len(group) for group in groups), len(units),
                         "no turn is lost on the way out")

    def test_only_a_valley_is_a_boundary_candidate(self):
        """TextTiling depth, and the rule that keeps one cut per topic change.

        Scoring every gap would cut the same change three times - once on the way down
        into the dip, once at the bottom, once on the way back up - and a document would
        be chopped into shoulder-sized pieces. Only a gap lower than both its neighbours
        is a candidate.
        """
        sims = [0.9, 0.85, 0.8, 0.2, 0.75, 0.9]
        depths = _depth_scores(sims)
        self.assertEqual([index for index, depth in enumerate(depths) if depth > 0.0], [3],
                         "the dip is at gap 3; its shoulders are not separate changes")

    def test_a_gradual_topic_change_is_cut_once_not_at_every_shoulder(self):
        a, m1, m2, b = ("freight carrier lane", "freight tariff duty", "freight tariff",
                        "tariff duty customs")
        units = [a, a, m1, m2, b, b]
        embedder = VocabularyEmbedder([["freight"], ["carrier"], ["lane"], ["tariff"],
                                       ["duty"], ["customs"]])
        groups, report = group_units(units, tokens=token_count, window=7500, floor=0,
                                     embed=embedder, units_per_block=1, threshold=0.25)
        self.assertEqual(len(groups), 3,
                         f"the topic changes once, through a drift: {groups}")
        self.assertEqual(report.boundaries, 2, "A | the drift | B")

    def test_the_purpose_of_the_module_is_unequal_pieces(self):
        units: list[str] = []
        for index, topic in enumerate(FIVE_TOPICS):
            units.extend(turns_for_topic(topic, 6 + 7 * index, label="Alice"))
        groups, report = group_units(units, tokens=token_count, window=7500, floor=150,
                                     embed=VocabularyEmbedder(FIVE_TOPICS),
                                     units_per_block=3)
        self.assertEqual(len(groups), 5)
        sizes = sorted(sum(token_count(unit) for unit in group) for group in groups)
        self.assertNotEqual(sizes[0], sizes[-1],
                            "a token window would have made them equal; a topic does not")
        self.assertGreaterEqual(sizes[-1], 2 * sizes[0])

    def test_without_an_embedder_the_cut_is_deterministic_and_says_so(self):
        units = self.units()
        first, report = group_units(units, tokens=token_count, window=2000, floor=150,
                                    embed=None)
        second, _again = group_units(units, tokens=token_count, window=2000, floor=150,
                                     embed=None)
        self.assertEqual(report.method, "fallback(no-embedder)")
        self.assertEqual(first, second, "the fallback is reproducible, run to run")
        for group in first:
            self.assertLessEqual(sum(token_count(unit) for unit in group), 2000,
                                 "the fallback still respects the window")
        self.assertEqual(sum(len(group) for group in first), len(units))

    def test_an_unreachable_embedder_changes_how_not_whether(self):
        def broken(texts):
            raise ConnectionError("model server is down")
        units = self.units()
        groups, report = group_units(units, tokens=token_count, window=2000, floor=150,
                                     embed=broken)
        self.assertEqual(report.method, "fallback(embedder-unreachable)")
        self.assertTrue(any("ConnectionError" in note for note in report.notes))
        self.assertEqual(sum(len(group) for group in groups), len(units))

    def test_a_vector_answer_of_the_wrong_shape_falls_back(self):
        units = self.units()
        groups, report = group_units(units, tokens=token_count, window=1500, floor=150,
                                     embed=lambda texts: [[0.0, 0.0] if index else []
                                                          for index in range(len(texts))])
        self.assertEqual(report.method, "fallback(bad-embedder-answer)")
        self.assertEqual(sum(len(group) for group in groups), len(units))

    def test_a_topic_bigger_than_the_window_is_split_at_turn_boundaries(self):
        units = turns_for_topic(FIVE_TOPICS[0], 200, label="Alice")
        groups, report = group_units(units, tokens=token_count, window=900, floor=150,
                                     embed=VocabularyEmbedder(FIVE_TOPICS),
                                     units_per_block=4)
        for group in groups:
            self.assertLessEqual(sum(token_count(unit) for unit in group), 900)
        self.assertEqual(sum(len(group) for group in groups), len(units))

    def test_a_tiny_topic_is_welded_back_rather_than_left_alone(self):
        units = (turns_for_topic(FIVE_TOPICS[0], 1, label="Alice")
                 + turns_for_topic(FIVE_TOPICS[1], 24, label="Bob")
                 + turns_for_topic(FIVE_TOPICS[2], 24, label="Carol"))
        groups, report = group_units(units, tokens=token_count, window=7500, floor=400,
                                     embed=VocabularyEmbedder(FIVE_TOPICS),
                                     units_per_block=4)
        for group in groups:
            self.assertGreaterEqual(sum(token_count(unit) for unit in group), 400,
                                    "a one-turn piece is what the floor exists to prevent")
        self.assertGreaterEqual(report.merges_under_floor, 1)

    def test_the_work_stays_bounded_on_a_long_document(self):
        units = self.units() * 12
        embedder = VocabularyEmbedder(FIVE_TOPICS)
        groups, report = group_units(units, tokens=token_count, window=7500, floor=150,
                                     embed=embedder, units_per_block=4, max_windows=40)
        self.assertTrue(report.coarsened, "a long document coarsens its blocks")
        self.assertLessEqual(report.blocks, 40)
        self.assertLessEqual(max(embedder.calls), 40, "one batched call, bounded windows")
        self.assertEqual(sum(len(group) for group in groups), len(units),
                         "bounding the windows never drops a unit")

    def test_the_report_is_counts_and_names_only(self):
        import json
        _groups, report = segment_bodies(turns_for_topic(FIVE_TOPICS[0], 8, label="Alice"),
                                         tokens=token_count, window=7500, floor=150,
                                         embed=None)
        blob = json.dumps(report.as_dict())
        self.assertNotIn("freight", blob, "the metadata never carries document text")
        self.assertIn("method", blob)


class ChunkerIntegration(unittest.TestCase):
    """The amendment's test: a 40k-token flat transcript of five topics, five pieces."""

    def transcript(self) -> str:
        lines: list[str] = []
        for index, topic in enumerate(FIVE_TOPICS):
            for turn in turns_for_topic(topic, 60, label=f"Speaker {index % 4}"):
                lines.append(turn)
        return "\n".join(lines) + "\n"

    def test_the_topic_cuts_reach_the_plan(self):
        text = self.transcript()
        self.assertGreater(token_count(text), 20000, "fixture must model a long meeting")
        plan = chunk_document(text, filename="Meeting.txt", max_piece_tokens=7500,
                              min_piece_tokens=150, kind="transcript",
                              topic_embed=VocabularyEmbedder(FIVE_TOPICS),
                              topic_options={"units_per_block": 4})
        self.assertEqual(plan.kind_detected, "transcript")
        self.assertEqual(len(plan.pieces), 5, f"five topics, five pieces: {len(plan.pieces)}")
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 7500)
        self.assertEqual(plan.stats["topic_method"], "embedder")
        self.assertGreaterEqual(plan.stats["topic_boundaries"], 4)
        self.assertEqual(coverage_gaps(text, plan)["unaccounted_word_tokens"], 0)

    def test_the_fallback_is_recorded_when_no_embedder_is_configured(self):
        text = self.transcript()
        plan = chunk_document(text, filename="Meeting.txt", max_piece_tokens=3000,
                              min_piece_tokens=150, kind="transcript")
        self.assertNotIn("topic_method", plan.stats,
                         "no topic pass ran, and the provenance does not claim one")
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 3000)
        self.assertEqual(coverage_gaps(text, plan)["unaccounted_word_tokens"], 0)

    def test_a_flat_document_without_headings_uses_the_same_segmentation(self):
        paragraphs = []
        for topic in FIVE_TOPICS:
            for index in range(4):
                paragraphs.append(
                    " ".join(f"The {word} position for quarter {index} is reported "
                             f"against {topic[(i + 1) % 3]} with the depot note and the "
                             f"ledger entry attached." for i, word in enumerate(topic * 3)))
        text = "\n\n".join(paragraphs) + "\n"
        plan = chunk_document(text, filename="Notes.txt", max_piece_tokens=1200,
                              min_piece_tokens=150,
                              topic_embed=VocabularyEmbedder(FIVE_TOPICS),
                              topic_options={"units_per_block": 1})
        self.assertEqual(len(plan.pieces), 5, "one piece per topic, five paragraphs each")
        self.assertEqual(plan.stats["topic_method"], "embedder")

    def test_headed_documents_are_left_alone(self):
        text = ("# Freight\n\nthe carrier lane report follows\n\n"
                "## Tariffs\n\nthe duty position is in customs\n")
        plan = chunk_document(text, filename="Mixed.txt", max_piece_tokens=7500,
                              min_piece_tokens=150,
                              topic_embed=VocabularyEmbedder(FIVE_TOPICS),
                              topic_options={"units_per_block": 1})
        self.assertNotEqual(plan.stats.get("topic_method"), "embedder",
                            "a document with headings has real boundaries already")
        self.assertIn("Section:", plan.pieces[0].header)

    def test_the_report_shape_survives_json(self):
        import json
        self.assertEqual(set(json.loads(json.dumps(TopicReport(method="x").as_dict()))),
                         {"method", "blocks", "units_per_block", "boundaries", "embedded",
                          "coarsened", "splits_over_window", "merges_under_floor", "notes"})


if __name__ == "__main__":
    unittest.main()
