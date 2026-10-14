"""Upload door vs filesystem-scan door: do the two paths treat the same file the
same way? (TODO near-term #3)

Written as reproductions first: the tests marked REPRODUCTION fail on the code
they were written against, and the rest pin the behaviour that must not change.

Covers the brief's items:
1. dedupe - the upload door used `get_document_by_sha256` (any status, newest
   row), the worker used `pick_duplicate_candidate` (active statuses only, never
   a `duplicate_content` row). One shared selection now.
2. skip rules - /upload stages the stream as `.<document_id>.part` inside
   UPLOADS_DIR; a scan landing in that window wrote a `rejected` row for a path
   that then vanished on rename.
3. validation entry points / auto_split - checked by
   TestDoorAndWorkerVerdictsAgree (no code change: the door re-validates with
   the measured size, and auto_split lands the same way through either door).
4. company attribution - documented in the hand-back, no code change (ADR 0004).

Everything runs offline: no LightRAG call is allowed (the worker client is a
mock that raises if used), rows live in a throwaway SQLite file.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiosqlite

import app.api as api
import app.state_store as state_store
import app.validation as validation
import app.worker as worker


def _run(coro):
    return asyncio.run(coro)


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class _UploadFile:
    """Stand-in for fastapi.UploadFile: filename, size, read(n)."""

    def __init__(self, filename: str, payload: bytes) -> None:
        self.filename = filename
        self.size = len(payload)
        self._buf = io.BytesIO(payload)

    async def read(self, n: int = -1) -> bytes:
        return self._buf.read(n)


class _TwoDoors(unittest.TestCase):
    """Fresh temp DB + separate source_docs/uploads dirs per test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-doors-")
        self.base = Path(self._tmp.name)
        self.src = self.base / "source_docs"
        self.uploads = self.base / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.src)
        settings.uploads_dir = str(self.uploads)
        _run(state_store.init_db())

    def tearDown(self) -> None:
        settings = state_store.settings
        (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir) = self._saved
        self._tmp.cleanup()

    def seed(self, *, document_id: str, path: str, status: str, sha256: str, **overrides) -> None:
        row = {
            "source_type": "filesystem",
            "company": None,
            "filename": Path(path).name,
            "original_filename": Path(path).name,
            "validation_state": "accept",
            "query_ready": status == "ingested",
            "track_id": None,
        }
        row.update(overrides)
        _run(
            state_store.upsert_document(
                document_id=document_id,
                path=path,
                source_type=row.pop("source_type"),
                company=row.pop("company"),
                filename=row.pop("filename"),
                original_filename=row.pop("original_filename"),
                sha256=sha256,
                status=status,
                track_id=row.pop("track_id"),
                validation_state=row.pop("validation_state"),
                query_ready=row.pop("query_ready"),
                **row,
            )
        )

    def set_updated_at(self, path: str, value: str) -> None:
        """Make the `updated_at DESC` ordering in get_document_by_sha256 explicit."""

        async def _do() -> None:
            async with aiosqlite.connect(state_store.settings.state_db_path) as db:
                await db.execute("UPDATE documents SET updated_at=? WHERE path=?", (value, path))
                await db.commit()

        _run(_do())

    def upload(self, filename: str, payload: bytes, company: str | None = None):
        return self.upload_file(_UploadFile(filename, payload), company)

    def upload_file(self, upload, company: str | None = None):
        # No LightRAG call is permitted from these tests.
        client = AsyncMock()
        client.post_json = AsyncMock(side_effect=AssertionError("no network in tests"))
        with patch.object(worker, "client", client):
            return _run(api.upload(file=upload, company=company))

    def rows_by_path(self) -> dict[str, dict]:
        return {row["path"]: row for row in _run(state_store._fetch_documents("SELECT * FROM documents"))}


# ── item 1: one dedupe implementation ───────────────────────────────────────

class TestUploadDedupeUsesWorkerSelection(_TwoDoors):
    PAYLOAD = b"the very same bytes through either door\n"

    def test_reproduction_upload_is_not_a_duplicate_of_a_rejected_row(self) -> None:
        """REPRODUCTION: the worker would never call a rejected row the original."""
        digest = _digest(self.PAYLOAD)
        self.seed(document_id="doc-rejected", path="rejected/gone.txt", status="rejected",
                  sha256=digest, validation_state="reject", error_stage="validation",
                  error_code="empty_file")

        envelope = self.upload("fresh.txt", self.PAYLOAD)

        self.assertIsNone(envelope.data["duplicate_of"])
        new_row = [row for path, row in self.rows_by_path().items()
                   if not path.startswith("rejected/")][0]
        self.assertIsNone(new_row["warnings_json"])
        self.assertEqual(new_row["status"], "pending")
        self.assertEqual(new_row["validation_state"], "accept")

    def test_reproduction_upload_is_not_a_duplicate_of_a_duplicate_row(self) -> None:
        """REPRODUCTION: two rows share the sha; the newest is itself flagged
        `duplicate_content`, the older one is the ingested original. The upload
        must point at the ingested one, the way pick_duplicate_candidate ranks."""
        digest = _digest(self.PAYLOAD)
        self.seed(document_id="doc-original", path=str(self.src / "companyA/report.txt"),
                  status="ingested", sha256=digest)
        self.set_updated_at(str(self.src / "companyA/report.txt"), "2020-01-01 00:00:00")
        self.seed(document_id="doc-duplicate", path=str(self.src / "companyB/report.txt"),
                  status="accepted", sha256=digest, error_stage="validation",
                  error_code="duplicate_content")
        self.set_updated_at(str(self.src / "companyB/report.txt"), "2030-01-01 00:00:00")

        envelope = self.upload("another.txt", self.PAYLOAD)

        duplicate_of = envelope.data["duplicate_of"]
        self.assertIsNotNone(duplicate_of)
        self.assertEqual(duplicate_of["document_id"], "doc-original")
        self.assertEqual(duplicate_of["path"], str(self.src / "companyA/report.txt"))

    def test_active_duplicate_is_still_flagged_at_the_door(self) -> None:
        """Parity guard: a real active original is still reported to the caller."""
        digest = _digest(self.PAYLOAD)
        self.seed(document_id="doc-original", path=str(self.src / "companyA/report.txt"),
                  status="ingested", sha256=digest)

        envelope = self.upload("another.txt", self.PAYLOAD)

        self.assertEqual(envelope.data["duplicate_of"]["document_id"], "doc-original")
        self.assertEqual(envelope.data["validation_state"], "warn")
        new_row = self.rows_by_path()[envelope.data["path"]]
        self.assertEqual(new_row["error_code"], "duplicate_content")
        self.assertIn("duplicate_of", new_row["warnings_json"])


