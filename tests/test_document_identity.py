"""The citation→document join: LightRAG returns a bare basename, the store holds an absolute path.

Seeded the way the real doors seed it: `documents.path` absolute, `documents.filename` the basename,
and citations carrying only what LightRAG gives back. An exact-path join against these rows matches
nothing, which is the bug these tests exist to keep fixed (stack2 R1/R2: 1046 citations, 0 attributed).
"""
from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

import app.api as api
import app.document_identity as document_identity
import app.state_store as state_store
from app.config import settings

NW = "Northwind Chemicals"
CP = "Contoso Pharma"
NW_DIR = "/app/source_docs/Northwind Chemicals"
CP_DIR = "/app/source_docs/Contoso Pharma"


def _run(coro):
    return asyncio.run(coro)


class SeededStore(unittest.TestCase):
    """A store with absolute paths and basenames, as the filesystem door writes them."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-join-")
        self.base = Path(self._tmp.name)
        saved = (state_store.settings.state_db_path, state_store.settings.source_docs_dir,
                 state_store.settings.uploads_dir)
        self._saved = saved
        state_store.settings.state_db_path = str(self.base / "state.db")
        state_store.settings.source_docs_dir = str(self.base / "source_docs")
        state_store.settings.uploads_dir = str(self.base / "uploads")
        (self.base / "source_docs").mkdir()
        (self.base / "uploads").mkdir()
        _run(state_store.init_db())
        self.rows: list[dict] = []

    def tearDown(self) -> None:
        for name, value in zip(("state_db_path", "source_docs_dir", "uploads_dir"), self._saved):
            setattr(state_store.settings, name, value)
        self._tmp.cleanup()

    def add(self, *, path: str, company: str | None, filename: str | None = None,
            sha256: str = "sha-unique", status: str = "ingested", document_id: str | None = None,
            submitted_source: str | None = None, source_type: str = "scan") -> dict:
        row = {"path": path, "company": company, "filename": filename or Path(path).name,
               "sha256": sha256, "status": status, "document_id": document_id or f"doc-{len(self.rows)}",
               "source_type": source_type, "submitted_source": submitted_source}
        self.rows.append(row)
        _run(state_store.upsert_document(
            document_id=row["document_id"], path=path, source_type=source_type, company=company,
            filename=row["filename"], original_filename=row["filename"], sha256=sha256,
            status=status, track_id=None, submitted_source=submitted_source))
        return row

    def own(self, *names: str) -> dict[str, document_identity.Ownership]:
        return _run(document_identity.ownership_for_paths(list(names)))


class TestBasenameJoin(SeededStore):
    def setUp(self):
        super().setUp()
        self.nw = self.add(path=f"{NW_DIR}/q3-report.pdf", company=NW, sha256="sha-nw")
        self.cp = self.add(path=f"{CP_DIR}/q3-report.pdf", company=CP, sha256="sha-cp")

    def test_a_bare_filename_citation_resolves_to_the_absolute_row(self):
        # The exact shape from the production check: citation "q3-report.pdf" is ambiguous here because
        # two companies hold different bytes under the same name - see the class below for the unique
        # case. First prove the lookup reaches rows at all.
        ownership = self.own("q3-report.pdf")["q3-report.pdf"]
        self.assertTrue(ownership.rows, "the basename join must reach rows the exact-path join cannot")
        self.assertEqual({row["company"] for row in ownership.rows}, {NW, CP})

    def test_an_exact_path_still_resolves(self):
        ownership = self.own(f"{NW_DIR}/q3-report.pdf")[f"{NW_DIR}/q3-report.pdf"]
        self.assertEqual(ownership.via, "path")

    def test_no_row_means_no_company_and_no_invention(self):
        ownership = self.own("phantom.pdf")["phantom.pdf"]
        self.assertEqual(ownership.companies, set())
        self.assertFalse(ownership.ambiguous)
        self.assertEqual(ownership.via, "none")

    def test_empty_path_list_makes_no_query(self):
        self.assertEqual(_run(document_identity.ownership_for_paths([])), {})


class TestUniqueBasename(SeededStore):
    def setUp(self):
        super().setUp()
        self.nw = self.add(path=f"{NW_DIR}/omp-profile.pdf", company=NW, sha256="sha-nw")
        self.cp = self.add(path=f"{CP_DIR}/tariff-model.xlsx", company=CP, sha256="sha-cp")

    def test_a_unique_basename_is_attributed_to_its_only_owner(self):
        for name, company in (("omp-profile.pdf", NW), ("tariff-model.xlsx", CP)):
            with self.subTest(name=name):
                ownership = self.own(name)[name]
                self.assertEqual(ownership.companies, {company})
                self.assertFalse(ownership.ambiguous)

    def test_a_bare_name_matches_a_path_written_with_windows_separators(self):
        self.add(path="C:\\source_docs\\Contoso Pharma\\tariff-model.xlsx", company=CP,
                 filename="tariff-model.xlsx", sha256="sha-cp2", document_id="doc-win")
        ownership = self.own("tariff-model.xlsx")["tariff-model.xlsx"]
        self.assertEqual(ownership.companies, {CP})
        self.assertFalse(ownership.ambiguous, "same company, so two paths are not an ambiguity")


class TestSharedFileIdenticalBytes(SeededStore):
    """One document two companies: the dedupe rule indexes it once, both own it."""

    def setUp(self):
        super().setUp()
        self.add(path=f"{NW_DIR}/cutover-checklist.txt", company=NW, sha256="sha-shared",
                 document_id="doc-nw-shared")
        self.add(path=f"{CP_DIR}/cutover-checklist.txt", company=CP, sha256="sha-shared",
                 document_id="doc-cp-shared")

    def test_identical_bytes_under_one_name_are_owned_by_both_companies(self):
        ownership = self.own("cutover-checklist.txt")["cutover-checklist.txt"]
        self.assertFalse(ownership.ambiguous)
        self.assertEqual(ownership.companies, {NW, CP})

    def test_the_other_owner_is_reached_even_when_the_index_kept_one_path(self):
        # COMPANY_AWARE_DEDUPE on: only one row may be retrieved by name; the content-hash widening is
        # what makes the shared file visible to its other owner.
        self.add(path=f"{NW_DIR}/only-one-indexed.txt", company=NW, sha256="sha-shared2",
                 document_id="doc-a")
        self.add(path=f"{CP_DIR}/renamed-for-other-company.txt", company=CP, sha256="sha-shared2",
                 document_id="doc-b")
        ownership = self.own("only-one-indexed.txt")["only-one-indexed.txt"]
        self.assertEqual(ownership.companies, {NW, CP})


class TestAmbiguousBasename(SeededStore):
    def setUp(self):
        super().setUp()
        self.add(path=f"{NW_DIR}/transcript.txt", company=NW, sha256="sha-nw-bytes",
                 document_id="doc-nw")
        self.add(path=f"{CP_DIR}/transcript.txt", company=CP, sha256="sha-cp-bytes",
                 document_id="doc-cp")

    def test_same_name_different_bytes_in_different_companies_is_ambiguous(self):
        ownership = self.own("transcript.txt")["transcript.txt"]
        self.assertTrue(ownership.ambiguous)
        self.assertEqual(ownership.companies, set(),
                         "an ambiguous name is never credited to a company")

    def test_the_rows_are_still_reported_so_the_count_is_investigation_ready(self):
        ownership = self.own("transcript.txt")["transcript.txt"]
        self.assertEqual(len(ownership.rows), 2)
        self.assertEqual(ownership.content_hashes, ("sha-cp-bytes", "sha-nw-bytes"))


class TestSameNameSameCompany(SeededStore):
    """Two versions of one company's file: nothing about that is ambiguous."""

    def setUp(self):
        super().setUp()
        self.add(path=f"{NW_DIR}/transcript.txt", company=NW, sha256="sha-nw-new")
        self.add(path=f"{NW_DIR}/archive/transcript.txt", company=NW, sha256="sha-nw-old")

    def test_different_bytes_under_one_name_in_one_company_is_attributed(self):
        ownership = self.own("transcript.txt")["transcript.txt"]
        self.assertFalse(ownership.ambiguous)
        self.assertEqual(ownership.companies, {NW})



