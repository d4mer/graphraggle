"""Offline tests for scripts/ranking_metrics.py and its wiring into overnight_loop.

Every fixture here is invented text: no corpus, no probe file, no staging, no
LightRAG data, no network. The formula expectations are hand-computed and written
out in the comments, because a metric whose expected value was produced by the
same code that computes it proves nothing.

One arithmetic convention to keep in mind: the log2 discount is taken at the
1-based position, so the first unit costs log2(2) = 1 (no discount) and the third
costs log2(4) = 2. Gain is 2^grade - 1, so grade 2 is worth 3 and grade 1 is
worth 1.
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR / "scripts"))

import ranking_metrics as rkm  # noqa: E402
import overnight_loop as ol  # noqa: E402


def _idcg(*grades: int, window: int = 5) -> float:
    """Ideal DCG: the same grades sorted best-first, discounted by position."""
    ordered = sorted(grades, reverse=True)[:window]
    return sum((2 ** grade - 1) / math.log2(position + 1)
               for position, grade in enumerate(ordered, start=1))


class TestNormalisationIsShared(unittest.TestCase):
    """One definition of "the same text", or the reports stop being comparable."""

    def test_overnight_loop_and_the_bench_import_the_same_function(self):
        self.assertIs(ol.normalise_quote, rkm.normalise_quote)
        self.assertIs(ol._RECALL_ECHO_KEYS, rkm.ECHO_KEYS)
        # normalise_quote MOVED to ranking_metrics and is re-exported, so the existing
        # `from overnight_loop import normalise_quote` keeps working - the bench, which
        # scores recall, must not end up with a second definition of a match.
        import chunk_embed_bench as ceb
        self.assertIs(ceb.normalise_quote, rkm.normalise_quote)

    def test_normalisation_collapses_whitespace_and_quote_marks(self):
        self.assertEqual(rkm.normalise_quote("  The   Depot\u2019s\tCOSTS\nrose  "),
                         "the depot\u2019s costs rose")
        self.assertEqual(rkm.normalise_quote('"Funded phase 2"'), "funded phase 2")
        self.assertEqual(rkm.normalise_quote(None), "")


class TestOrderedExtractor(unittest.TestCase):
    """Rank is only ever "position in the response walk", so pin the walk."""

    def test_strings_come_back_in_payload_order_depth_first(self):
        payload = {
            "status": "success",                    # an echo key: dropped
            "query": "what rose?",                  # an echo key: dropped
            "result": "first block",
            "data": [
                {"content": "second block", "extra": ["third block"]},
                "fourth block",
            ],
            "references": ["fifth block"],
        }
        self.assertEqual(
            rkm.context_units(payload, skip="what rose?"),
            ["first block", "second block", "third block", "fourth block",
             "fifth block"],
        )

    def test_the_binary_check_walks_the_same_units(self):
        payload = {"result": "one", "data": [{"content": "two"}, "three"]}
        self.assertEqual(ol.context_strings(payload), rkm.context_units(payload))

    def test_the_question_string_is_not_a_unit(self):
        payload = {"result": ["the depot closed", "the depot closed"]}
        self.assertEqual(rkm.context_units(payload, skip="the depot closed"), [])


class TestFirstHitAndRecall(unittest.TestCase):
    def test_single_gold_quote_at_rank_one_three_and_absent(self):
        gold = ["the depot closed"]
        units = ["noise one", "noise two", "the depot closed", "noise four"]
        self.assertEqual(rkm.first_hit_rank(units, gold), 3)
        self.assertEqual(rkm.reciprocal_rank(3), 1 / 3)
        self.assertIsNone(rkm.first_hit_rank(["nothing here"], gold))
        self.assertEqual(rkm.reciprocal_rank(None), 0.0)
        self.assertEqual(rkm.hit_at_k(rkm.relevance_by_unit(units, gold), 3), 1)
        self.assertEqual(rkm.hit_at_k(rkm.relevance_by_unit(units, gold), 2), 0)

    def test_recall_fraction_is_per_quote_and_na_when_empty(self):
        self.assertEqual(rkm.recall_fraction([True, False, True, False]), 0.5)
        self.assertEqual(rkm.recall_fraction([True, True]), 1.0)
        self.assertEqual(rkm.recall_fraction([False]), 0.0)
        self.assertIsNone(rkm.recall_fraction([]), "a probe with no gold is na, not 0")

    def test_graded_recall_weights_grade_mass_and_ignores_grade_zero(self):
        # Two quotes, grades 2 and 1: finding only the grade-1 one is 1/3 of the mass.
        self.assertEqual(rkm.graded_recall_fraction([(2, False), (1, True)]), 0.3333)
        self.assertEqual(rkm.graded_recall_fraction([(2, True), (1, True)]), 1.0)
        self.assertEqual(rkm.graded_recall_fraction([(2, True), (0, False)]), 1.0,
                         "a quote graded 0 declared itself irrelevant")
        self.assertIsNone(rkm.graded_recall_fraction([(0, False), (0, True)]))
        self.assertIsNone(rkm.graded_recall_fraction([]))


class TestAveragePrecision(unittest.TestCase):
    def test_relevant_positions_at_two_and_four(self):
        # precision@2 = 1/2, precision@4 = 2/4 -> AP = (0.5 + 0.5) / 2.
        self.assertEqual(rkm.average_precision([0, 2, 0, 2]), 0.5)

    def test_relevant_positions_at_one_and_three(self):
        # precision@1 = 1, precision@3 = 2/3 -> AP = (1 + 0.6667) / 2.
        self.assertEqual(round(rkm.average_precision([2, 0, 2]), 4), 0.8333)

    def test_nothing_relevant_scores_zero(self):
        self.assertEqual(rkm.average_precision([0, 0, 0]), 0.0)
        self.assertEqual(rkm.average_precision([]), 0.0)


class TestNdcg(unittest.TestCase):
    def test_grade_two_at_first_position_is_perfect(self):
        self.assertEqual(rkm.ndcg_at_k([2, 0, 0, 0, 0]), 1.0)

    def test_grade_two_at_third_position_is_half(self):
        # gain 3 at position 3 discounts by log2(4) = 2 -> 1.5; ideal 3/1 -> ratio 0.5.
        self.assertEqual(round(rkm.ndcg_at_k([0, 0, 2], k=5), 4), 0.5)

    def test_the_same_two_grades_in_the_other_order_score_lower(self):
        # good-first: 3/log2(2) + 1/log2(3) = 3.630930, and that is also the ideal.
        good_first = rkm.ndcg_at_k([2, 1, 0, 0, 0], k=5)
        # buried: 1/log2(2) + 3/log2(3) = 2.892789 over the same ideal 3.630930.
        buried = rkm.ndcg_at_k([1, 2, 0, 0, 0], k=5)
        self.assertEqual(round(good_first, 4), 1.0)
        self.assertEqual(round(buried, 4), 0.7967)
        self.assertLess(buried, good_first,
                        "NDCG must be able to tell found-and-ranked from found-and-buried")
        self.assertAlmostEqual(_idcg(2, 1), 3.630930, places=5)

    def test_a_probe_with_no_relevant_unit_scores_zero(self):
        self.assertEqual(rkm.ndcg_at_k([0, 0, 0], k=5), 0.0)
        self.assertEqual(rkm.ndcg_at_k([], k=5), 0.0)
        self.assertEqual(rkm.ndcg_at_k([2, 1], k=0), 0.0)


class TestEachQuoteCountsOnce(unittest.TestCase):
    def test_a_quote_in_two_units_is_credited_at_its_first_appearance(self):
        gold = ["the depot closed"]
        units = ["filler", "the depot closed early", "and the depot closed late"]
        self.assertEqual(rkm.relevance_by_unit(units, gold), [0, 2, 0])
        row = rkm.score_probe(gold, units)
        self.assertEqual((row["gold_n"], row["found_n"]), (1, 1))
        self.assertEqual(row["first_hit_rank"], 2)
        self.assertEqual(row["mrr"], 0.5)
        self.assertEqual(row["evidence_recall"], 1.0, "not 2.0 for a repeated quote")

    def test_one_unit_carrying_two_quotes_is_one_position_but_two_quotes(self):
        gold = ["freight costs rose", "the depot closed"]
        units = ["freight costs rose and the depot closed"]
        row = rkm.score_probe(gold, units)
        self.assertEqual((row["gold_n"], row["found_n"]), (2, 2))
        self.assertEqual(row["evidence_recall"], 1.0)
        self.assertTrue(row["all_hit"])
        self.assertEqual(row["mrr"], 1.0)

    def test_grades_of_two_quotes_credited_in_one_unit_use_the_best_grade(self):
        gold = ["answer quote", "support quote"]
        grades = [2, 1]
        units = ["answer quote and support quote"]
        row = rkm.score_probe(gold, units, grades)
        self.assertEqual(row["graded_recall"], 1.0)
        self.assertEqual(rkm.relevance_by_unit(units, gold, grades), [2])


class TestScoreProbeShape(unittest.TestCase):
    def test_a_probe_with_no_gold_quote_is_na_not_a_row_of_zeros(self):
        self.assertIsNone(rkm.score_probe([], ["any context"]))
        self.assertIsNone(rkm.score_probe(["  "], ["any context"]))

    def test_every_rank_number_carries_the_unverified_order_marker(self):
        row = rkm.score_probe(["the depot closed"], ["the depot closed"])
        self.assertEqual(row["rank_order"], rkm.RANK_ORDER_MARKER)
        self.assertEqual(row["rank_order"], "unverified-order")

    def test_all_hit_never_exceeds_any_hit_and_recall_is_one_exactly_when_all_hit(self):
        cases = [
        (["a fact", "b fact"], ["a fact here"]),
        (["a fact", "b fact"], ["a fact here", "b fact here"]),
        (["a fact", "b fact"], ["b fact here", "unrelated"]),
        (["a fact", "b fact"], ["nothing at all"]),
        (["a fact"], ["a fact"]),
        ]
        for gold, units in cases:
            row = rkm.score_probe(gold, units)
            self.assertLessEqual(row["any_hit"], True)
            self.assertEqual(row["all_hit"] <= row["any_hit"], True, (gold, units))
            self.assertEqual(row["evidence_recall"] == 1.0, row["all_hit"], (gold, units))
            self.assertLessEqual(row["hit@5"], 1)

    def test_a_quote_spanning_two_units_is_a_miss_here_and_a_hit_in_the_join(self):
        """The documented gap between the gate and the rank metrics.

        evidence_in_context joins every returned string before matching, so a gold
        quote that happens to straddle a unit boundary scores a hit there. Per-unit
        scoring, which is what says WHERE the evidence sits, cannot see it. Pinning
        both keeps nobody "fixing" one of them by accident.
        """
        probe = {"id": "p1", "question": "what happened?", "evidence": "depot closed"}
        payload = {"result": ["the depot", "closed at noon"]}
        self.assertEqual(ol.evidence_in_context(probe, payload), "hit")
        row = rkm.score_probe(["depot closed"], rkm.context_units(payload))
        self.assertFalse(row["any_hit"])
        self.assertEqual(row["mrr"], 0.0)

    def test_mean_metric_skips_na_and_never_invents_zero(self):
        self.assertEqual(rkm.mean_metric([None, None]), None)
        self.assertEqual(rkm.mean_metric([]), None)
        self.assertEqual(rkm.mean_metric([1.0, None, 0.0]), 0.5)
        self.assertEqual(rkm.mean_metric([True, 1.0]), 1.0, "a bool is not a measurement")


class TestProbeGradeSchema(unittest.TestCase):
    """`evidence_grades` is optional; every existing probe file keeps its old score."""

    def test_no_grades_key_means_every_quote_is_grade_two(self):
        item = {"evidence": ["the depot closed", "freight rose in march"]}
        self.assertIsNone(ol.evidence_grades_of(item))
        self.assertEqual(ol.gold_evidence_with_grades(item),
                         [("the depot closed", 2), ("freight rose in march", 2)])

    def test_evidence_as_a_bare_string_still_works(self):
        item = {"evidence": "the depot closed", "evidence_grades": [1]}
        self.assertEqual(ol.gold_evidence_with_grades(item), [("the depot closed", 1)])

    def test_grades_align_with_the_evidence_list_positionally(self):
        item = {"evidence": ["quote one here", "quote two here", "quote three here"],
                "evidence_grades": [2, 1, 0]}
        self.assertEqual(ol.gold_evidence_with_grades(item),
                         [("quote one here", 2), ("quote two here", 1), ("quote three here", 0)])

    def test_a_grade_slot_that_is_missing_or_unusable_means_two(self):
        short = {"evidence": ["quote one here", "quote two here"], "evidence_grades": [1]}
        self.assertEqual(ol.gold_evidence_with_grades(short),
                         [("quote one here", 1), ("quote two here", 2)])
        for bad in (["x", 7], [True, None], [-1, 3]):
            item = {"evidence": ["quote one here", "quote two here"], "evidence_grades": bad}
            self.assertEqual(ol.gold_evidence_with_grades(item),
                             [("quote one here", 2), ("quote two here", 2)], bad)
        not_a_list = {"evidence": "quote one here", "evidence_grades": "2"}
        self.assertIsNone(ol.evidence_grades_of(not_a_list))

    def test_a_dropped_short_quote_drops_its_grade_slot_too(self):
        item = {"evidence": ["ab", "the depot closed", "freight rose"],
                "evidence_grades": [0, 1, 2]}
        self.assertEqual(ol.gold_evidence_with_grades(item),
                         [("the depot closed", 1), ("freight rose", 2)])

    def test_turn_identifier_keys_never_take_evidence_grades(self):
        item = {"evidence_turn": ["turn 41 says it"], "evidence_grades": [1]}
        self.assertEqual(ol.gold_evidence_with_grades(item), [("turn 41 says it", 2)])

    def test_grades_never_change_the_binary_gate(self):
        graded = {"id": "p1", "question": "q", "evidence": ["the depot closed"],
                  "evidence_grades": [1]}
        ungraded = {"id": "p1", "question": "q", "evidence": ["the depot closed"]}
        payload = {"result": ["context: the depot closed at noon"]}
        self.assertEqual(ol.evidence_in_context(graded, payload),
                         ol.evidence_in_context(ungraded, payload))
        self.assertEqual(
            [quote for quote, _ in ol.gold_evidence_with_grades(graded)],
            ol.gold_evidence_of(graded))

    def test_a_grade_zero_quote_still_counts_as_evidence_for_the_gate(self):
        """grade 0 means "not relevant for the RANKING", not "not evidence"."""
        item = {"id": "p1", "question": "q", "evidence": ["the depot closed"],
                "evidence_grades": [0]}
        self.assertEqual(ol.evidence_in_context(item, {"result": "the depot closed"}), "hit")
        pairs = ol.gold_evidence_with_grades(item)
        row = rkm.score_probe([quote for quote, _grade in pairs], ["the depot closed"],
                              [grade for _quote, grade in pairs])
        self.assertTrue(row["any_hit"])
        self.assertIsNone(row["graded_recall"], "no grade mass, so na rather than 0.0")


class TestScoreSetColumns(unittest.TestCase):
    """The per-arm numbers: over gold-evidence rows only, na never becomes a zero."""

    ITEMS = [{"id": "p1", "question": "q", "expected_answer": "a"},
             {"id": "p2", "question": "q", "expected_answer": "b"}]
    VERDICTS = ["correct", "correct"]

    def test_columns_average_over_the_measured_rows(self):
        extras = [
            {"recall": "hit", "recall_metrics": {
                "evidence_recall": 1.0, "graded_recall": 1.0, "mrr": 1.0,
                "average_precision": 1.0, "hit@1": 1, "hit@5": 1, "ndcg@k": 1.0,
                "any_hit": True, "all_hit": True}},
            {"recall": "hit", "recall_metrics": {
                "evidence_recall": 0.5, "graded_recall": 0.5, "mrr": 0.5,
                "average_precision": 0.5, "hit@1": 0, "hit@5": 1, "ndcg@k": 0.25,
                "any_hit": True, "all_hit": False}},
        ]
        summary = ol.score_set(self.ITEMS, self.VERDICTS, [1.0, 1.0], extras)
        self.assertEqual(summary["ranking_measured_n"], 2)
        self.assertEqual(summary["evidence_recall"], 0.75)
        self.assertEqual(summary["mrr"], 0.75)
        self.assertEqual(summary["ndcg@5"], 0.625)
        self.assertEqual(summary["hit@1"], 0.5)
        self.assertEqual(summary["hit@5"], 1.0)
        self.assertEqual(summary["any_hit_pct"], 100.0)
        self.assertEqual(summary["all_hit_pct"], 50.0)
        self.assertEqual(summary["rank_order"], "unverified-order")

    def test_a_summary_without_the_new_measurements_reports_na_not_zero(self):
        old_style = [{"recall": "hit"}, {"recall": "miss"}]
        summary = ol.score_set(self.ITEMS, self.VERDICTS, [1.0, 1.0], old_style)
        self.assertEqual(summary["evidence_n"], 2, "the binary gate is untouched")
        self.assertEqual(summary["evidence_in_context_pct"], 50.0)
        self.assertEqual(summary["ranking_measured_n"], 0)
        for key in ("evidence_recall", "graded_recall", "mrr", "average_precision",
                    "hit@1", "hit@5", "ndcg@5", "any_hit_pct", "all_hit_pct"):
            self.assertIsNone(summary[key], f"{key} would read as a measured failure")
        self.assertIsNone(summary["rank_order"])

    def test_no_extras_at_all_still_scores(self):
        summary = ol.score_set(self.ITEMS, self.VERDICTS, [1.0, 1.0])
        self.assertEqual(summary["evidence_in_context_pct"], 0.0)
        self.assertIsNone(summary["mrr"])

    def test_all_hit_pct_never_exceeds_any_hit_pct(self):
        extras = [
            {"recall_metrics": {"any_hit": True, "all_hit": True, "evidence_recall": 1.0,
                                "mrr": 1.0, "hit@1": 1, "hit@5": 1, "ndcg@k": 1.0}},
            {"recall_metrics": {"any_hit": True, "all_hit": False, "evidence_recall": 0.5,
                                "mrr": 0.5, "hit@1": 0, "hit@5": 0, "ndcg@k": 0.1}},
            {"recall_metrics": {"any_hit": False, "all_hit": False, "evidence_recall": 0.0,
                                "mrr": 0.0, "hit@1": 0, "hit@5": 0, "ndcg@k": 0.0}},
        ]
        items = self.ITEMS[:1] * 3
        summary = ol.score_set(items, self.VERDICTS[:1] * 3, [], extras)
        self.assertLessEqual(summary["all_hit_pct"], summary["any_hit_pct"])
        self.assertEqual(summary["any_hit_pct"], round(200 / 3, 2))


class TestPairedBootstrapOnNewMetrics(unittest.TestCase):
    """Two arms get an interval on the graded numbers, deterministically."""

    def test_fixed_seed_gives_the_same_interval_twice(self):
        a = {"p1": 1.0, "p2": 0.5, "p3": 1.0, "p4": 0.0}
        b = {"p1": 0.5, "p2": 0.5, "p3": 0.0, "p4": 0.0}
        first = ol.paired_bootstrap_difference(a, b, seed=7)
        second = ol.paired_bootstrap_difference(a, b, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(first["n"], 4)
        self.assertEqual(first["difference"], 0.375)

    def test_probes_measured_for_ranking_may_be_fewer_than_probes_scored(self):
        scores = {"p1": 1.0, "p2": 0.0, "p3": 1.0}
        mrr = {"p1": 1.0, "p2": 0.0}          # p3 carried no gold quote
        hits = ol.paired_bootstrap_difference(scores, scores)
        ranks = ol.paired_bootstrap_difference(mrr, mrr)
        self.assertLess(ranks["n"], hits["n"])
        self.assertEqual(ranks["difference"], 0.0)

    def test_no_shared_probes_reports_zero_paired(self):
        stats = ol.paired_bootstrap_difference({"p1": 1.0}, {"p2": 1.0})
        self.assertEqual(stats["n"], 0)
        self.assertFalse(stats["excludes_zero"])
        # Reported to the lead, NOT fixed here (out of this brief): in exactly this
        # no-overlap case the function returns only_in_a/only_in_b as 0, so the
        # unpaired counts disappear when they matter most.
        self.assertEqual((stats["only_in_a"], stats["only_in_b"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
