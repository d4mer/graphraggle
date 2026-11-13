"""A skipped duplicate must reach a terminal status (GL-07/08 fix 2).

Why this exists
---------------
With ``COMPANY_AWARE_DEDUPE`` off - the production default - a second copy of the
same bytes is refused before any submit. The row was written ``accepted``, and
``accepted`` is the status the pipeline reads as "waiting to be submitted": the
worker only polls submitted tracks, so those rows never terminated. The GL-07/08
run on stack2 (2026-11-12) counted one such row in Northwind Chemicals and two in
Contoso Pharma. They are now written ``skipped``, and the row is also reachable as
the company that owns the file.

What the tests pin
------------------
- a duplicate marked at the scan door lands on ``skipped``, with ``duplicate_of``
  still in warnings_json, ``query_ready`` false and ``readiness_reason``
  ``duplicate_content``
- the upload door's other stuck shape - a row the door flagged
  ``duplicate_content`` and left at ``pending`` - is terminated too
- a row the OLD build left at ``accepted`` is promoted once and then stable: the
  next scan is the backfill, no operator step
- a settled skipped row is never re-submitted, retried or polled
- ``skipped`` is not an ACTIVE duplicate target: it cannot suppress a later copy
- a skipped row cannot become ``ingested`` directly, and can go back to work when
  the original disappears
- the company join still answers "which company has this file" for a skipped row
- a skipped row is not an ACTIVE superseding row

Fixtures are invented text in a temp directory; every upstream call is mocked.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.api as api
import app.state_store as state_store
import app.worker as worker


def _run(coro):
    return asyncio.run(coro)


def _row(path: str) -> dict | None:
    return _run(state_store.get_document_by_path(path))


def _duplicate_of(path: str) -> dict | None:
    return worker.parse_warning_metadata((_row(path) or {}).get("warnings_json")).get(
        "duplicate_of")


def _worker_meta(path: str) -> dict:
    meta = worker.parse_warning_metadata((_row(path) or {}).get("warnings_json")).get("worker")
    return meta if isinstance(meta, dict) else {}


class _Store(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-skipdup-")
        self.base = Path(self._tmp.name)
        self.src = self.base / "source_docs"
        self.uploads = self.base / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir,
                       settings.uploads_dir, settings.company_aware_dedupe)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.src)
        settings.uploads_dir = str(self.uploads)
        settings.company_aware_dedupe = False
        _run(state_store.init_db())
        self.client = AsyncMock()
        self.client.post_json = AsyncMock(return_value={"track_id": "track-1"})
        self.client.post_file = AsyncMock(return_value={"track_id": "track-2"})
        self.polls = AsyncMock()
        patches = [
            patch.object(worker, "client", self.client),
            patch.object(worker, "poll_track", self.polls),
        ]
        for p in patches:
            p.start()
        self._patches = patches

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        settings = state_store.settings
        (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir,
         settings.company_aware_dedupe) = self._saved
        self._tmp.cleanup()

    def write(self, relative: str, text: str) -> Path:
        path = self.src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def seed(self, path: str, status: str, *, document_id: str, sha: str,
             source_type: str = "filesystem", company: str | None = None, **overrides) -> None:
        row = {
            "source_type": source_type,
            "company": company,
            "filename": Path(path).name,
            "original_filename": Path(path).name,
            "sha256": sha,
            "validation_state": "accept",
            "query_ready": status == "ingested",
            "track_id": None,
        }
        row.update(overrides)
        _run(state_store.upsert_document(document_id=document_id, path=path,
                                         status=status, **row))

    def scan(self) -> None:
        _run(worker.scan_once())

    def submitted(self) -> list[str]:
        return [str(call.args[1]["file_source"])
                for call in self.client.post_json.await_args_list]


class TestDuplicateReachesTerminalStatus(_Store):
    BYTES = "invented quarterly note for the fake company\n"

    def setUp(self) -> None:
        super().setUp()
        self.original = self.write("Northwind Chemicals/quarterly.txt", self.BYTES)
        self.copy = self.write("Contoso Pharma/quarterly.txt", self.BYTES)
        self.sha = worker.sha256_of(self.original)
        self.assertEqual(worker.sha256_of(self.copy), self.sha)
        self.seed(str(self.original), "ingested", document_id="doc-orig", sha=self.sha,
                  company="Northwind Chemicals")

    def test_second_copy_is_skipped_not_accepted(self) -> None:
        self.scan()

        row = _row(str(self.copy))
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["error_code"], "duplicate_content")
        self.assertEqual(row["error_stage"], "validation")
        self.assertIsNone(row["track_id"])
        self.assertFalse(row["query_ready"])
        self.assertEqual(row["company"], "Contoso Pharma")
        # The duplicate evidence is unchanged: that is how the join finds the file.
        self.assertEqual(_duplicate_of(str(self.copy))["path"], str(self.original))
        self.assertEqual(_worker_meta(str(self.copy))["dedupe_action"],
                         "skip_duplicate_content")

    def test_readiness_and_counts_name_the_decision(self) -> None:
        self.scan()

        [annotated] = [doc for doc in _run(state_store.list_documents())
                       if doc["path"] == str(self.copy)]
        self.assertFalse(annotated["query_ready"])
        self.assertEqual(annotated["readiness_reason"], "duplicate_content")
        summary = _run(state_store.get_summary())
        self.assertEqual(summary["by_status"]["skipped"], 1)
        self.assertEqual(summary["by_readiness_reason"]["duplicate_content"], 1)
        self.assertEqual(summary["by_status"]["ingested"], 1)

    def test_a_settled_row_is_never_reswept_resubmitted_or_polled(self) -> None:
        self.scan()
        first = _row(str(self.copy))
        submits, polls = len(self.submitted()), self.polls.await_count

        self.scan()

        self.assertEqual(_row(str(self.copy)), first, "no write, no updated_at churn")
        self.assertEqual(len(self.submitted()), submits, "the copy is never submitted")
        self.assertEqual(self.polls.await_count, polls, "a row with no track is never polled")
        self.assertEqual(_row(str(self.copy))["retry_count"], 0)

    def test_orphaned_skipped_duplicate_goes_back_to_work(self) -> None:
        """The original stops being an original -> the copy is ingested instead."""
        self.scan()
        self.assertEqual(_row(str(self.copy))["status"], "skipped")
        # `ingested -> failed` is denied by the lifecycle guard, so use a real
        # terminal-not-active status: a row that no longer validates.
        _run(state_store.update_document_state(
            str(self.original), status="rejected", validation_state="reject",
            error_stage="validation", error_code="unsupported_encoding",
            query_ready=False))

        self.scan()

        row = _row(str(self.copy))
        self.assertIn(str(self.copy), self.submitted())
        self.assertEqual(row["status"], "submitted")
        self.assertIsNotNone(row["track_id"])
        self.assertIsNone(_duplicate_of(str(self.copy)))
        self.assertEqual(row["error_code"], None)

    def test_legacy_accepted_row_is_promoted_once_by_the_next_scan(self) -> None:
        """Backfill by behaviour: rows written by the old build need no operator step.

        The SQL note in docs/08-change-log.md is for an operator who wants the counts
        to move before the next scan; nothing in app/ runs it.
        """
        self.seed(str(self.copy), "accepted", document_id="doc-legacy", sha=self.sha,
                  company="Contoso Pharma", validation_state="warn",
                  error_stage="validation", error_code="duplicate_content",
                  error_message="Duplicate content already represented by another document",
                  warnings_json=json.dumps({
                      "duplicate_of": {"path": str(self.original), "company": "Northwind Chemicals"},
                      "worker": {"dedupe_action": "skip_duplicate_content"},
                  }, sort_keys=True))

        self.scan()
        promoted = _row(str(self.copy))
        self.assertEqual(promoted["status"], "skipped")

        self.scan()
        self.assertEqual(_row(str(self.copy)), promoted, "promoted once, then stable")


class TestUploadDoorDuplicateRow(_Store):
    """The other non-terminal shape: /upload flags it, the worker never submits it."""

    def test_pending_duplicate_from_the_door_is_terminated(self) -> None:
        staged = self.uploads / "2222-quarterly.txt"
        staged.write_text("invented shared note\n", encoding="utf-8")
        sha = worker.sha256_of(staged)
        original = self.write("Northwind Chemicals/quarterly.txt", "invented shared note\n")
        self.seed(str(original), "ingested", document_id="doc-orig", sha=sha,
                  company="Northwind Chemicals")
        # Exactly what app/api.py /upload writes for a duplicate.
        self.seed(str(staged), "pending", document_id="doc-up", sha=sha,
                  source_type="upload", company="Contoso Pharma",
                  validation_state="warn", error_stage="validation",
                  error_code="duplicate_content",
                  warnings_json=json.dumps(
                      {"duplicate_of": {"path": str(original), "company": "Northwind Chemicals"}},
                      sort_keys=True))

        self.scan()

        row = _row(str(staged))
        self.assertEqual(row["status"], "skipped")
        self.assertNotIn(str(staged), self.submitted())


class TestSkippedIsNotActive(_Store):
    def test_a_skipped_row_never_suppresses_another_copy(self) -> None:
        skipped = {
            "path": "/x/companyA/one.txt", "sha256": "sha-x", "status": "skipped",
            "company": "companyA", "error_code": "duplicate_content",
            "updated_at": "2026-01-01 00:00:00", "document_id": "d1",
        }
        self.assertIsNone(worker.pick_duplicate_candidate([skipped], "/x/companyB/two.txt"))
        self.assertNotIn("skipped", worker.ACTIVE_DUPLICATE_STATUSES)
        self.assertNotIn("skipped", state_store.ACTIVE_SUPERSEDING_STATUSES)

    def test_a_skipped_row_does_not_supersede_the_row_it_names(self) -> None:
        annotated = state_store.annotate_documents([
            {"document_id": "d-target", "path": "/x/a.txt", "status": "ingested",
             "validation_state": "accept"},
            {"document_id": "d-skipped", "path": "/x/b.txt", "status": "skipped",
             "validation_state": "warn", "supersedes_document_id": "d-target"},
        ])
        by_id = {doc["document_id"]: doc for doc in annotated}
        self.assertEqual(by_id["d-target"]["readiness_reason"], "ingested")
        self.assertTrue(by_id["d-target"]["query_ready"])


class TestCompanyJoinStillWorks(_Store):
    """The gateway join is by path: `skipped` must not cost the company its file."""

    def test_a_citation_path_resolves_to_the_skipped_rows_company(self) -> None:
        path = self.write("Contoso Pharma/shared-checklist.txt", "invented checklist\n")
        sha = worker.sha256_of(path)
        original = self.write("Northwind Chemicals/shared-checklist.txt", "invented checklist\n")
        self.seed(str(original), "ingested", document_id="doc-orig", sha=sha,
                  company="Northwind Chemicals")
        self.seed(str(path), "skipped", document_id="doc-copy", sha=sha,
                  company="Contoso Pharma", validation_state="warn",
                  error_stage="validation", error_code="duplicate_content",
                  warnings_json=json.dumps({
                      "duplicate_of": {"path": str(original), "company": "Northwind Chemicals",
                                       "match_paths": [str(original)]},
                  }, sort_keys=True))

        included, scope = _run(api.scope_query_citations(
            [{"file_path": str(path)}], requested_company="Contoso Pharma"))

        self.assertEqual(len(included), 1, "Contoso still owns this path")
        self.assertEqual(included[0]["company"], "Contoso Pharma")
        self.assertEqual(included[0]["document_id"], "doc-copy")
        self.assertFalse(included[0]["query_ready"])
        self.assertEqual(included[0]["readiness_reason"], "duplicate_content")
        self.assertEqual(scope["included_citation_count"], 1)


class TestLifecycleEdges(unittest.TestCase):
    def test_every_status_can_be_written_skipped(self) -> None:
        for source in state_store.STATUSES:
            with self.subTest(source=source):
                self.assertTrue(state_store.is_legal_transition(source, "skipped"))

    def test_skipped_must_be_revalidated_before_it_can_be_ingested(self) -> None:
        self.assertFalse(state_store.is_legal_transition("skipped", "ingested"))
        self.assertFalse(state_store.is_legal_transition("skipped", "processing"))
        for target in ("pending", "validating", "accepted", "submitted", "skipped"):
            with self.subTest(target=target):
                self.assertTrue(state_store.is_legal_transition("skipped", target))

    def test_unknown_status_rows_are_still_allowed_out(self) -> None:
        self.assertTrue(state_store.is_legal_transition("quarantined", "skipped"))

    def test_a_real_skipped_row_can_return_to_pending_for_a_reindex(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="grg-skip-edge-")
        settings = state_store.settings
        saved = settings.state_db_path
        settings.state_db_path = str(Path(tmp.name) / "state.db")
        try:
            _run(state_store.init_db())
            _run(state_store.upsert_document(
                document_id="d1", path="/x/a.txt", source_type="filesystem",
                company=None, filename="a.txt", original_filename="a.txt",
                sha256="sha-a", status="skipped", track_id=None,
                validation_state="warn", error_code="duplicate_content"))
            self.assertTrue(_run(state_store.update_document_state("/x/a.txt",
                                                                   status="pending")))
            self.assertEqual(_row("/x/a.txt")["status"], "pending")
        finally:
            settings.state_db_path = saved
            tmp.cleanup()

    def test_derive_readiness_for_a_skipped_row_without_a_duplicate_code(self) -> None:
        ready, reason = state_store.derive_readiness(
            {"status": "skipped", "validation_state": "warn",
             "error_code": worker.SKIP_RULE_ERROR_CODE, "document_id": "d1"})
        self.assertFalse(ready)
        self.assertEqual(reason, "skipped")


if __name__ == "__main__":
    unittest.main()