class TestSubmittedSourceJoin(SeededStore):
    def test_the_recorded_submitted_name_is_matched_first(self):
        self.add(path=f"{NW_DIR}/q3.pdf", company=NW, sha256="sha-nw",
                 submitted_source="northwind-chemicals--q3.pdf", document_id="doc-nw")
        self.add(path=f"{CP_DIR}/q3.pdf", company=CP, sha256="sha-cp",
                 submitted_source="contoso-pharma--q3.pdf", document_id="doc-cp")
        # A company-qualified submission makes the name LightRAG keeps unique: neither bare basename
        # collides, because each row is found by the name we actually sent.
        for name, company in (("northwind-chemicals--q3.pdf", NW), ("contoso-pharma--q3.pdf", CP)):
            with self.subTest(name=name):
                ownership = self.own(name)[name]
                self.assertEqual(ownership.via, "submitted_source")
                self.assertEqual(ownership.companies, {company})
                self.assertFalse(ownership.ambiguous)

    def test_a_bare_basename_still_works_for_rows_that_predate_the_column(self):
        self.add(path=f"{NW_DIR}/legacy.pdf", company=NW, sha256="sha-legacy")
        ownership = self.own("legacy.pdf")["legacy.pdf"]
        self.assertEqual(ownership.via, "filename")
        self.assertEqual(ownership.companies, {NW})


