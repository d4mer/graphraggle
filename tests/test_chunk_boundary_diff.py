"""Offline tests for scripts/chunk_boundary_diff.py.

Every fixture here is invented text. The two commits the end-to-end tests compare are real
commits of this repo (the chunker rule change), because the tool's whole job is to compare
commits - but nothing in these tests reads a corpus file, a probe, staging or LightRAG data.

What the tests hold the tool to:
- it reports counts, offsets, digests and booleans, and NEVER any input text;
- a boundary that moves without any piece changing still counts as divergence at that index
  while sha_overlap stays high (that distinction is the whole point of the two columns);
- each side runs against its own checkout, so the two versions of the module cannot see
  each other;
- a ref that resolves to neither a commit nor a repo tree fails loudly rather than quietly
  chunking the working tree.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT = ROOT_DIR / "scripts" / "chunk_boundary_diff.py"

LEFT = "0047a69"     # _TERMINAL.match: all-caps lines ending in . ! ? were headings
RIGHT = "f7051e6"    # _TERMINAL.search: they are not

# Invented PDF-like text: running header, page marks, an all-caps section line that ends in a
# full stop (the line whose verdict flips), an all-caps line that does NOT end in one, an
# "A.B. CORP." style line, a numbered heading, a table and body prose.
FIXTURE_LINES = [
    "ACME FREIGHT LEDGER - CONFIDENTIAL",
    "",
    "page 1",
    "",
    "FREIGHT COSTS ROSE IN MARCH.",          # index 4 (1-based line 5): the flip
    "The recorded freight spend for the northern lanes rose by eleven percent in March,",
    "which the carrier attributes to the surcharge introduced at the start of the month.",
    "Depot 0 handled 1,240 loads while depot 4 handled 980 loads over the same period.",
    "",
    "3.1 Lane detail",
    "",
    "Load   Depot   Cost",
    "1240   0   44900",
    "980   4   31100",
    "",
    "A.B. CORP",
    "A.B. CORP.",                            # also flips: a trailing full stop
    "The contract with A.B. CORP runs to the end of the year.",
    "",
    "UNITED STATES OF AMERICA",              # no terminal punctuation: heading on both sides
    "The board approved the annex at its 118th meeting without dissent.",
    "",
    "page 2",
    "",
    "DEPOT HOURS WERE CHANGED.",             # flips
    "Depot 7 opened at seven and closed at sixteen for nine working days in April.",
    "The exception log records twenty entries, of which three were cleared the same day.",
]
# 1-based line numbers of the all-caps lines carrying terminal punctuation.
FLIPPED_LINES = [5, 17, 25]
BOTH_SIDES_HEADING = 20      # "UNITED STATES OF AMERICA", no terminal punctuation


def _refs_exist() -> bool:
    for ref in (LEFT, RIGHT):
        done = subprocess.run(["git", "-C", str(ROOT_DIR), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if done.returncode != 0:
            return False
    return True


class TestDiffPieces(unittest.TestCase):
    """Pure arithmetic on two piece lists: no subprocess, no repo."""

    def setUp(self):
        self.mod = self._load()

    @staticmethod
    def _load():
        sys.path.insert(0, str(ROOT_DIR / "scripts"))
        import chunk_boundary_diff
        return chunk_boundary_diff

    def _side(self, texts: list[str]) -> dict:
        return {"shas": [hashlib.sha256(text.encode()).hexdigest()[:16] for text in texts],
                "lengths": [len(text) for text in texts]}

    def test_identical_sides_report_no_divergence(self):
        report = self.mod.diff_pieces(self._side(["alpha", "beta"]), self._side(["alpha", "beta"]))
        self.assertEqual((report["pieces_left"], report["pieces_right"]), (2, 2))
        self.assertEqual(report["identical_at_same_index"], 2)
        self.assertIsNone(report["first_divergence_index"])
        self.assertEqual(report["sha_overlap"], 2)
        self.assertEqual(report["regions"], [])

    def test_a_shifted_but_unchanged_piece_diverges_in_position_and_overlaps_in_content(self):
        """The distinction that makes the audit's "0 overlap" mean what it says."""
        left = self._side(["filler", "the depot closed at noon"])
        right = self._side(["the depot closed at noon", "filler"])
        report = self.mod.diff_pieces(left, right)
        self.assertEqual(report["identical_at_same_index"], 0)
        self.assertEqual(report["first_divergence_index"], 0)
        self.assertEqual(report["sha_overlap"], 2, "both sides contain both pieces, reordered")
        self.assertEqual(report["differing_positions"], 2)

    def test_offsets_and_lengths_are_reported_per_side(self):
        left, right = self._side(["aaaa", "bb"]), self._side(["aaaaa", "b"])
        report = self.mod.diff_pieces(left, right)
        first = report["regions"][0]
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["chars_left"], [0, 4])
        self.assertEqual(first["chars_right"], [0, 5])
        second = report["regions"][1]
        self.assertEqual(second["chars_left"], [4, 6])
        self.assertEqual(second["chars_right"], [5, 6])

    def test_extra_pieces_are_counted_and_divergence_starts_where_they_begin(self):
        report = self.mod.diff_pieces(self._side(["a", "b", "c"]), self._side(["a", "b"]))
        self.assertEqual(report["extra_on_left"], 1)
        self.assertEqual(report["identical_at_same_index"], 2)
        self.assertEqual(report["first_divergence_index"], 2)

    def test_length_summary_uses_the_same_regime_language_the_audit_reports(self):
        report = self.mod.diff_pieces(self._side(["a" * 10, "b" * 20]), self._side(["c" * 15]))
        self.assertEqual(report["length_p50_left"], 15.0)
        self.assertEqual(report["length_max_right"], 15)
        self.assertEqual(report["total_chars_left"], 30)
        self.assertEqual(report["extra_on_right"], 0)


