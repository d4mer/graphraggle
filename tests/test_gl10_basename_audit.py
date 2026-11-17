"""The basename audit: the numbers the re-ingest decision is made from, and one contract with the app.

Fixtures are invented company names and invented file bytes in a temp directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_SPEC = importlib.util.spec_from_file_location(
    "gl10_basename_audit",
    Path(__file__).resolve().parents[1] / "scripts" / "gl10_basename_audit.py")
audit = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(audit)

import app.document_identity as document_identity


class TestSlugRuleMatchesTheApp(unittest.TestCase):
    def test_the_two_rules_agree_on_every_shape_we_expect(self):
        for company in ("Northwind Chemicals", "Solvay & Cie.", "Média  Nord", "acme-corp",
                        "Acme Corp", "!!!", "", "  Spaced   Out  "):
            with self.subTest(company=company):
                self.assertEqual(audit.company_slug(company),
                                 document_identity.company_slug(company))


class TestBuckets(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory(prefix="grg-audit-")
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.alpha = self.root / "Alpha Chemicals"
        self.beta = self.root / "Beta Logistics"
        self.alpha.mkdir()
        self.beta.mkdir()

    def write(self, company_dir: Path, name: str, body: str) -> None:
        (company_dir / name).write_text(body, encoding="utf-8")

    def test_unique_shared_and_ambiguous_names_are_counted_apart(self):
        self.write(self.alpha, "unique-alpha.txt", "a")
        self.write(self.beta, "shared.txt", "same bytes")
        self.write(self.alpha, "shared.txt", "same bytes")
        self.write(self.alpha, "clash.txt", "alpha bytes")
        self.write(self.beta, "clash.txt", "beta bytes")
        self.write(self.alpha, "versions.txt", "v1")
        report = audit.scan_corpora([self.alpha, self.beta])
        counts = report["counts"]
        self.assertEqual(counts["unique"], 2, "unique-alpha + versions.txt (one company)")
        self.assertEqual(counts["shared_identical"], 1)
        self.assertEqual(counts["ambiguous"], 1)
        self.assertEqual(counts["unattributed_rows"], 0)
        self.assertEqual(report["files"], 6)

    def test_names_are_hashed_unless_asked(self):
        self.write(self.alpha, "clash.txt", "a")
        self.write(self.beta, "clash.txt", "b")
        report = audit.scan_corpora([self.alpha, self.beta])
        self.assertEqual(len(report["ambiguous_names"][0]["name_sha256"]), 16)
        self.assertNotIn("clash", json.dumps(report["ambiguous_names"]))

    def test_qualified_names_expose_a_prefix_collision(self):
        # Two companies that slug alike reproduce the ambiguity the prefix exists to remove; the audit
        # says so instead of hiding it.
        gamma = self.root / "acme-corp"
        delta = self.root / "Acme Corp"
        gamma.mkdir()
        delta.mkdir()
        (gamma / "q1.txt").write_text("one", encoding="utf-8")
        (delta / "q1.txt").write_text("two", encoding="utf-8")
        report = audit.scan_corpora([gamma, delta])
        self.assertEqual(report["qualified_name_collisions"], ["acme-corp--q1.txt"])


class TestStateDatabase(unittest.TestCase):
    def test_a_database_without_the_new_column_still_audits(self):
        import sqlite3

        with TemporaryDirectory(prefix="grg-audit-db-") as tmp:
            db = Path(tmp) / "state.db"
            conn = sqlite3.connect(db)
            conn.executescript(
                "CREATE TABLE documents (document_id TEXT PRIMARY KEY, path TEXT, filename TEXT,"
                " original_filename TEXT, sha256 TEXT, company TEXT, status TEXT);")
            for i, (path, company, sha) in enumerate((
                    ("/app/source_docs/Alpha/q3.pdf", "Alpha Chemicals", "sha-a"),
                    ("/app/source_docs/Beta/q3.pdf", "Beta Logistics", "sha-b"),
                    ("/app/source_docs/Alpha/own.pdf", "Alpha Chemicals", "sha-o"))):
                conn.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?)",
                             (f"d{i}", path, Path(path).name, Path(path).name, sha, company,
                              "ingested"))
            conn.commit()
            conn.close()
            report = audit.scan_state_db(db)
        self.assertEqual(report["rows"], 3)
        self.assertEqual(report["rows_with_submitted_source"], 0)
        self.assertEqual(report["counts"]["ambiguous"], 1)
        self.assertEqual(report["counts"]["unique"], 1)


if __name__ == "__main__":
    unittest.main()