class TestJoinHygiene(SeededStore):
    def test_like_metacharacters_in_a_filename_do_not_over_match(self):
        self.add(path=f"{NW_DIR}/100%.pdf", company=NW, sha256="sha-a")
        self.add(path=f"{NW_DIR}/100x.pdf", company=CP, sha256="sha-b")
        ownership = self.own("100%.pdf")["100%.pdf"]
        self.assertEqual(ownership.companies, {NW})

    def test_the_store_row_for_a_citation_keeps_company_and_path(self):
        self.add(path=f"{NW_DIR}/plain.pdf", company=NW, sha256="sha-plain")
        rows = _run(state_store.get_documents_by_paths([f"{NW_DIR}/plain.pdf"]))
        self.assertEqual(rows[0]["company"], NW)
        self.assertIsNone(rows[0].get("submitted_source"),
                          "the migration leaves existing rows untouched")


class TestCitationAttributionThroughTheJoin(SeededStore):
    """The R0 symptom: 815 citations came back and `company` was set on none of them."""

    def test_a_bare_filename_citation_comes_back_with_a_company_and_a_document_id(self):
        self.add(path=f"{NW_DIR}/omp-profile.pdf", company=NW, sha256="sha-nw")
        included, scope = _run(api.scope_query_citations([{"file_path": "omp-profile.pdf"}],
                                                         requested_company=NW))
        self.assertEqual(len(included), 1, "the join reaches the row the path join could not")
        self.assertEqual(included[0]["company"], NW)
        self.assertEqual(included[0]["document_id"], self.rows[0]["document_id"])
        self.assertEqual(scope["excluded_citation_count"], 0)

    def test_an_exact_upload_door_path_still_joins(self):
        self.add(path=f"{settings.uploads_dir}/6f2a--q3.pdf", company=NW, sha256="sha-up",
                 source_type="upload")
        included, _scope = _run(api.scope_query_citations(
            [{"file_path": f"{settings.uploads_dir}/6f2a--q3.pdf"}], requested_company=NW))
        self.assertEqual([c["company"] for c in included], [NW])

    def test_an_ambiguous_basename_is_never_shown_and_is_counted_apart(self):
        self.add(path=f"{NW_DIR}/transcript.txt", company=NW, sha256="sha-nw")
        self.add(path=f"{CP_DIR}/transcript.txt", company=CP, sha256="sha-cp")
        included, scope = _run(api.scope_query_citations([{"file_path": "transcript.txt"}],
                                                         requested_company=NW))
        self.assertEqual(included, [], "no company may be credited for an undecided name")
        self.assertEqual(scope["ambiguous_citation_count"], 1)
        self.assertEqual(scope["excluded_citation_count"], 0,
                         "ambiguous is not the same fault as unowned")

    def test_a_shared_file_is_listed_under_both_owners_on_the_gl10_path(self):
        self.add(path=f"{NW_DIR}/cutover-checklist.txt", company=NW, sha256="sha-shared")
        self.add(path=f"{CP_DIR}/cutover-checklist.txt", company=CP, sha256="sha-shared")
        included, _scope = _run(api.scope_query_citations(
            [{"file_path": "cutover-checklist.txt"}], requested_company=None,
            allowed_companies=[NW]))
        self.assertEqual(len(included), 1)
        self.assertEqual(included[0]["company"], NW, "the row shown belongs to the company asked for")
        self.assertEqual(included[0]["companies"], sorted([NW, CP]))

    def test_the_older_single_company_envelope_is_unchanged(self):
        # `companies` is added only on the GL-10 (allowed_companies) path: the pre-GL-10 company
        # request must keep the exact envelope shape it has today.
        self.add(path=f"{NW_DIR}/cutover-checklist.txt", company=NW, sha256="sha-shared")
        self.add(path=f"{CP_DIR}/cutover-checklist.txt", company=CP, sha256="sha-shared")
        included, _scope = _run(api.scope_query_citations([{"file_path": "cutover-checklist.txt"}],
                                                          requested_company=NW))
        self.assertEqual(len(included), 1)
        self.assertNotIn("companies", included[0])