class TestDiffHeadings(unittest.TestCase):
    def setUp(self):
        self.mod = self._load()

    @staticmethod
    def _load():
        sys.path.insert(0, str(ROOT_DIR / "scripts"))
        import chunk_boundary_diff
        return chunk_boundary_diff

    def test_flips_are_line_length_and_two_booleans(self):
        left = {"lines_examined": 4, "all_caps_true": 2, "heading_true": 2,
                "all_caps": [[1, 10, True], [2, 8, False], [3, 9, True], [4, 4, False]],
                "heading": [[1, 10, True], [2, 8, True], [3, 9, True], [4, 4, False]]}
        right = {"lines_examined": 4, "all_caps_true": 1, "heading_true": 1,
                 "all_caps": [[1, 10, True], [2, 8, False], [3, 9, False], [4, 4, False]],
                 "heading": [[1, 10, True], [2, 8, False], [3, 9, True], [4, 4, False]]}
        report = self.mod.diff_headings(left, right)
        self.assertEqual(report["all_caps"]["flipped"], 1)
        self.assertEqual(report["all_caps"]["flips"],
                         [{"line": 3, "length": 9, "left": True, "right": False}])
        self.assertEqual(report["heading"]["flipped"], 1)
        self.assertEqual(report["heading"]["flips"][0]["line"], 2)
        self.assertEqual(report["all_caps"]["true_left"], 2)
        self.assertEqual(report["all_caps"]["true_right"], 1)

    def test_no_line_text_survives_the_report(self):
        report = self.mod.diff_headings(
            {"lines_examined": 1, "all_caps_true": 1, "heading_true": 1,
             "all_caps": [[1, 30, True]], "heading": [[1, 30, True]]},
            {"lines_examined": 1, "all_caps_true": 0, "heading_true": 0,
             "all_caps": [[1, 30, False]], "heading": [[1, 30, False]]})
        self.assertNotIn("ACME", json.dumps(report))
        self.assertEqual(set(report["all_caps"]["flips"][0]), {"line", "length", "left", "right"})


