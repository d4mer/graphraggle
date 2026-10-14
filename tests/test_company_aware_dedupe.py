"""Company-aware duplicate selection (COMPANY_AWARE_DEDUPE) and the poll guard.

Two changes in app/, both dark behind settings.company_aware_dedupe (default
false, so the shipped default is byte-identical to the old company-blind picker):

- app/worker.pick_duplicate_candidate now takes the NEW document's company. When
  the flag is on, a candidate may suppress a new document only if that document
  is unscoped (company NULL) or the candidate is in the same company. Comparison
  is normalize_company() == normalize_company() - strip only, no case folding,
  mirroring scope_query_citations in api.py.
- When the flag released a document past another company's SHA match, the row is
  marked, and poll_track accepts PROCESSED as ingested only if a returned
  documents[].file_path matches the path we submitted. Marked rows that fail that
  check land on error_code=cross_company_not_indexed instead of a false ingested.
  Unmarked rows take exactly the path they take today.

REPRODUCTION tests (named test_reproduction_*) fail on the pre-change code; the
rest pin behaviour that must not change.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.state_store as state_store
import app.worker as worker


def _run(coro):
    return asyncio.run(coro)


def _row(path: str) -> dict | None:
    return _run(state_store.get_document_by_path(path))


def _worker_meta(path: str) -> dict:
    row = _row(path) or {}
    return (json.loads(row.get("warnings_json") or "{}")).get("worker") or {}


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-cadedupe-")
        self.base = Path(self._tmp.name)
        self.src = self.base / "source_docs"
        self.uploads = self.base / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        settings = state_store.settings
        self._saved = (
            settings.state_db_path,
            settings.source_docs_dir,
            settings.uploads_dir,
            settings.company_aware_dedupe,
        )
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.src)
        settings.uploads_dir = str(self.uploads)
        settings.company_aware_dedupe = False
        _run(state_store.init_db())

    def tearDown(self) -> None:
        settings = state_store.settings
        (
            settings.state_db_path,
            settings.source_docs_dir,
            settings.uploads_dir,
            settings.company_aware_dedupe,
        ) = self._saved
        self._tmp.cleanup()

    def flag(self, on: bool) -> None:
        state_store.settings.company_aware_dedupe = on

    def seed(self, path: str, *, document_id: str, company, status: str, sha: str,
             source_type: str = "filesystem", **overrides) -> None:
        row = {
            "filename": Path(path).name,
            "original_filename": Path(path).name,
            "validation_state": "accept",
            "query_ready": status == "ingested",
            "track_id": None,
        }
        row.update(overrides)
        _run(state_store.upsert_document(
            document_id=document_id, path=str(path), source_type=source_type,
            company=company, company_source="path" if company else "unscoped",
            company_source_detail=None, content_hash=sha, sha256=sha, status=status,
            ingested_at=None, **row,
        ))

    @contextlib.contextmanager
    def mocked_client(self, *, post_json=None, post_file=None, get=None):
        """No network in tests: every upstream call is a mock we can assert on."""
        post_json = post_json or AsyncMock(return_value={"track_id": "trk-1"})
        post_file = post_file or AsyncMock(return_value={"track_id": "trk-1"})
        get = get or AsyncMock(return_value={})
        with patch.object(worker.client, "post_json", post_json), \
             patch.object(worker.client, "post_file", post_file), \
             patch.object(worker.client, "get", get):
            yield SimpleNamespace(post_json=post_json, post_file=post_file, get=get)


class TestPickerCompanyScoping(_Base):
    """The rule itself: who may suppress whom."""

    CAND_PATH = "/data/source_docs/companyA/report.txt"

    def candidate(self, company) -> dict:
        return {
            "document_id": "docA", "path": self.CAND_PATH, "company": company,
            "status": "ingested", "updated_at": "2026-10-01 00:00:00",
            "original_filename": "report.txt", "validation_state": "accept",
            "error_code": None,
        }

    def test_flag_off_ignores_company_entirely(self) -> None:
        docs = [self.candidate("companyA")]
        blind = worker.pick_duplicate_candidate(docs, "/data/uploads/b--report.txt")
        scoped = worker.pick_duplicate_candidate(
            docs, "/data/uploads/b--report.txt", company="companyB")
        self.assertIsNotNone(scoped)
        self.assertEqual(scoped, blind, "flag off must not change a single field")

    def test_reproduction_scoped_new_doc_is_not_suppressed_by_another_company(self) -> None:
        self.flag(True)
        self.assertIsNone(worker.pick_duplicate_candidate(
            [self.candidate("companyA")], "/data/uploads/b--report.txt", company="companyB"))

    def test_same_company_still_suppressed(self) -> None:
        self.flag(True)
        picked = worker.pick_duplicate_candidate(
            [self.candidate("companyA")], "/data/uploads/a--report.txt", company="companyA")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["document_id"], "docA")

    def test_unscoped_new_doc_still_suppressed(self) -> None:
        self.flag(True)
        picked = worker.pick_duplicate_candidate(
            [self.candidate("companyA")], "/data/uploads/x--report.txt", company=None)
        self.assertIsNotNone(picked)

    def test_scoped_new_doc_not_suppressed_by_unscoped_candidate(self) -> None:
        self.flag(True)
        self.assertIsNone(worker.pick_duplicate_candidate(
            [self.candidate(None)], "/data/uploads/b--report.txt", company="companyB"))

    def test_whitespace_variants_agree(self) -> None:
        self.flag(True)
        picked = worker.pick_duplicate_candidate(
            [self.candidate("  companyB  ")], "/data/uploads/b--report.txt", company="companyB")
        self.assertIsNotNone(picked, "normalize_company strips, so these are one company")

    def test_case_variants_do_not_agree(self) -> None:
        # Pinned deliberately: query scoping compares the raw stored company with
        # ==, so folding case on one side only would submit documents whose
        # citations are then filtered out. Change both sides or neither.
        self.flag(True)
        self.assertIsNone(worker.pick_duplicate_candidate(
            [self.candidate("CompanyB")], "/data/uploads/b--report.txt", company="companyB"))


class TestWorkerDoor(_Base):
    """What the worker does with a cross-company SHA match, per flag state."""

    def setUp(self) -> None:
        super().setUp()
        self.a_path = self.src / "companyA" / "report.txt"
        self.a_path.parent.mkdir(parents=True)
        self.a_path.write_text("identical bytes\n")
        self.b_path = self.uploads / "bbbb--report.txt"
        self.b_path.write_text("identical bytes\n")
        # The real digest: the worker looks candidates up by the hash it computes
        # from the file on disk, so a fake hash would match nothing.
        self.sha = worker.sha256_of(self.a_path)
        self.assertEqual(self.sha, worker.sha256_of(self.b_path))
        self.seed(str(self.a_path), document_id="docA", company="companyA",
                  status="ingested", sha=self.sha)
        self.seed(str(self.b_path), document_id="docB", company="companyB",
                  status="pending", sha=self.sha, source_type="upload")

    def test_flag_off_worker_still_marks_it_duplicate(self) -> None:
        with self.mocked_client() as up:
            _run(worker.handle_candidate(self.b_path, "upload", self.base))
        row = _row(str(self.b_path))
        self.assertEqual(row["status"], "accepted")
        self.assertEqual(row["error_code"], "duplicate_content")
        self.assertFalse(row["query_ready"])
        self.assertEqual(up.post_json.await_count, 0)
        self.assertNotIn("cross_company_guard", _worker_meta(str(self.b_path)))

    def test_reproduction_worker_submits_cross_company_duplicate_when_flag_on(self) -> None:
        self.flag(True)
        with self.mocked_client() as up:
            _run(worker.handle_candidate(self.b_path, "upload", self.base))
        row = _row(str(self.b_path))
        self.assertEqual(up.post_json.await_count, 1, "the document must reach upstream")
        self.assertEqual(row["status"], "submitted")
        self.assertIsNone(row["error_code"])
        self.assertEqual(up.post_json.await_args.args[1]["file_source"], str(self.b_path))

    def test_marker_recorded_for_released_documents(self) -> None:
        self.flag(True)
        with self.mocked_client():
            _run(worker.handle_candidate(self.b_path, "upload", self.base))
        meta = _worker_meta(str(self.b_path))
        self.assertEqual(meta.get("cross_company_guard"), "verify_file_path_on_process")
        self.assertEqual(meta.get("cross_company_duplicate_path"), str(self.a_path))
        self.assertEqual(meta.get("cross_company_duplicate_company"), "companyA")

    def test_no_marker_for_ordinary_submission(self) -> None:
        self.flag(True)
        other = self.uploads / "cccc--unique.txt"
        other.write_text("nobody else has these bytes\n")
        self.seed(str(other), document_id="docC", company="companyC",
                  status="pending", sha="sha-unique", source_type="upload")
        with self.mocked_client():
            _run(worker.handle_candidate(other, "upload", self.base))
        self.assertEqual(_row(str(other))["status"], "submitted")
        self.assertNotIn("cross_company_guard", _worker_meta(str(other)))

    def test_same_company_duplicate_is_still_skipped_when_flag_on(self) -> None:
        self.flag(True)
        twin = self.uploads / "aaaa2--report.txt"
        twin.write_text("identical bytes\n")
        self.seed(str(twin), document_id="docA2", company="companyA",
                  status="pending", sha=self.sha, source_type="upload")
        with self.mocked_client() as up:
            _run(worker.handle_candidate(twin, "upload", self.base))
        self.assertEqual(_row(str(twin))["error_code"], "duplicate_content")
        self.assertEqual(up.post_json.await_count, 0)


class TestPollTrackGuard(_Base):
    """The guard: PROCESSED must carry our path, for marked rows only."""

    def setUp(self) -> None:
        super().setUp()
        self.path = self.uploads / "bbbb--report.txt"
        self.path.write_text("identical bytes\n")
        self.seed(str(self.path), document_id="docB", company="companyB",
                  status="submitted", sha=worker.sha256_of(self.path), source_type="upload",
                  track_id="trk-1")
        self.other = "/data/source_docs/companyA/report.txt"

    def mark(self) -> None:
        warnings_json = worker.merge_warning_metadata(
            None,
            worker_updates=worker.cross_company_guard_updates(
                {"path": self.other, "company": "companyA"}),
        )
        _run(state_store.update_document_state(str(self.path), warnings_json=warnings_json))

    def poll(self, file_paths: list[str]) -> None:
        payload = {"track_id": "trk-1", "documents": [
            {"id": f"d{i}", "status": "processed", "file_path": p}
            for i, p in enumerate(file_paths)
        ], "total_count": len(file_paths), "status_summary": {"processed": len(file_paths)}}
        with self.mocked_client(get=AsyncMock(return_value=payload)):
            _run(worker.poll_track(_row(str(self.path))))

    def test_reproduction_marker_and_no_matching_path_fails_visibly(self) -> None:
        self.mark()
        self.poll([self.other])
        row = _row(str(self.path))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "cross_company_not_indexed")
        self.assertFalse(row["query_ready"])
        detail = json.loads(row["error_detail"])
        self.assertEqual(detail["expected_path"], str(self.path))
        self.assertIn(self.other, detail["returned_paths"])
        self.assertEqual(_worker_meta(str(self.path)).get("cross_company_guard"), "path_mismatch")

    def test_marker_with_matching_path_ingests(self) -> None:
        self.mark()
        self.poll([self.other, str(self.path)])
        row = _row(str(self.path))
        self.assertEqual(row["status"], "ingested")
        self.assertTrue(row["query_ready"])
        self.assertNotIn("cross_company_guard", _worker_meta(str(self.path)))

    def test_marker_matching_by_basename_ingests(self) -> None:
        # /documents/upload tags the uploaded filename, not the path we handed it.
        self.mark()
        self.poll([self.other, self.path.name])
        self.assertEqual(_row(str(self.path))["status"], "ingested")

    def test_unmarked_row_takes_todays_path_even_without_our_path(self) -> None:
        self.poll([self.other])
        row = _row(str(self.path))
        self.assertEqual(row["status"], "ingested")
        self.assertTrue(row["query_ready"])

    def test_flag_off_leaves_no_marker_so_the_guard_never_runs(self) -> None:
        self.flag(False)
        self.assertIn("cross_company_guard", worker.cross_company_guard_updates(None))
        self.assertIsNone(worker.cross_company_guard_mismatch(
            json.dumps({"worker": {}}), str(self.path), [{"file_path": self.other}]))


if __name__ == "__main__":
    unittest.main()