class TestMigrationIsNoOpForExistingRows(unittest.TestCase):
    def test_an_old_database_gains_the_column_without_losing_rows(self):
        tmp = tempfile.TemporaryDirectory(prefix="grg-join-mig-")
        self.addCleanup(tmp.cleanup)
        db = Path(tmp.name) / "state.db"
        saved = (state_store.settings.state_db_path, state_store.settings.source_docs_dir,
                 state_store.settings.uploads_dir)
        self.addCleanup(lambda: [setattr(state_store.settings, name, value) for name, value in
                                 zip(("state_db_path", "source_docs_dir", "uploads_dir"), saved)])
        state_store.settings.state_db_path = str(db)
        state_store.settings.source_docs_dir = str(Path(tmp.name) / "source_docs")
        state_store.settings.uploads_dir = str(Path(tmp.name) / "uploads")
        (Path(tmp.name) / "source_docs").mkdir()
        (Path(tmp.name) / "uploads").mkdir()

        import aiosqlite

        old_schema = state_store.SCHEMA.replace("    submitted_source TEXT,\n", "")
        old_migrations = dict(state_store.MIGRATIONS)
        old_migrations.pop("submitted_source")

        async def build_old_database() -> None:
            async with aiosqlite.connect(db) as conn:
                await conn.executescript(old_schema)
                await conn.execute(
                    "INSERT INTO documents (document_id, path, source_type, company, filename,"
                    " original_filename, sha256, status, created_at, updated_at)"
                    " VALUES ('old-1', '/app/source_docs/Solvay/doc.pdf', 'scan', 'Solvay',"
                    " 'doc.pdf', 'doc.pdf', 'sha-old', 'ingested', datetime('now'), datetime('now'))")
                await conn.commit()

        async def patch_migrations_and_reopen() -> None:
            original = state_store.MIGRATIONS
            state_store.MIGRATIONS = old_migrations
            try:
                await state_store.init_db()          # re-open without the new migration registered
            finally:
                state_store.MIGRATIONS = original

        _run(build_old_database())
        _run(patch_migrations_and_reopen())

        rows_before = _run(state_store.get_documents_by_paths(["/app/source_docs/Solvay/doc.pdf"]))
        self.assertEqual(len(rows_before), 1)
        _run(state_store.init_db())                  # now with the migration in place
        rows_after = _run(state_store.get_documents_by_paths(["/app/source_docs/Solvay/doc.pdf"]))
        self.assertEqual(len(rows_after), 1, "existing rows are unchanged")
        self.assertEqual(rows_after[0]["company"], "Solvay")
        self.assertIsNone(rows_after[0]["submitted_source"])
        ownership = _run(document_identity.ownership_for_paths(["doc.pdf"]))["doc.pdf"]
        self.assertEqual(ownership.companies, {"Solvay"}, "old rows work through the basename rule")


if __name__ == "__main__":
    unittest.main()
