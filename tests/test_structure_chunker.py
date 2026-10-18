"""The structure-aware pre-chunker: rules, invariants, and its three call sites.

Covers ``app/structure_chunker.py`` (pure), the worker wiring behind
``STRUCTURE_CHUNKING_ENABLED`` (default off), the experiment-level
``structure_chunking`` key in ``scripts/overnight_loop.py``, and the
``"strategy": "structure"`` arm of ``scripts/chunk_embed_bench.py`` - all the same
function, so a benchmark or loop result describes the shipped path.

All fixtures are synthetic; this suite never reads corpus, probe or LightRAG data.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from collections import Counter
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "scripts"))

from app.structure_chunker import (              # noqa: E402
    DELIMITER_CANDIDATES,
    StructureChunkerError,
    chunk_document,
    pick_delimiter,
    render_for_lightrag,
    tokenizer_name,
    token_count,
    _content_words,
)

CONFIGS = _ROOT / "docs" / "ops" / "loop" / "chunk-embed-bench-configs.json"


# ── synthetic fixtures, one per rule ───────────────────────────────────────

TRANSCRIPT = (
    "(Alice Chen) We should ship the reranker this week, the terms are already cleared.\n"
    "(Bob, Acme Legal) I still have concerns about the vendor terms and the cost.\n"
    "(Alice Chen) Legal cleared the terms in July, the memo is in the shared drive.\n"
    "(Bob, Acme Legal) Then we should record the decision before the board meets.\n"
) * 3

HUGE_TURN = TRANSCRIPT + "(Alice Chen) " + ("recorded decision "*90) + "\n"

MARKDOWN_TABLE = ("Table 4 Costs by part\n"
                  "| Part | Cost | Owner |\n|---|---|---|\n"
                  + "\n".join(f"| part {index} | {index * 7} | owner {index} |"
                              for index in range(1, 11)))

TAB_TABLE = ("name\tq1\tq2\tq3\n"
             + "\n".join(f"region {index}\t{index * 3}\t{index * 7}\t{index * 11}"
                         for index in range(1, 12)))

COLUMNAR = ("ColumnA      ColumnB      ColumnC\n"
            + "\n".join(f"value {index}     middle {index}     tail {index}"
                        for index in range(1, 14)))

HEADINGS = """# Procurement policy

## 1. Scope
This policy covers every purchase above the agreed threshold.

### 1.1 Exceptions
Gifts and emergency repairs are handled separately by the owner.

- ask the owner before ordering
- record the amount in the ledger
- keep the receipt attached to the claim

## 2. Approval
Two signatures are required above the cap, one of them the budget holder.
"""

NESTED_LISTS = ("Lead-in line that introduces what the list below means.\n\n"
                "- first item with a few extra words so it is not trivial\n"
                "  - nested one.a under the first item\n"
                "  - nested one.b under the first item\n"
                "- second item with its own tail of words\n\n"
                "- third item after the blank line\n")

FURNITURE = "\n".join(
    ["Acme Corp confidential"] * 5
    # a running footer that is identical on every page is the recognisable shape;
    # "Page 1", "Page 2", ... are not repeats and must never be dropped
    + ["Page 3"] * 5
    + ["Section Alpha", "Alpha alpha alpha alpha alpha.", "Section Beta",
       "Beta beta beta beta beta beta."] * 3
)

TIMESTAMPISH = """1.2.3 Numbers

0:12 12 2026-01-02