# ── item 2: the in-flight upload temp file must not be scanned ─────────────

class TestScanIgnoresStagedUploads(_TwoDoors):
    def test_reproduction_mid_stream_upload_part_file_creates_no_row(self) -> None:
        """REPRODUCTION: /upload writes UPLOADS_DIR/.<document_id>.part and only
        renames it at the end; a scan in that window used to write a `rejected`
        row for a path that then disappears."""
        document_id = "6f1a2b3c-0000-4000-8000-000000000001"
        staged = self.uploads / f".{document_id}.part"
        staged.write_bytes(b"half a document, still streaming")

        client = AsyncMock()
        client.get = AsyncMock(side_effect=AssertionError("no network in tests"))
        client.post_json = AsyncMock(side_effect=AssertionError("no network in tests"))
        client.post_file = AsyncMock(side_effect=AssertionError("no network in tests"))
        with patch.object(worker, "client", client):
            _run(worker.scan_once())

        self.assertEqual(self.rows_by_path(), {})

    def test_real_upload_row_is_still_scanned(self) -> None:
        """Parity guard: the skip rule must not hide a finished upload."""
        payload = b"a finished upload, ready to ingest\n"
        (self.uploads / "doc-1--report.txt").write_bytes(payload)

        client = AsyncMock()
        client.post_file = AsyncMock(return_value={"track_id": "track-1"})
        client.post_json = AsyncMock(return_value={"track_id": "track-1"})
        with patch.object(worker, "client", client):
            _run(worker.scan_once())

        row = self.rows_by_path()[str(self.uploads / "doc-1--report.txt")]
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["source_type"], "upload")


# ── item 3: accepted at the door, rejected by the worker ───────────────────

class TestDoorAndWorkerVerdictsAgree(_TwoDoors):
    """Item 3: the door validates filename+size, the worker validates content.

    What the code actually does, pinned here as evidence: /upload re-runs
    validate_upload with the *measured* size after the stream is written, so the
    size-shaped verdicts (empty_file, large_text_file/auto_split) cannot differ
    between the doors. Only content-shaped ones (encoding warnings, duplicate
    selection) are the worker's alone.
    """

    def test_empty_body_is_caught_at_the_door_even_with_a_declared_size(self) -> None:
        staged = _UploadFile("notes.txt", b"")
        staged.size = 100  # client declares a body; nothing actually arrives
        envelope = self.upload_file(staged)

        self.assertFalse(envelope.ok)
        self.assertEqual(envelope.error["code"], "empty_file")
        rows = self.rows_by_path()
        self.assertEqual(list(rows), [envelope.data["path"]])
        self.assertTrue(envelope.data["path"].startswith("rejected/"))
        self.assertEqual(list(self.uploads.iterdir()), [])  # temp file cleaned up
        row = rows[envelope.data["path"]]
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["validation_state"], "reject")
        self.assertEqual(row["error_code"], "empty_file")
        self.assertEqual(row["source_type"], "upload")

        detail = _run(api.document_detail(envelope.data["document_id"]))
        self.assertEqual(detail.data["document"]["readiness_reason"], "validation_rejected")

    def test_auto_split_is_flagged_the_same_way_at_both_doors(self) -> None:
        payload = b"x" * validation.AUTO_SPLIT_THRESHOLD
        envelope = self.upload("big_transcript.txt", payload)

        row = self.rows_by_path()[envelope.data["path"]]
        self.assertEqual(row["status"], "pending")
        self.assertEqual(row["validation_state"], "auto_split")
        self.assertEqual(row["error_code"], "large_text_file")

        client = AsyncMock()
        client.post_json = AsyncMock(side_effect=AssertionError("must not submit an auto_split row"))
        client.post_file = AsyncMock(side_effect=AssertionError("must not submit an auto_split row"))
        with patch.object(worker, "client", client):
            _run(worker.scan_once())

        self.assertEqual(self.rows_by_path()[envelope.data["path"]], row)
        client.post_json.assert_not_called()
        client.post_file.assert_not_called()

        detail = _run(api.document_detail(envelope.data["document_id"]))
        self.assertEqual(detail.data["document"]["readiness_reason"], "split_required")


if __name__ == "__main__":
    unittest.main()