@unittest.skipUnless(_refs_exist(), "the two comparison commits are not in this checkout")
class TestEndToEndAgainstRealCommits(unittest.TestCase):
    """The tool run for real, once per mode, on invented text."""

    fixture: Path
    runs: dict[str, dict] = {}

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.tmp = tempfile.TemporaryDirectory(prefix="cbd-test-")
        cls.fixture = Path(cls.tmp.name) / "invented.txt"
        cls.fixture.write_text("\n".join(FIXTURE_LINES) + "\n", encoding="utf-8")
        for mode, extra in (("pieces", []), ("headings", ["--only-function"])):
            done = subprocess.run(
                [sys.executable, str(SCRIPT), "--left", LEFT, "--right", RIGHT,
                 "--text-file", str(cls.fixture), "--filename", "invented.pdf",
                 "--repo", str(ROOT_DIR), "--env", "WORKER_PDF_TEXT_BACKEND=pypdf",
                 "--json", *extra],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT_DIR))
            assert done.returncode == 0, done.stderr.decode()[:400]
            cls.runs[mode] = json.loads(done.stdout.decode())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_both_sides_ran_against_archives_not_the_working_tree(self):
        for side in ("left", "right"):
            report = self.runs["pieces"][side]
            self.assertEqual(report["kind"], "archive")
            self.assertTrue(report["commit"], "a side without a commit is not a comparison")
        self.assertNotEqual(self.runs["pieces"]["left"]["commit"],
                            self.runs["pieces"]["right"]["commit"])

    def test_the_detector_disagreement_is_found_on_the_lines_i_planted(self):
        headings = self.runs["headings"]["headings"]
        caps = headings["all_caps"]
        self.assertEqual(caps["flipped"], len(FLIPPED_LINES),
                         "the planted all-caps lines ending in a full stop must flip")
        self.assertEqual([flip["line"] for flip in caps["flips"]], FLIPPED_LINES)
        for flip in caps["flips"]:
            self.assertTrue(flip["left"], "the old build called them headings")
            self.assertFalse(flip["right"], "the fixed build does not")
            self.assertEqual(flip["length"], len(FIXTURE_LINES[flip["line"] - 1]))
        self.assertEqual(headings["heading"]["flipped"], len(FLIPPED_LINES),
                         "the composite detector moves by exactly the same lines")

    def test_a_line_that_is_a_heading_on_both_sides_is_not_reported_as_flipped(self):
        caps = self.runs["headings"]["headings"]["all_caps"]
        self.assertNotIn(BOTH_SIDES_HEADING, [flip["line"] for flip in caps["flips"]])
        self.assertEqual(caps["true_left"] - caps["true_right"], caps["flipped"])

    def test_the_report_contains_no_input_text(self):
        """Counts, digests and line numbers only - that is what makes the tool shareable."""
        for mode, report in self.runs.items():
            blob = json.dumps(report)
            for line in FIXTURE_LINES:
                if len(line) > 4:
                    self.assertNotIn(line, blob, f"{mode} leaked a line of the fixture")
            for word in ("FREIGHT", "Depot", "ledger", "surcharge", "CORP"):
                self.assertNotIn(word.lower(), blob.lower(), f"{mode} leaked the word {word!r}")
            self.assertNotIn("invented text about freight", blob)

    def test_piece_digests_and_offsets_are_present_and_short(self):
        report = self.runs["pieces"]
        pieces = report["pieces"]
        for side in ("left", "right"):
            self.assertEqual(len(pieces[side]["shas"]), len(pieces[side]["lengths"]))
            for sha in pieces[side]["shas"]:
                self.assertRegex(sha, r"^[0-9a-f]{16}$")
        diff = report["diff"]
        self.assertEqual(diff["pieces_left"], len(pieces["left"]["shas"]))
        for region in diff["regions"]:
            self.assertEqual(region["chars_left"][1] - region["chars_left"][0],
                             region["length_left"])

    def test_env_pins_are_echoed_for_reproducibility(self):
        self.assertEqual(self.runs["pieces"]["env_pins"], {"WORKER_PDF_TEXT_BACKEND": "pypdf"})

    def test_the_two_interpreters_report_different_detector_counts(self):
        left, right = (self.runs["headings"][side] for side in ("left", "right"))
        self.assertGreater(left["all_caps_true"], right["all_caps_true"],
                           "the old build finds more all-caps headings than the fixed one")
        self.assertEqual(left["lines_examined"], right["lines_examined"])

    def test_the_text_file_route_does_not_run_an_extractor(self):
        for side in ("left", "right"):
            self.assertEqual(self.runs["pieces"][side]["extract_backend"], "text-file")


class TestRefResolution(unittest.TestCase):
    def test_an_unresolvable_ref_is_refused_naming_both_attempts(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cbd-ref-") as temp:
            fixture = Path(temp) / "invented.txt"
            fixture.write_text("\n".join(FIXTURE_LINES) + "\n", encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), "--left", "no-such-ref",
                                   "--right", "no-such-ref-either", "--text-file", str(fixture),
                                   "--repo", str(ROOT_DIR)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT_DIR))
        self.assertNotEqual(done.returncode, 0)
        message = done.stderr.decode()
        self.assertIn("no-such-ref", message)
        self.assertIn("commit-ish", message)

    def test_a_commit_ish_in_a_non_git_directory_is_refused_without_a_traceback(self):
        """An archive export or vendored copy has no git history: say so, do not raise."""
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cbd-nogit-") as temp:
            stub = Path(temp) / "app" / "structure_chunker.py"
            stub.parent.mkdir(parents=True)
            stub.write_text("", encoding="utf-8")
            fixture = Path(temp) / "invented.txt"
            fixture.write_text("\n".join(FIXTURE_LINES) + "\n", encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), "--left", "v9.9.9",
                                   "--right", "v9.9.9-alpha", "--text-file", str(fixture),
                                   "--repo", temp],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=temp)
        self.assertNotEqual(done.returncode, 0)
        message = done.stderr.decode()
        self.assertNotIn("Traceback", message)
        self.assertIn("v9.9.9", message)
        self.assertIn("commit-ish", message)

    def test_a_repo_tree_path_is_allowed_but_labelled(self):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="cbd-tree-") as temp:
            fixture = Path(temp) / "invented.txt"
            fixture.write_text("\n".join(FIXTURE_LINES) + "\n", encoding="utf-8")
            done = subprocess.run([sys.executable, str(SCRIPT), "--left", str(ROOT_DIR),
                                   "--right", str(ROOT_DIR), "--text-file", str(fixture),
                                   "--repo", str(ROOT_DIR), "--json"],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(ROOT_DIR))
        self.assertEqual(done.returncode, 0, done.stderr.decode()[:400])
        report = json.loads(done.stdout.decode())
        for side in ("left", "right"):
            self.assertIn("NOT a clean checkout", report[side]["kind"])
        self.assertEqual(report["diff"]["sha_overlap"], report["diff"]["pieces_left"],
                         "the same tree over the same text must agree with itself")


if __name__ == "__main__":
    unittest.main()