Reported totals were 12 and 20 for the quarter.
"""

CRLF_UNICODE = ("Header line\r\n\r\nCafé naïve — “quoted” and é and ü.\r\n"
                + "Deux mots encore. " * 12 + "\r\nFin.\r\n")

PLAIN = ("It was a Tuesday when the decision was finally taken, after weeks of argument "
         "about cost and about the vendor's terms. ") * 6


# ── the rules, one test each ───────────────────────────────────────────────

class TestRules(unittest.TestCase):
    def test_transcript_is_cut_at_turn_boundaries_and_detected(self):
        plan = chunk_document(TRANSCRIPT, filename="Standup_2026-10-09.txt",
                              budget_tokens=80, header_tokens=25)
        self.assertEqual(plan.kind_detected, "transcript")
        self.assertGreater(len(plan.pieces), 1)
        for piece in plan.pieces:
            self.assertEqual(piece.kind, "turn")
            lines = [line for line in piece.body.splitlines() if line.strip()]
            self.assertTrue(lines, "empty piece")
            self.assertTrue(lines[0].startswith("("), "a piece must not start mid-turn")
            self.assertIn("[Meeting: Standup | Date: 2026-10-09", piece.header)
        self.assertEqual({piece.part_total for piece in plan.pieces}, {len(plan.pieces)})

    def test_one_huge_turn_is_split_without_losing_a_word(self):
        plan = chunk_document(HUGE_TURN, filename="Standup_2026-10-09.txt",
                              budget_tokens=120, header_tokens=20)
        self.assertEqual(plan.kind_detected, "transcript")
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 120)
        self.assertEqual(plan.stats.get("oversized_turns_split"), 1)
        # the giant turn's words are all still there
        bodies = " ".join(piece.body for piece in plan.pieces)
        self.assertEqual(bodies.count("recorded decision"), 90)

    def test_markdown_table_splits_by_row_with_the_header_row_repeated(self):
        plan = chunk_document(MARKDOWN_TABLE, filename="Costs_2026-04-11.md",
                              budget_tokens=90, header_tokens=20)
        self.assertEqual(plan.kind_detected, "table")
        self.assertGreater(len(plan.pieces), 1)
        for index, piece in enumerate(plan.pieces):
            lines = piece.body.splitlines()
            if index:
                self.assertEqual(lines[0], "| Part | Cost | Owner |",
                                 "every piece of a split table carries the header row")
                self.assertEqual(lines[1], "|---|---|---|", "and its separator rule")
        self.assertGreaterEqual(plan.stats.get("repeated_table_header_rows", 0), 2)
        # the caption stays with the FIRST piece only
        self.assertIn("Table 4 Costs by part", plan.pieces[0].body)
        self.assertNotIn("Table 4 Costs by part",
                         " ".join(piece.body for piece in plan.pieces[1:]))

    def test_tab_separated_table_is_recognised_as_a_table(self):
        plan = chunk_document(TAB_TABLE, filename="Regions_2026.tsv", budget_tokens=70,
                              header_tokens=16)
        self.assertEqual(plan.kind_detected, "table")
        self.assertEqual(plan.stats.get("tables"), 1)
        for piece in plan.pieces[1:]:
            self.assertEqual(piece.body.splitlines()[0], "name\tq1\tq2\tq3")

    def test_columnar_two_space_runs_are_a_table(self):
        plan = chunk_document(COLUMNAR, filename="Wide.txt", budget_tokens=70,
                              header_tokens=16)
        self.assertEqual(plan.kind_detected, "table")
        self.assertTrue(all(piece.kind == "table" for piece in plan.pieces))

    def test_a_row_wider_than_the_budget_splits_at_cell_boundaries_repeating_the_key(self):
        wide = ("| Item | Notes |\n|---|---|\n"
                "| key-cell | " + " | ".join(f"note {index}" for index in range(40)) + " |\n")
        plan = chunk_document(wide, filename="Wide_2026.md", budget_tokens=60,
                              header_tokens=12)
        bodies = [piece.body for piece in plan.pieces]
        self.assertGreater(len(bodies), 1)
        for body in bodies:
            self.assertIn("key-cell", body, "the row key is repeated on every piece")
        self.assertGreaterEqual(plan.stats.get("repeated_table_row_keys", 0), 1)
        self.assertNotIn("cell-over-budget", plan.warnings,
                         "cell splitting avoided splitting inside a cell")

    def test_a_single_huge_cell_is_split_by_words_and_says_so(self):
        blob = ("| Item | Notes |\n|---|---|\n"
                "| key-cell | " + " ".join(f"word{index}" for index in range(60)) + " |\n")
        plan = chunk_document(blob, filename="Blob_2026.md", budget_tokens=60,
                              header_tokens=12)
        self.assertGreater(len(plan.pieces), 1)
        self.assertEqual(plan.stats.get("table_cells_split"), 1)
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 60)

    def test_headings_start_pieces_and_feed_the_section_path(self):
        plan = chunk_document(HEADINGS, filename="Procurement_2026-03-15.md",
                              budget_tokens=70, header_tokens=30)
        self.assertEqual(plan.kind_detected, "structured")
        paths = [piece.section_path for piece in plan.pieces]
        self.assertIn(("Procurement policy", "Scope"), paths)
        self.assertIn(("Procurement policy", "Scope", "Exceptions"), paths)
        self.assertIn(("Procurement policy", "Approval"), paths)
        headed = [piece for piece in plan.pieces if piece.kind == "section"]
        self.assertTrue(headed)
        self.assertTrue(any("Section: Procurement policy / Scope" in (piece.header or "")
                            for piece in headed))

    def test_a_list_travels_with_its_lead_in_when_it_fits(self):
        plan = chunk_document(NESTED_LISTS, filename="Lists.txt", budget_tokens=200,
                              header_tokens=20)
        self.assertEqual(len(plan.pieces), 1)
        self.assertIn("Lead-in line", plan.pieces[0].body)
        self.assertIn("nested one.a", plan.pieces[0].body)
        tight = chunk_document(NESTED_LISTS, filename="Lists.txt", budget_tokens=45,
                               header_tokens=10)
        self.assertGreater(len(tight.pieces), 1)
        for piece in tight.pieces:
            self.assertLessEqual(piece.token_estimate, 45)

    def test_running_headers_and_page_markers_are_dropped_and_counted(self):
        plan = chunk_document(FURNITURE, filename="Deck.txt", budget_tokens=90,
                              header_tokens=20)
        self.assertEqual(plan.stats.get("dropped_running_lines"), 16)
        self.assertEqual(plan.stats.get("dropped_page_markers"), 5)
        joined = "\n".join(piece.body for piece in plan.pieces)
        self.assertNotIn("Acme Corp confidential", joined)
        self.assertNotIn("Page 3", joined)
        # ... and the content that only LOOKED repeatable is intact
        self.assertEqual(joined.count("Alpha alpha alpha alpha alpha."), 3)
        self.assertEqual(joined.count("Beta beta beta beta beta beta."), 3)

    def test_repeated_content_lines_are_never_dropped(self):
        repeated = "(Alice Chen) Got it.\n" * 6 + "(Bob, Acme Legal) Understood here too.\n" * 6
        plan = chunk_document(repeated, filename="Standup.txt", budget_tokens=120,
                              header_tokens=20)
        joined = "\n".join(piece.body for piece in plan.pieces)
        self.assertEqual(joined.count("Got it."), 6)
        self.assertEqual(joined.count("Understood here too."), 6)
        self.assertEqual(plan.stats.get("dropped_running_lines", 0), 0)

    def test_numbers_that_look_like_timestamps_are_not_headings_or_furniture(self):
        plan = chunk_document(TIMESTAMPISH, filename="Numbers_2026-01-02.txt",
                              budget_tokens=120, header_tokens=20)
        joined = "\n".join(piece.body for piece in plan.pieces)
        self.assertIn("0:12 12 2026-01-02", joined)
        self.assertIn("Reported totals were 12 and 20", joined)
        self.assertEqual(plan.stats.get("dropped_running_lines", 0), 0)

    def test_crlf_and_unicode_survive_intact(self):
        plan = chunk_document(CRLF_UNICODE, filename="Notes_2026-02-02.txt",
                              budget_tokens=60, header_tokens=20)
        joined = "\n".join(piece.body for piece in plan.pieces)
        self.assertIn("Café naïve — “quoted” and é and ü.", joined)
        self.assertNotIn("\r", joined)
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 60)

    def test_plain_text_falls_back_to_paragraphs_sentences_words(self):
        plan = chunk_document(PLAIN, filename="notes.txt", budget_tokens=50,
                              header_tokens=15)
        self.assertEqual(plan.kind_detected, "plain")
        self.assertGreater(len(plan.pieces), 1)
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 50)
        self.assertEqual(" ".join(piece.body for piece in plan.pieces).count(
            "decision was finally taken"), 6)

    def test_coverage_accounts_for_every_word_or_reports_the_gap(self):
        from app.structure_chunker import ChunkPlan, Piece, coverage_gaps
        source = "alpha beta gamma delta epsilon zeta\n"
        full = chunk_document(source, filename="words.txt", budget_tokens=12, header_tokens=4)
        whole = coverage_gaps(source, full)
        self.assertEqual(whole["unaccounted_word_tokens"], 0)
        self.assertEqual(whole["covered_pct"], 100.0)
        # A plan that lost words must say how many, in words, and not name them.
        leaky = ChunkPlan(pieces=[Piece(body="alpha beta", header="", kind="plain",
                                        section_path=())], kind_detected="plain")
        gaps = coverage_gaps(source, leaky)
        self.assertEqual(gaps["missing_word_tokens"], 4)
        self.assertEqual(gaps["unaccounted_word_tokens"], 4)
        self.assertLess(gaps["covered_pct"], 100.0)
        blob = json.dumps(gaps)
        for leaked in ("gamma", "delta", "epsilon", "zeta"):
            self.assertNotIn(leaked, blob, "the coverage report carries counts, never text")

    def test_no_input_shape_can_stall_the_scan(self):
        """A line none of the branches claims must still be consumed, not looped on.

        A hang here is an overnight ingest that never finishes, which is worse than a
        wrong chunk: the driver's only defence against an ingest that never returns is
        its wall-clock ceiling, and it would spend the whole night there. (Found by
        sabotage: disable the heading branch and the scan never advances on "# Heading".)
        """
        odd = ["# Heading", "* item", "  ", "\t", "|", "|---|", "1.2.3.4.5.6.7.8 x",
               "---", "   \t  x", "\f", "-"]
        text = "\n".join(odd * 3)
        plan = chunk_document(text, filename="odd.txt", budget_tokens=40, header_tokens=12)
        self.assertTrue(plan.pieces)
        joined = "\n".join(piece.body for piece in plan.pieces)
        # Structure survives repetition: a heading or a list item that appears three
        # times is the document repeating itself, not a page header.
        self.assertIn("# Heading", joined)
        self.assertIn("* item", joined)
        # The only thing allowed to disappear is the furniture-shaped line, and it is
        # counted rather than silently removed.
        kept = [word for line in text.splitlines() if line.strip()
                for word in _content_words(line)]
        out = [word for piece in plan.pieces for word in _content_words(piece.body)]
        self.assertEqual(Counter(kept) - Counter(out),
                         Counter({"1.2.3.4.5.6.7.8": 3, "x": 3}),
                         "only the furniture-shaped line may go")
        self.assertEqual(plan.stats.get("dropped_running_lines"), 3)

    def test_a_forced_kind_that_does_not_fit_says_so_and_still_returns_a_plan(self):
        plan = chunk_document(PLAIN, filename="notes.txt", kind="transcript",
                              budget_tokens=50)
        self.assertIn("transcript-not-detected", plan.warnings)
        self.assertEqual(plan.kind_detected, "plain")
        self.assertTrue(plan.pieces)

    def test_kind_can_be_forced_off_for_speed(self):
        plan = chunk_document(TRANSCRIPT, filename="Standup.txt", kind="plain",
                              budget_tokens=100)
        self.assertNotEqual(plan.kind_detected, "transcript")

    def test_empty_input_returns_an_empty_plan_rather_than_raising(self):
        plan = chunk_document("   \n\n", filename="blank.txt")
        self.assertTrue(plan.empty)
        self.assertIn("empty-input", plan.warnings)

    def test_an_unsplittable_word_is_reported_not_silently_truncated(self):
        monster = "short. " + ("x" * 400) + " tail.\n" * 2
        plan = chunk_document(monster, filename="blob.txt", budget_tokens=60,
                              header_tokens=10)
        oversized = [piece for piece in plan.pieces if piece.token_estimate > 60]
        self.assertTrue(oversized, "the unsplittable word must survive, oversize")
        for piece in oversized:
            self.assertEqual(len(piece.body.split()), 1,
                             "only a single unsplittable word may exceed the budget")


# ── the invariants, across the whole fixture set ──────────────────────────

FIXTURES = {
    "transcript": TRANSCRIPT, "huge turn": HUGE_TURN, "markdown table": MARKDOWN_TABLE,
    "tab table": TAB_TABLE, "columnar": COLUMNAR, "headings": HEADINGS,
    "nested lists": NESTED_LISTS, "furniture": FURNITURE, "timestamps": TIMESTAMPISH,
    "crlf unicode": CRLF_UNICODE, "plain": PLAIN,
}


class TestInvariants(unittest.TestCase):
    """The contract, checked over every fixture at three budgets.

    Bodies, minus inserted headers and the repeats the rules force, account for every
    content word of the input; nothing is over budget unless it is one unsplittable
    word; no piece is empty; the delimiter appears nowhere inside a body; and the
    rendered text splits back into exactly the planned pieces.
    """

    def check(self, name, text, budget, headers=True):
        plan = chunk_document(text, filename="Vendor_Review_2026-04-11.txt",
                              budget_tokens=budget, header_tokens=25, headers=headers)
        self.assertTrue(plan.pieces, f"{name} b{budget}: no pieces")
        kept = [word for line in text.splitlines() if line.strip()
                for word in _content_words(line)]
        dropped = plan.stats.get("dropped_running_words", 0)
        repeats = plan.stats.get("repeated_words", 0)
        out = [word for piece in plan.pieces for word in _content_words(piece.body)]
        missing = Counter(kept) - Counter(out)
        self.assertEqual(sum(missing.values()), dropped,
                         f"{name} b{budget}: lost words {dict(missing)}")
        self.assertEqual(len(out), len(kept) - dropped + repeats,
                         f"{name} b{budget}: accounting out={len(out)} kept={len(kept)} "
                         f"dropped={dropped} repeats={repeats}")
        for piece in plan.pieces:
            self.assertTrue(piece.body.strip(), f"{name} b{budget}: empty piece")
            if piece.token_estimate > budget:
                self.assertEqual(len(piece.body.split()), 1,
                                 f"{name} b{budget}: oversize multi-word piece "
                                 f"{piece.body[:60]!r}")
        rendered, params = render_for_lightrag(plan)
        delimiter = params["params"]["split_by_character"]
        self.assertNotIn(delimiter, "".join(piece.body for piece in plan.pieces),
                         f"{name} b{budget}: delimiter inside a body")
        parts = [part for part in rendered.split(delimiter) if part]
        self.assertEqual(len(parts), len(plan.pieces),
                         f"{name} b{budget}: render split into {len(parts)}")
        for piece, part in zip(plan.pieces, parts):
            self.assertEqual(part, piece.render(),
                             f"{name} b{budget}: render drifted from the plan")
        return plan

    def test_invariants_hold_for_every_fixture_at_three_budgets(self):
        for name, text in FIXTURES.items():
            for budget in (60, 120, 350):
                with self.subTest(fixture=name, budget=budget):
                    self.check(name, text, budget)

    def test_invariants_hold_without_headers(self):
        for name, text in FIXTURES.items():
            with self.subTest(fixture=name):
                plan = self.check(name, text, 120, headers=False)
                self.assertTrue(all(piece.header is None for piece in plan.pieces))

    def test_the_plan_is_deterministic(self):
        for name, text in FIXTURES.items():
            with self.subTest(fixture=name):
                first = chunk_document(text, filename="Doc_2026-01-02.txt",
                                       budget_tokens=140, header_tokens=25)
                second = chunk_document(text, filename="Doc_2026-01-02.txt",
                                        budget_tokens=140, header_tokens=25)
                self.assertEqual([piece.render() for piece in first.pieces],
                                 [piece.render() for piece in second.pieces])

    def test_no_mid_word_cut(self):
        """Splitting a word would change the word multiset; that is what this checks."""
        text = "Supercalifragilistic expialidocious " * 20 + "\n\n" + "hyphen-ated tokens here " * 20
        plan = chunk_document(text, filename="words.txt", budget_tokens=40,
                              header_tokens=10)
        for piece in plan.pieces:
            self.assertNotIn("Supercalifragilisticexpialidocious", piece.body)
        self.assertEqual(Counter(_content_words(" ".join(p.body for p in plan.pieces))),
                         Counter(_content_words(text)))


# ── headers ────────────────────────────────────────────────────────────────

class TestHeaders(unittest.TestCase):
    def test_header_format_and_omitted_fields(self):
        plan = chunk_document(HEADINGS, filename="Procurement_2026-03-15.md",
                              budget_tokens=300, header_tokens=40)
        header = plan.pieces[0].header
        self.assertTrue(header.startswith("[Doc: Procurement | Date: 2026-03-15"),
                        header)
        self.assertTrue(header.endswith("]"), header)
        self.assertIn("Part 1/", header)
        titleless = chunk_document(HEADINGS, filename="transcript.txt",
                                   budget_tokens=300, header_tokens=40)
        self.assertNotIn("Doc:", titleless.pieces[0].header)
        self.assertIn("Part 1/", titleless.pieces[0].header)

    def test_header_is_capped_and_fields_are_dropped_in_priority_order(self):
        deep = ("# Alpha\n## Beta\n## Gamma\n### Delta deep inside the document with more\n"
                "Some body text that belongs to the deepest section of this file.\n")
        plan = chunk_document(deep, filename="Alpha_2026-03-15.md",
                              budget_tokens=300, header_tokens=10)
        for piece in plan.pieces:
            self.assertLessEqual(token_count(piece.header), 10,
                                 f"header over cap: {piece.header!r}")
        self.assertIn("header-fields-dropped-to-fit", plan.warnings)
        last = plan.pieces[-1].header
        self.assertNotIn("Section:", last, "the section path goes first")
        self.assertNotIn("Date:", last, "then the date")
        self.assertIn("Doc: Alpha", last, "the document title is the LAST field dropped")

    def test_an_impossibly_small_cap_still_leaves_a_part_marker(self):
        plan = chunk_document(HEADINGS, filename="Procurement_2026-03-15.md",
                              budget_tokens=300, header_tokens=1)
        for piece in plan.pieces:
            self.assertTrue(piece.header.startswith("[Part ") and piece.header.endswith("]"),
                            piece.header)
        self.assertIn("header-minimised", plan.warnings)

    def test_header_never_eats_the_body_budget(self):
        text = "Word. " * 200
        plan = chunk_document(text, filename="A_Very_Long_Document_Name_For_A_Test_2026.txt",
                              budget_tokens=80, header_tokens=60)
        for piece in plan.pieces:
            self.assertLessEqual(piece.token_estimate, 80)

    def test_headers_false_means_no_header_at_all(self):
        plan = chunk_document(HEADINGS, filename="Procurement.md", budget_tokens=120,
                              headers=False)
        self.assertTrue(all(piece.header is None for piece in plan.pieces))
        self.assertEqual(plan.pieces[0].token_estimate,
                         token_count(plan.pieces[0].body))

    def test_shuffled_placebo_keeps_the_words_and_moves_the_attachment(self):
        honest = chunk_document(HEADINGS, filename="Procurement_2026-03-15.md",
                                budget_tokens=120, header_tokens=40)
        placebo = chunk_document(HEADINGS, filename="Procurement_2026-03-15.md",
                                 budget_tokens=120, header_tokens=40, headers="shuffled")
        self.assertEqual([_content_words(piece.body) for piece in honest.pieces],
                         [_content_words(piece.body) for piece in placebo.pieces],
                         "the placebo may not change any body")
        self.assertGreater(len(honest.pieces), 2)
        self.assertTrue(any(piece.section_path for piece in honest.pieces),
                        "this fixture must produce distinct section paths")
        self.assertEqual(Counter(_content_words(" ".join(p.header or "" for p in honest.pieces))),
                         Counter(_content_words(" ".join(p.header or "" for p in placebo.pieces))),
                         "same header words, wrong piece")
        self.assertNotEqual([piece.header for piece in honest.pieces],
                            [piece.header for piece in placebo.pieces],
                            "the attachment has to actually move")
        self.assertEqual(placebo.stats.get("headers_shuffled"), 1)
        for piece in placebo.pieces:
            self.assertIn(f"Part {piece.part_index}/{piece.part_total}", piece.header,
                          "part numbers stay about the piece they now sit on")

    def test_body_and_header_together_are_the_number_the_budget_applies_to(self):
        plan = chunk_document(FURNITURE, filename="Deck.txt", budget_tokens=70,
                              header_tokens=25)
        for piece in plan.pieces:
            self.assertEqual(piece.token_estimate, token_count(piece.render()))


# ── delimiter and the LightRAG params ─────────────────────────────────────

class TestDelimiterAndRender(unittest.TestCase):
    def test_the_chosen_delimiter_is_absent_from_every_body(self):
        plan = chunk_document(TRANSCRIPT, filename="Standup.txt", budget_tokens=80)
        delimiter = pick_delimiter(plan)
        self.assertIn(delimiter, DELIMITER_CANDIDATES)
        self.assertNotIn(delimiter, "".join(piece.body for piece in plan.pieces))

    def test_an_explicit_delimiter_that_occurs_in_the_text_is_refused(self):
        plan = chunk_document(TRANSCRIPT, filename="Standup.txt", budget_tokens=80)
        with self.assertRaises(StructureChunkerError) as caught:
            pick_delimiter(plan, "(Alice Chen)")
        self.assertIn("would cut where this plan did not", str(caught.exception))

    def test_a_whitespace_only_delimiter_is_refused(self):
        plan = chunk_document(PLAIN, filename="notes.txt", budget_tokens=80)
        with self.assertRaises(StructureChunkerError):
            pick_delimiter(plan, "   ")

    def test_params_are_the_shape_lightrag_1_5_7_accepts(self):
        plan = chunk_document(MARKDOWN_TABLE, filename="Costs.md", budget_tokens=90)
        text, params = render_for_lightrag(plan)
        self.assertEqual(params["strategy"], "fixed_token")
        block = params["params"]
        self.assertIs(block["split_by_character_only"], True,
                      "a bool, not 1: that image answers a bool-as-int with a 422")
        self.assertIsInstance(block["chunk_token_size"], int)
        self.assertEqual(block["chunk_token_size"], 90)
        self.assertIsInstance(block["split_by_character"], str)
        self.assertTrue(text.endswith(block["split_by_character"]))

    def test_an_empty_plan_cannot_be_rendered(self):
        plan = chunk_document("", filename="blank.txt")
        with self.assertRaises(StructureChunkerError):
            render_for_lightrag(plan)


# ── token counting ─────────────────────────────────────────────────────────

class TestTokenCounting(unittest.TestCase):
    def test_the_estimator_over_counts_never_under_counts_short_text(self):
        for text in ("a", "hello world", "1", "a b c d e f g h i j k l m n o p",
                     "internationalization", "word. " * 30):
            self.assertGreaterEqual(token_count(text), len(text.split()),
                                    f"under-counted: {text!r}")

    def test_which_counter_was_used_is_reported_not_assumed(self):
        plan = chunk_document(PLAIN, filename="notes.txt", budget_tokens=80)
        self.assertIn(plan.stats["tokenizer"],
                      ("tiktoken:cl100k_base", "conservative-overcount"))
        self.assertEqual(plan.stats["tokenizer"], tokenizer_name())

    def test_token_percentiles_are_recorded(self):
        plan = chunk_document(PLAIN, filename="notes.txt", budget_tokens=50)
        self.assertLessEqual(plan.stats["tokens_p50"], plan.stats["tokens_p95"])
        self.assertLessEqual(plan.stats["tokens_p95"], plan.stats["tokens_max"])
        self.assertLessEqual(plan.stats["tokens_max"], 50)



# ── worker wiring (behind STRUCTURE_CHUNKING_ENABLED) ──────────────────────

import asyncio                                        # noqa: E402
import hashlib                                        # noqa: E402
import tempfile                                       # noqa: E402
from unittest.mock import AsyncMock, patch            # noqa: E402

os.environ.setdefault("RAG_API_KEY", "1234")
os.environ.setdefault("LIGHTRAG_INTERNAL_API_KEY", "internal")
os.environ.setdefault("LIGHTRAG_BASE_URL", "http://example:9621")
os.environ.setdefault("SOURCE_DOCS_DIR", "/tmp/source_docs")
os.environ.setdefault("UPLOADS_DIR", "/tmp/uploads")
os.environ.setdefault("STATE_DB_PATH", "/tmp/state.db")

from typing import Any                                # noqa: E402

import app.worker as worker                           # noqa: E402
from app.pdf_extract import pdftotext_available      # noqa: E402
from tests.test_document_extract import make_docx, make_pdf  # noqa: E402
import chunk_embed_bench as bench                     # noqa: E402


def _run_async(coroutine):
    return asyncio.new_event_loop().run_until_complete(coroutine)


class _FakeStore:
    """The five state-store calls submit_file makes, in memory."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def get_document_by_path(self, path: str) -> dict | None:
        return self.docs.get(path)

    async def get_documents_by_sha256(self, digest: str) -> list[dict]:
        return [doc for doc in self.docs.values() if doc.get("sha256") == digest]

    async def upsert_document(self, path: str, **fields) -> None:
        self.docs.setdefault(path, {"path": path}).update(fields)

    async def update_document_state(self, path: str, **fields) -> bool:
        self.docs.setdefault(path, {"path": path}).update(fields)
        return True

    async def increment_retry(self, path: str, **kwargs) -> int:
        return 0

    def meta(self, path: str) -> dict:
        block = json.loads(self.docs.get(path, {}).get("warnings_json") or "{}").get("worker")
        return block if isinstance(block, dict) else {}


