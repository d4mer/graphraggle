"""Lifecycle cleanup (deferred items from the TODO #2 review).

Covers the two behaviour changes in app/:
- app/worker.py submit_file: the failure write-backs after the LightRAG submit
  are compare-and-set on the status this scan wrote ("accepted"), so an operator
  reindex that lands while the submit is in flight is not overwritten by our
  stale snapshot; the successful upsert is deliberately NOT guarded (a dropped
  write would strand a live track with no row pointing at it).
- app/state_store.increment_retry: optional expect_track_id / expect_status, so
  a retry is never counted against a row that has since moved to a different
  submission.

The REPRODUCTION tests fail on the pre-change code; the others pin behaviour
that must not change.
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

import app.state_store as state_store
import app.worker as worker


def _run(coro):
    return asyncio.run(coro)


def _row(path: str) -> dict | None:
    return _run(state_store.get_document_by_path(path))


class _Cleanup(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-cleanup-")
        self.base = Path(self._tmp.name)
        self.src = self.base / "source_docs"
        self.src.mkdir()
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.src)
        settings.uploads_dir = str(self.base / "uploads")
        _run(state_store.init_db())

    def tearDown(self) -> None:
        settings = state_store.settings
        (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir) = self._saved
        self._tmp.cleanup()

    def seed(self, path: str, status: str, retry_count: int | None = None, **overrides) -> None:
        document_id = overrides.pop("document_id", "doc-1")
        row = {
            "source_type": "filesystem",
            "company": None,
            "filename": Path(path).name,
            "original_filename": Path(path).name,
            "sha256": "sha-a",
            "validation_state": "accept",
            "query_ready": status == "ingested",
            "track_id": None,
        }
        row.update(overrides)
        _run(
            state_store.upsert_document(
                document_id=document_id,
                path=path,
                status=status,
                **row,
            )
        )
        if retry_count is not None:
            _run(state_store.update_document_state(path, retry_count=retry_count))


class ConnectError(Exception):
    """Named like httpx.ConnectError, which is what is_transient_exception keys on."""


def _failing_client() -> AsyncMock:
    client = AsyncMock()
    client.post_json = AsyncMock(side_effect=ConnectError("connection refused"))
    client.post_file = AsyncMock(side_effect=ConnectError("connection refused"))
    return client


# ── item 2: increment_retry must not count a retry against a moved row ─────

class TestIncrementRetryGuard(_Cleanup):
    PATH = "/docs/a.txt"

    def test_reproduction_retry_is_not_counted_against_a_row_that_moved(self) -> None:
        """REPRODUCTION: the worker's retry decision comes from a snapshot. If a
        newer submission replaced the track since, the retry must not be burned."""
        self.seed(self.PATH, "failed", track_id="track-old", error_stage="track_poll",
                  error_code="track_status_transient", retry_count=1)

        # The row moves on while the scan that read it is still working.
        _run(state_store.update_document_state(self.PATH, status="submitted", track_id="track-new"))

        with self.assertLogs("gateway.state_store", level="WARNING") as logs:
            count = _run(state_store.increment_retry(
                self.PATH, expect_track_id="track-old", expect_status="failed"
            ))

        self.assertEqual(count, 1)
        self.assertEqual(_row(self.PATH)["retry_count"], 1)
        self.assertEqual(_row(self.PATH)["track_id"], "track-new")
        self.assertTrue(any("retry not counted" in line for line in logs.output), logs.output)

    def test_retry_still_counts_when_the_row_matches(self) -> None:
        self.seed(self.PATH, "failed", track_id=None, error_stage="submit",
                  error_code="submit_transient_failed", retry_count=1)
        count = _run(state_store.increment_retry(
            self.PATH, expect_track_id=None, expect_status="failed"
        ))
        self.assertEqual(count, 2)
        self.assertEqual(_row(self.PATH)["retry_count"], 2)

    def test_uncalled_signature_still_counts(self) -> None:
        """Back-compat: no expectations means the old unconditional behaviour."""
        self.seed(self.PATH, "failed", retry_count=0)
        self.assertEqual(_run(state_store.increment_retry(self.PATH)), 1)


# ── item 1: submit_file's post-submit write-backs ──────────────────────────

class TestSubmitWriteBacks(_Cleanup):
    def setUp(self) -> None:
        super().setUp()
        self.file = self.src / "report.txt"
        self.file.write_text("content the worker can submit\n", encoding="utf-8")
        self.digest = worker.sha256_of(self.file)

    def test_reproduction_submit_failure_does_not_clobber_a_later_reindex(self) -> None:
        """REPRODUCTION: /ingest/reindex lands while the submit is in flight. The
        failure verdict describes OUR attempt; the operator's newer state wins."""
        self.seed(str(self.file), "accepted", sha256=self.digest, content_hash=self.digest)
        # The API's reindex write (api.py:1695) as it would land mid-submit.
        reindex_warnings = json.dumps(
            {"worker": {"reindex_requested": True, "requested_from_status": "ingested"}},
            sort_keys=True,
        )
        _run(state_store.update_document_state(
            str(self.file),
            status="pending",
            track_id=None,
            error_stage="reindex",
            error_code="reindex_requested",
            warnings_json=reindex_warnings,
        ))

        client = _failing_client()
        with patch.object(worker, "client", client):
            _run(worker.submit_file(self.file, "filesystem", self.digest))

        row = _row(str(self.file))
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["error_code"], "reindex_requested")
        self.assertIn("reindex_requested", row["warnings_json"])

    def test_submit_failure_still_lands_on_the_row_we_read(self) -> None:
        """Parity guard: an ordinary failed submit behaves exactly as before."""
        self.seed(str(self.file), "accepted", sha256=self.digest, content_hash=self.digest)

        client = _failing_client()
        with patch.object(worker, "client", client):
            _run(worker.submit_file(self.file, "filesystem", self.digest))

        row = _row(str(self.file))
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "submit_transient_failed")
        self.assertEqual(row["error_stage"], "submit")
        self.assertIsNone(row["track_id"])

    def test_successful_submit_lands_even_if_the_row_was_moved_mid_flight(self) -> None:
        """Why the successful upsert carries no CAS: the row has to learn about
        the live track, or the submission can never be polled or ingested."""
        self.seed(str(self.file), "accepted", sha256=self.digest, content_hash=self.digest)
        _run(state_store.update_document_state(str(self.file), status="pending", track_id=None))

        client = AsyncMock()
        client.post_json = AsyncMock(return_value={"track_id": "track-new"})
        with patch.object(worker, "client", client):
            _run(worker.submit_file(self.file, "filesystem", self.digest))

        row = _row(str(self.file))
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["track_id"], "track-new")
        self.assertEqual(row["retry_count"], 0)


# ── item 4: superseded is read-derived, never written ──────────────────────

class TestSupersededIsDerived(_Cleanup):
    def test_supersession_is_derived_at_read_time(self) -> None:
        original = self.src / "companyA/a.txt"
        successor = self.src / "companyA/a-v2.txt"
        self.seed(str(original), "ingested", document_id="doc-old", sha256="sha-old")
        self.seed(str(successor), "ingested", document_id="doc-new", sha256="sha-new",
                  supersedes_document_id="doc-old")

        by_id = {doc["document_id"]: doc for doc in _run(state_store.list_documents())}
        self.assertEqual(by_id["doc-old"]["status"], "ingested")  # never rewritten
        self.assertEqual(by_id["doc-old"]["readiness_reason"], "superseded")
        self.assertFalse(by_id["doc-old"]["query_ready"])
        self.assertEqual(by_id["doc-new"]["readiness_reason"], "ingested")
        self.assertEqual(state_store.READ_DERIVED_STATUSES, frozenset({"superseded"}))


if __name__ == "__main__":
    unittest.main()