DOC_TEXT = ("Quarterly numbers\n\n| Region | Q1 | Q2 |\n|---|---|---|\n"
            + "\n".join(f"| north {index} | {index * 3} | {index * 5} |"
                        for index in range(1, 8))
            + "\n\nThe north grew every quarter of the year, on the same headcount.\n")


class TestWorkerWiring(unittest.TestCase):
    """Off must be byte-identical; on must send the params; failure must fall back."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        (self.root / "source_docs").mkdir(parents=True)
        self.store = _FakeStore()
        self.post_json = AsyncMock(return_value={"track_id": "trk-1"})
        self.post_file = AsyncMock(return_value={"track_id": "trk-1"})
        patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.root / "source_docs"),
            uploads_dir=str(self.root / "uploads"),
            transcript_prep_enabled=False,
            structure_chunking_enabled=False,
            structure_chunking_budget_tokens=120,
            structure_chunking_header_tokens=20,
            structure_chunking_headers=True,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def _patches(self):
        return patch.multiple(
            worker,
            get_document_by_path=self.store.get_document_by_path,
            get_documents_by_sha256=self.store.get_documents_by_sha256,
            update_document_state=self.store.update_document_state,
            upsert_document=self.store.upsert_document,
            increment_retry=self.store.increment_retry,
        )

    def _submit(self, name: str, content: str) -> dict:
        path = self.root / "source_docs" / name
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest,
                                      "status": "accepted", "company": None,
                                      "filename": name, "original_filename": name,
                                      "warnings_json": None}
        with self._patches(), patch.object(worker.client, "post_json", self.post_json), \
             patch.object(worker.client, "post_file", self.post_file):
            _run_async(worker.submit_file(path, "filesystem", digest))
        body = self.post_json.await_args.args[1]
        return {"path": str(path), "digest": digest, "body": body, "text": body["text"]}

    def test_flag_off_body_is_byte_identical_to_today(self):
        got = self._submit("numbers.md", DOC_TEXT)
        self.assertEqual(set(got["body"]), {"text", "file_source"},
                         "no chunking key at all when the flag is off")
        self.assertEqual(got["text"], worker.normalize_text_submission_payload(DOC_TEXT))
        self.assertNotIn("structure_chunking", self.store.meta(got["path"]))

    def test_flag_on_posts_the_delimiter_params_and_keeps_the_dedupe_hash(self):
        with patch.object(worker.settings, "structure_chunking_enabled", True):
            got = self._submit("numbers_2026-04.md", DOC_TEXT)
        params = got["body"]["chunking"]
        self.assertEqual(params["strategy"], "fixed_token")
        self.assertTrue(params["params"]["split_by_character_only"])
        # chunk_token_size is NOT the budget: it must clear the largest piece, or LightRAG
        # is free to re-cut the pieces we chose (see the fit-check tests below).
        self.assertGreaterEqual(params["params"]["chunk_token_size"], 120)
        self.assertIn(params["params"]["split_by_character"], got["text"])
        self.assertIn("[Doc: numbers 2026 04 | Part 1/3]", got["text"],
                         "the header is built from the file name, title only here")
        row = self.store.docs[got["path"]]
        self.assertEqual(row["sha256"], got["digest"],
                         "the dedupe hash stays the ORIGINAL file bytes")

    def test_flag_on_records_provenance_as_counts_and_names_only(self):
        with patch.object(worker.settings, "structure_chunking_enabled", True):
            got = self._submit("numbers_2026-04.md", DOC_TEXT)
        meta = self.store.meta(got["path"])
        self.assertEqual(meta["structure_chunking"], "applied")
        self.assertEqual(meta["structure_chunking_kind"], "table")
        self.assertGreater(meta["structure_chunking_pieces"], 1)
        self.assertLessEqual(meta["structure_chunking_tokens_max"], 120)
        self.assertIn(meta["structure_chunking_tokenizer"],
                      ("tiktoken:cl100k_base", "conservative-overcount"))
        self.assertIsInstance(meta["structure_chunking_warnings"], list)
        blob = json.dumps(meta)
        for leak in ("Region", "north 1", "headcount"):
            self.assertNotIn(leak, blob, "provenance must carry no document text")

    def test_headers_false_reaches_the_plan(self):
        with patch.multiple(worker.settings, structure_chunking_enabled=True,
                            structure_chunking_headers=False):
            got = self._submit("numbers_2026-04.md", DOC_TEXT)
        self.assertNotIn("[Doc:", got["text"])
        self.assertIn("split_by_character", got["body"]["chunking"]["params"])

    def test_a_failing_planner_falls_back_to_the_untouched_text(self):
        def explode(*args, **kwargs):
            raise RuntimeError("planner exploded")
        with patch.object(worker.settings, "structure_chunking_enabled", True), \
             patch.object(worker, "chunk_document", explode):
            got = self._submit("numbers_2026-04.md", DOC_TEXT)
        self.assertEqual(got["text"], worker.normalize_text_submission_payload(DOC_TEXT),
                         "the document still goes in, unchunked")
        self.assertNotIn("chunking", got["body"], "and no params are sent with old text")
        meta = self.store.meta(got["path"])
        self.assertEqual(meta["structure_chunking"], "failed")
        self.assertIn("planner exploded", meta["structure_chunking_reason"])

    def test_binary_upload_path_is_untouched(self):
        path = self.root / "source_docs" / "deck.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest,
                                      "status": "accepted", "company": None,
                                      "filename": "deck.pdf", "original_filename": "deck.pdf",
                                      "warnings_json": None}
        with patch.object(worker.settings, "structure_chunking_enabled", True), \
             self._patches(), patch.object(worker.client, "post_json", self.post_json), \
             patch.object(worker.client, "post_file", self.post_file):
            _run_async(worker.submit_file(path, "filesystem", digest))
        self.assertTrue(self.post_file.await_count == 1,
                        "a .pdf must still go through /documents/upload")
        self.assertEqual(self.post_json.await_count, 0,
                         "the extractor route is a separate decision, not this one")


class TestBinaryRoutes(unittest.TestCase):
    """PDF, DOCX and PPTX reach LightRAG pre-cut, or they reach it as they do today.

    Every failure mode has to land on the upload route with a recorded reason: a parser bug
    may change how a document is chunked, never whether it is ingested.
    """

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.docs = self.root / "source_docs"
        self.docs.mkdir(parents=True)
        self.store = _FakeStore()
        self.post_json = AsyncMock(return_value={"track_id": "trk-1"})
        self.post_file = AsyncMock(return_value={"track_id": "trk-1"})
        patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.docs), uploads_dir=str(self.root / "uploads"),
            transcript_prep_enabled=False, structure_chunking_enabled=True,
            structure_chunking_budget_tokens=160, structure_chunking_header_tokens=20,
            structure_chunking_headers=True, structure_chunking_max_piece_tokens=450,
            structure_chunking_fit_margin_tokens=64,
            structure_chunking_embed_window_tokens=512,
            structure_chunking_embed_tokenizer_path="",
            structure_chunking_fail_on_lost_text=False,
            worker_pptx_extract_enabled=True, worker_pdf_extract_enabled=True,
            worker_docx_extract_enabled=True, worker_pdf_text_backend="auto",
            worker_pdf_max_bytes=0, worker_pdf_max_chars=0, worker_docx_max_bytes=0,
            worker_docx_max_chars=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _submit(self, path: Path) -> dict[str, Any]:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest, "status": "accepted",
                                      "company": None, "filename": path.name,
                                      "original_filename": path.name, "warnings_json": None}
        with patch.multiple(worker,
                            get_document_by_path=self.store.get_document_by_path,
                            get_documents_by_sha256=self.store.get_documents_by_sha256,
                            update_document_state=self.store.update_document_state,
                            upsert_document=self.store.upsert_document,
                            increment_retry=self.store.increment_retry), \
             patch.object(worker.client, "post_json", self.post_json), \
             patch.object(worker.client, "post_file", self.post_file):
            _run_async(worker.submit_file(path, "filesystem", digest))
        return {"path": str(path), "digest": digest,
                "json_body": self.post_json.await_args.args[1] if self.post_json.await_count else None,
                "text_route": self.post_json.await_count, "upload_route": self.post_file.await_count,
                "meta": self.store.meta(str(path))}

    def test_a_docx_table_goes_in_pre_cut_with_the_header_repeated(self):
        rows = [["Region", "Q1", "Q2"]] + [
            [f"north {index}", str(index * 3), str(index * 5)] for index in range(1, 61)]
        path = make_docx([("Heading1", "Budget detail"), ("", "Every region, every quarter.")],
                         [rows], ["p", "p", "t"], self.docs / "budget_2026-04.docx")
        got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (0, 1),
                         "the worker parsed it, so the text route is the one that carries cuts")
        body = got["json_body"]
        params = body["chunking"]
        self.assertTrue(params["params"]["split_by_character_only"])
        delimiter = params["params"]["split_by_character"]
        pieces = [piece for piece in body["text"].split(delimiter) if piece.strip()]
        self.assertGreater(len(pieces), 1)
        table_pieces = [piece for piece in pieces if "| north" in piece]
        self.assertGreaterEqual(len(table_pieces), 2, "the 60-row table had to split")
        for piece in table_pieces:
            self.assertIn("Region | Q1 | Q2", piece,
                          "every piece the table split across keeps the header row")
        meta = got["meta"]
        self.assertEqual(meta["structure_extract"], "worker")
        self.assertEqual(meta["structure_extract_kind"], "docx")
        self.assertEqual(meta["structure_chunking"], "applied")
        self.assertEqual(meta["structure_chunking_kind"], "table")
        self.assertLessEqual(meta["structure_chunking_tokens_max"], 160)
        self.assertEqual(meta["structure_chunking_lost_word_tokens"], 0)
        self.assertEqual(got["digest"], hashlib.sha256(path.read_bytes()).hexdigest(),
                         "the dedupe hash is still the file bytes, not the parsed text")

    def test_a_pdf_loses_its_page_furniture_but_keeps_every_row(self):
        if not pdftotext_available():
            self.skipTest("poppler's pdftotext is not installed here")
        pages = [
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "1. Revenue"),
             (72, 700, "Region        Q1         Q2"),
             (72, 686, "north         120        150"),
             (180, 672, "grew on the same headcount"),
             (72, 658, "south         70         90")],
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "2. Costs"),
             (72, 700, "Costs fell in the north region after the vendor renegotiation.")],
            [(72, 760, "Acme Corporation confidential"),
             (72, 720, "3. Risks"),
             (72, 700, "Risks were unchanged from the previous quarter, per the register.")],
        ]
        path = make_pdf(pages, self.docs / "report_2026-04.pdf")
        got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (0, 1))
        text = got["json_body"]["text"]
        self.assertNotIn("Acme Corporation confidential", text,
                         "the running header is furniture and is counted, not submitted")
        self.assertIn("grew on the same headcount", text, "the wrapped row stayed one row")
        meta = got["meta"]
        self.assertEqual(meta["structure_extract_kind"], "pdf")
        self.assertGreaterEqual(meta["structure_chunking_dropped_lines"], 3)

    def test_a_deck_takes_the_same_preparation_as_a_text_file(self):
        try:
            from pptx import Presentation
        except ImportError:
            self.skipTest("python-pptx is not installed")
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "Regions"
        table = deck.slides[0].shapes.add_table(3, 3, 100000, 2000000, 4000000, 1000000).table
        for column, heading in enumerate(("Region", "Q1", "Q2")):
            table.cell(0, column).text = heading
        for row, values in enumerate((("north", "12", "15"), ("south", "7", "9")), start=1):
            for column, value in enumerate(values):
                table.cell(row, column).text = value
        path = self.docs / "deck.pptx"
        deck.save(str(path))
        got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (0, 1))
        self.assertIn("chunking", got["json_body"],
                      "the deck is pre-cut like every other document now")

    def test_with_the_flag_off_a_pdf_is_still_uploaded_untouched(self):
        path = self.docs / "plain.pdf"
        path.write_bytes(b"%PDF-1.4\nnot really, but the route never reads it\n")
        with patch.object(worker.settings, "structure_chunking_enabled", False):
            got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (1, 0))
        self.assertNotIn("structure_extract", got["meta"], "no new metadata either")

    def test_a_kill_switch_on_one_type_leaves_the_other_alone(self):
        rows = [["Region", "Q1"], ["north", "12"]]
        docx = make_docx([("", "table")], [rows], ["p", "t"], self.docs / "one.docx")
        with patch.object(worker.settings, "worker_docx_extract_enabled", False):
            got = self._submit(docx)
        self.assertEqual((got["upload_route"], got["text_route"]), (1, 0))

    def test_a_scan_falls_back_to_upload_with_the_reason_recorded(self):
        path = make_pdf([[], []], self.docs / "scan.pdf")
        got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (1, 0),
                         "the document still gets ingested; only the cuts are lost")
        meta = got["meta"]
        self.assertEqual(meta["structure_extract"], "fallback_upload")
        self.assertEqual(meta["structure_extract_error"], "pdf_empty")
        self.assertIn("no extractable text", meta["structure_extract_reason"])

    def test_a_corrupt_docx_falls_back_to_upload(self):
        path = self.docs / "broken.docx"
        path.write_bytes(b"definitely not a zip")
        got = self._submit(path)
        self.assertEqual((got["upload_route"], got["text_route"]), (1, 0))
        self.assertEqual(got["meta"]["structure_extract_error"], "docx_extract_failed")

    def test_a_piece_over_the_ceiling_is_not_submitted_chunked(self):
        path = self.docs / "monster.txt"
        path.write_text("start " + "x" * 1200 + " end\n", encoding="utf-8")
        with patch.object(worker.settings, "structure_chunking_max_piece_tokens", 200):
            got = self._submit(path)
        self.assertEqual(got["meta"]["structure_chunking"], "fallback")
        self.assertIn("MAX_PIECE_TOKENS", got["meta"]["structure_chunking_reason"])
        self.assertNotIn("chunking", got["json_body"],
                         "an uncut submission beats a submission that breaks the ceiling")

    def test_the_window_check_uses_the_embedders_own_token_count_when_configured(self):
        tokenizer_dir = self.root / "tokenizer"
        tokenizer_dir.mkdir()
        (tokenizer_dir / "vocab.txt").write_text("[CLS]\n[SEP]\n[UNK]\nthe\n", encoding="utf-8")
        path = self.docs / "numbers.txt"
        path.write_text("the " * 40 + "\n", encoding="utf-8")
        with patch.multiple(worker.settings,
                            structure_chunking_embed_tokenizer_path=str(tokenizer_dir),
                            structure_chunking_embed_window_tokens=30):
            got = self._submit(path)
        meta = got["meta"]
        self.assertEqual(meta["structure_chunking"], "fallback")
        self.assertIn("embedder window", meta["structure_chunking_reason"])
        self.assertIn("embedder tokens", meta["structure_chunking_fit_basis"])
        self.assertGreater(meta["structure_chunking_embedder_tokens_max"], 30)
        self.assertNotIn("chunking", got["json_body"])

    def test_the_default_window_check_runs_on_the_conservative_estimate(self):
        path = self.docs / "long.txt"
        path.write_text("word " * 200 + "\n", encoding="utf-8")
        with patch.object(worker.settings, "structure_chunking_embed_window_tokens", 120):
            got = self._submit(path)
        self.assertEqual(got["meta"]["structure_chunking"], "fallback")
        self.assertIn("estimate", got["meta"]["structure_chunking_fit_basis"])

    def test_chunk_token_size_clears_the_largest_piece_so_nothing_is_re_cut(self):
        path = self.docs / "table.txt"
        lines = ["| Region | Q1 | Q2 |", "|---|---|---|"] + [
            f"| north {index} | {index * 3} | {index * 5} |" for index in range(1, 40)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        got = self._submit(path)
        params = got["json_body"]["chunking"]["params"]
        largest = got["meta"]["structure_chunking_tokens_max"]
        self.assertGreaterEqual(params["chunk_token_size"], largest + 64,
                                "if chunk_token_size sat at the budget, LightRAG could re-cut")

    def test_a_payload_whose_cuts_do_not_round_trip_is_not_submitted_chunked(self):
        def broken(plan, delimiter=None, *, chunk_token_size=None):
            return ("text with no markers at all",
                    {"strategy": "fixed_token",
                     "params": {"chunk_token_size": 400, "split_by_character": "\x1e",
                                "split_by_character_only": True}})
        path = self.docs / "words.txt"
        path.write_text("\n".join(f"row {index} records alpha beta gamma delta"
                                   for index in range(1, 9)) + "\n", encoding="utf-8")
        with patch.object(worker, "render_for_lightrag", broken):
            got = self._submit(path)
        self.assertEqual(got["meta"]["structure_chunking"], "fallback")
        self.assertIn("cut markers", got["meta"]["structure_chunking_reason"])
        self.assertEqual(got["meta"]["structure_chunking_delimiters"], 0)
        self.assertNotIn("chunking", got["json_body"],
                         "no params without the matching markers in the text")

    def test_a_coverage_gap_is_recorded_and_only_blocks_when_told_to(self):
        path = self.docs / "words.txt"
        path.write_text("alpha beta gamma delta epsilon zeta eta theta\n", encoding="utf-8")
        def leaking(source_text, plan):
            return {"source_word_tokens": 9, "piece_word_tokens": 9,
                    "furniture_word_tokens": 0, "missing_word_tokens": 3,
                    "unaccounted_word_tokens": 3, "unaccounted_distinct": 3,
                    "covered_pct": 66.67}
        with patch.object(worker, "coverage_gaps", leaking):
            got = self._submit(path)
            self.assertEqual(got["meta"]["structure_chunking_lost_word_tokens"], 3)
            self.assertEqual(got["meta"]["structure_chunking"], "applied",
                             "a gap is recorded by default: blocking ingest on a heuristic "
                             "is a worse outcome than the gap")
            with patch.object(worker.settings, "structure_chunking_fail_on_lost_text", True):
                strict = self._submit(path)
        self.assertEqual(strict["meta"]["structure_chunking"], "fallback")
        self.assertIn("3 word tokens", strict["meta"]["structure_chunking_reason"])
        self.assertNotIn("chunking", strict["json_body"])

    def test_transcript_text_is_still_cut_at_turn_boundaries(self):
        path = self.docs / "meeting.txt"
        turns = [f"Alice: point number {index} about the quarterly budget review meeting."
                 for index in range(1, 15)]
        path.write_text("\n".join(turns) + "\n", encoding="utf-8")
        got = self._submit(path)
        meta = got["meta"]
        self.assertEqual(meta["structure_chunking_kind"], "transcript")
        delimiter = got["json_body"]["chunking"]["params"]["split_by_character"]
        pieces = [piece for piece in got["json_body"]["text"].split(delimiter) if piece.strip()]
        self.assertGreater(len(pieces), 1)
        for piece in pieces:
            self.assertTrue(piece.splitlines()[1].startswith("Alice:"),
                            "a turn is never split, so a piece starts at a turn")


# ── the benchmark arm, so the same function is measured and shipped ────────

class TestBenchmarkStrategy(unittest.TestCase):
    def test_the_grid_carries_the_structure_arms(self):
        configs = bench.load_configs(CONFIGS)
        by_name = {config["name"]: config for config in configs}
        for name in ("structure-350", "structure-350+rerank", "structure-350-nohdr",
                     "structure-350-shuffled"):
            self.assertIn(name, by_name, f"the grid is missing {name}")
        self.assertEqual(by_name["structure-350"]["chunker"]["strategy"], "structure")
        self.assertEqual(by_name["structure-350"]["chunker"]["budget_tokens"], 350)
        self.assertIs(by_name["structure-350-nohdr"]["chunker"]["headers"], False)
        self.assertEqual(by_name["structure-350-shuffled"]["chunker"]["headers"], "shuffled")

    def test_the_structure_strategy_routes_to_the_shipped_module(self):
        tokenizer = bench.WhitespaceTokenizer()
        pieces = bench.chunk_document(HEADINGS, {"strategy": "structure",
                                                "budget_tokens": 60, "headers": True},
                                      tokenizer)
        self.assertGreater(len(pieces), 1)
        self.assertIn("[Section:", pieces[0])
        from app import structure_chunker as shipped
        direct = shipped.chunk_document(HEADINGS, filename="", budget_tokens=60)
        self.assertEqual(pieces, [piece.render() for piece in direct.pieces],
                         "the benchmark must not keep its own imitation of the chunker")

    def test_the_placebo_is_accepted_by_the_loader_and_moves_headers(self):
        specs = {"headers": "shuffled", "budget_tokens": 60}
        plain = bench.chunk_document(HEADINGS, {"strategy": "structure", "headers": True},
                                     bench.WhitespaceTokenizer())
        shuffled = bench.chunk_document(HEADINGS, {"strategy": "structure", **specs},
                                        bench.WhitespaceTokenizer())
        self.assertNotEqual([chunk.splitlines()[0] for chunk in plain],
                            [chunk.splitlines()[0] for chunk in shuffled])

    def test_bad_structure_settings_are_refused_before_any_embedding(self):
        base = {"name": "a", "chunker": {"strategy": "structure"},
                "embedder": {"model": "m"}}
        for bad in ({"budget_tokens": 5}, {"budget_tokens": "350"},
                    {"header_tokens": -1}, {"headers": "yes"}):
            with self.subTest(setting=bad):
                payload = dict(base, chunker={"strategy": "structure", **bad})
                path = Path(tempfile.mkdtemp()) / "configs.json"
                path.write_text(json.dumps({"configs": [payload]}), encoding="utf-8")
                with self.assertRaises(bench.BenchError):
                    bench.load_configs(path)

    def test_the_error_message_lists_the_structure_strategy(self):
        with self.assertRaises(bench.BenchError) as caught:
            bench.chunk_document("text", {"strategy": "custom"}, bench.WhitespaceTokenizer())
        self.assertIn("structure", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
