"""Tests for packet-25: source_docs files are submitted through LightRAG's
input-dir scan instead of ``POST /documents/upload``.

LightRAG v1.5.7 refuses an upload whose canonical basename already exists in its
``--input-dir`` (HTTP 409). Production points that directory at SOURCE_DOCS_DIR,
so every non-text file there was permanently un-ingestible. These tests pin:

- a .docx under SOURCE_DOCS_DIR never calls /documents/upload and is covered by
  exactly one /documents/scan per scan_once() cycle, for N files
- every deferred row gets the same scan track_id and status "submitted"
- scanning_skipped_pipeline_busy and HTTP 429/409/503 on the scan are transient:
  no failure row, the document stays retryable
- WORKER_SCAN_SUBMIT_ENABLED=false falls back to the old upload path
- a file under UPLOADS_DIR still uses /documents/upload; a .txt still uses
  /documents/text
- poll_track reads a shared scan track per file (matched by basename), keeps a
  not-yet-listed file processing rather than stale, and leaves the existing
  empty-documents stale_track_id behaviour alone

Nothing here touches the network or a real database.
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

_REQUIRED_ENV = {
    "RAG_API_KEY": "1234",
    "LIGHTRAG_INTERNAL_API_KEY": "internal",
    "LIGHTRAG_BASE_URL": "http://example:9621",
    "SOURCE_DOCS_DIR": "/tmp/source_docs",
    "UPLOADS_DIR": "/tmp/uploads",
    "STATE_DB_PATH": "/tmp/state.db",
}
for _key, _value in _REQUIRED_ENV.items():
    os.environ.setdefault(_key, _value)

import app.lightrag_client as lightrag_module
import app.worker as worker


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStateStore:
    """In-memory stand-in for the app.state_store functions worker.py calls."""

    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def get_document_by_path(self, path: str):
        doc = self.docs.get(path)
        return dict(doc) if doc else None

    async def get_documents_by_sha256(self, sha256: str) -> list[dict]:
        return [dict(doc) for doc in self.docs.values() if doc.get("sha256") == sha256]

    async def update_document_state(self, path: str, **kwargs) -> None:
        doc = self.docs.setdefault(path, {"path": path})
        doc.update(kwargs)

    async def upsert_document(self, **kwargs) -> None:
        doc = self.docs.setdefault(kwargs["path"], {"path": kwargs["path"]})
        doc.update(kwargs)

    async def increment_retry(self, path: str) -> int:
        doc = self.docs.get(path)
        if not doc:
            return 0
        doc["retry_count"] = int(doc.get("retry_count") or 0) + 1
        return doc["retry_count"]

    def meta(self, path: str) -> dict:
        raw = self.docs.get(path, {}).get("warnings_json") or "{}"
        block = json.loads(raw).get("worker")
        return block if isinstance(block, dict) else {}


class _FakeClient:
    """Records every LightRAG call the worker makes."""

    def __init__(self, scan_response: dict | None = None, scan_error: Exception | None = None):
        if scan_error is not None:
            self.post_scan = AsyncMock(side_effect=scan_error)
        else:
            self.post_scan = AsyncMock(
                return_value=scan_response or {"status": "scanning", "track_id": "scan-track-1"}
            )
        self.post_file = AsyncMock(return_value={"track_id": "upload-track-1", "status": "accepted"})
        self.post_json = AsyncMock(return_value={"track_id": "text-track-1", "status": "accepted"})
        self.get = AsyncMock(return_value={"track_id": "t", "status": "processing", "documents": []})


def _scan_meta(track_source: str = "scan") -> str:
    return json.dumps({"worker": {"track_source": track_source}})


# ── uses_scan_submit: the routing predicate ─────────────────────────────────

class TestUsesScanSubmit(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.src = self.root / "source_docs"
        self.uploads = self.root / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        self.patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.src),
            uploads_dir=str(self.uploads),
            worker_scan_submit_enabled=True,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_binary_office_file_under_source_docs_uses_scan(self):
        path = self.src / "canary.docx"
        path.write_bytes(b"PK\x03\x04 fake docx")
        self.assertTrue(worker.uses_scan_submit(path))

    def test_text_extension_still_uses_documents_text(self):
        path = self.src / "note.md"
        path.write_text("# note\n", encoding="utf-8")
        self.assertFalse(worker.uses_scan_submit(path))

    def test_file_under_uploads_dir_does_not_use_scan(self):
        path = self.uploads / "dropped.docx"
        path.write_bytes(b"PK\x03\x04 fake docx")
        self.assertFalse(worker.uses_scan_submit(path))

    def test_flag_off_disables_scan_routing(self):
        path = self.src / "canary.docx"
        path.write_bytes(b"PK\x03\x04 fake docx")
        with patch.object(worker.settings, "worker_scan_submit_enabled", False):
            self.assertFalse(worker.uses_scan_submit(path))

    def test_nested_file_under_source_docs_uses_scan(self):
        nested = self.src / "2026" / "q1"
        nested.mkdir(parents=True)
        path = nested / "deck.pptx"
        path.write_bytes(b"fake pptx bytes")
        self.assertTrue(worker.uses_scan_submit(path))


# ── scan_once: one scan per cycle, no upload for source_docs files ──────────

class TestScanOnceScanSubmission(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.src = self.root / "source_docs"
        self.uploads = self.root / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        self.store = _FakeStateStore()
        self.patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.src),
            uploads_dir=str(self.uploads),
            worker_scan_submit_enabled=True,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _patch_store(self):
        return patch.multiple(
            worker,
            get_document_by_path=self.store.get_document_by_path,
            get_documents_by_sha256=self.store.get_documents_by_sha256,
            update_document_state=self.store.update_document_state,
            upsert_document=self.store.upsert_document,
            increment_retry=self.store.increment_retry,
        )

    def _write(self, name: str, payload: bytes | None = None) -> Path:
        path = self.src / name
        path.write_bytes(payload if payload is not None else f"content of {name}".encode("utf-8"))
        return path

    def test_docx_under_source_docs_never_calls_upload(self):
        self._write("canary.docx", b"PK\x03\x04 fake docx body")
        fake = _FakeClient()
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        fake.post_file.assert_not_awaited()
        fake.post_scan.assert_awaited_once()

    def test_one_scan_covers_every_file_in_the_cycle(self):
        for name in ("a.docx", "b.pptx", "c.xlsx"):
            self._write(name, f"distinct bytes for {name}".encode("utf-8"))
        fake = _FakeClient()
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        fake.post_scan.assert_awaited_once()
        fake.post_file.assert_not_awaited()
        track_ids = {doc.get("track_id") for doc in self.store.docs.values()}
        self.assertEqual(track_ids, {"scan-track-1"})
        for path, doc in self.store.docs.items():
            self.assertEqual(doc.get("status"), "submitted", path)
            self.assertEqual(worker.is_scan_track(doc.get("warnings_json")), True, path)

    def test_scan_skipped_pipeline_busy_leaves_rows_retryable(self):
        path = self._write("canary.docx", b"PK\x03\x04 fake docx body")
        fake = _FakeClient(scan_response={
            "status": "scanning_skipped_pipeline_busy",
            "message": "LightRAG pipeline busy",
            "track_id": None,
        })
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        doc = self.store.docs[str(path)]
        self.assertNotEqual(doc.get("status"), "failed")
        self.assertEqual(doc.get("status"), "accepted")
        self.assertIsNone(doc.get("track_id"))
        self.assertEqual(doc.get("error_code"), "scan_deferred")
        self.assertTrue(worker.is_retryable_failure(doc))
        self.assertEqual(self.store.meta(str(path)).get("retryable"), True)
        self.assertEqual(self.store.meta(str(path)).get("failure_class"), "transient")

    def test_scan_http_error_is_transient_not_a_failure_row(self):
        path = self._write("canary.docx", b"PK\x03\x04 fake docx body")

        class _Http429(Exception):
            pass

        fake = _FakeClient(scan_error=_Http429("HTTP 429 Too Many Requests"))
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        doc = self.store.docs[str(path)]
        self.assertNotEqual(doc.get("status"), "failed")
        self.assertEqual(doc.get("error_code"), "scan_deferred")
        self.assertTrue(worker.is_retryable_failure(doc))

    def test_missing_track_id_in_response_is_transient(self):
        path = self._write("canary.docx", b"PK\x03\x04 fake docx body")
        fake = _FakeClient(scan_response={"status": "scanning", "message": "no track id"})
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        doc = self.store.docs[str(path)]
        self.assertNotEqual(doc.get("status"), "failed")
        self.assertEqual(doc.get("error_code"), "scan_deferred")

    def test_flag_off_falls_back_to_upload_path(self):
        self._write("canary.docx", b"PK\x03\x04 fake docx body")
        fake = _FakeClient()
        with self._patch_store(), patch.object(worker, "client", fake), \
                patch.object(worker.settings, "worker_scan_submit_enabled", False):
            _run_async(worker.scan_once())

        fake.post_file.assert_awaited_once()
        self.assertEqual(fake.post_file.await_args.args[0], "/documents/upload")
        fake.post_scan.assert_not_awaited()

    def test_uploads_dir_file_still_uses_upload(self):
        (self.uploads / "dropped.docx").write_bytes(b"PK\x03\x04 fake docx body")
        fake = _FakeClient()
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        fake.post_file.assert_awaited_once()
        fake.post_scan.assert_not_awaited()

    def test_text_file_still_uses_documents_text(self):
        (self.src / "note.txt").write_text("plain text note\n", encoding="utf-8")
        fake = _FakeClient()
        with self._patch_store(), patch.object(worker, "client", fake):
            _run_async(worker.scan_once())

        fake.post_json.assert_awaited_once()
        self.assertEqual(fake.post_json.await_args.args[0], "/documents/text")
        fake.post_file.assert_not_awaited()
        fake.post_scan.assert_not_awaited()

    def test_scan_log_line_reports_file_count_and_track_id(self):
        for name in ("a.docx", "b.docx"):
            self._write(name, f"different bytes {name}".encode("utf-8"))
        fake = _FakeClient()
        logged = []
        with self._patch_store(), patch.object(worker, "client", fake), \
                patch.object(worker, "warn", logged.append):
            _run_async(worker.scan_once())

        self.assertTrue(any("2 file(s)" in line and "scan-track-1" in line for line in logged))


# ── poll_track: per-file status from a shared scan track ────────────────────

class TestPollTrackScanTrack(unittest.TestCase):
    def setUp(self):
        self.store = _FakeStateStore()

    def _doc(self, name: str, status: str, track_id: str = "scan-track-1") -> dict:
        path = f"source_docs/{name}"
        return {
            "path": path,
            "status": status,
            "track_id": track_id,
            "retry_count": 0,
            "warnings_json": _scan_meta(),
        }

    def _poll(self, doc: dict, track_docs: list[dict]):
        fake = _FakeClient()
        fake.get = AsyncMock(return_value={
            "track_id": doc["track_id"],
            "status": "processing",
            "documents": track_docs,
        })
        with patch.multiple(
            worker,
            get_document_by_path=self.store.get_document_by_path,
            update_document_state=self.store.update_document_state,
        ), patch.object(worker, "client", fake):
            self.store.docs[doc["path"]] = dict(doc)
            _run_async(worker.poll_track(dict(doc)))
        return self.store.docs[doc["path"]]

    def test_each_file_takes_its_own_status_from_the_shared_track(self):
        track_docs = [
            {"file_path": "input/canary.docx", "status": "processed"},
            {"file_path": "input/broken.docx", "status": "failed"},
            {"file_path": "input/pending.docx", "status": "processing"},
        ]
        ingested = self._poll(self._doc("canary.docx", "submitted"), track_docs)
        self.assertEqual(ingested["status"], "ingested")
        self.assertTrue(ingested.get("query_ready"))

        failed = self._poll(self._doc("broken.docx", "submitted"), track_docs)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed.get("error_code"), "lightrag_processing_failed")

        processing = self._poll(self._doc("pending.docx", "submitted"), track_docs)
        self.assertEqual(processing["status"], "processing")

    def test_one_failed_sibling_does_not_fail_this_file(self):
        track_docs = [
            {"file_path": "input/canary.docx", "status": "processed"},
            {"file_path": "input/broken.docx", "status": "failed"},
        ]
        ingested = self._poll(self._doc("canary.docx", "submitted"), track_docs)
        self.assertEqual(ingested["status"], "ingested")

    def test_absent_entry_stays_processing_not_stale(self):
        track_docs = [{"file_path": "input/other.docx", "status": "processed"}]
        doc = self._poll(self._doc("canary.docx", "submitted"), track_docs)
        self.assertEqual(doc["status"], "processing")
        self.assertIsNone(doc.get("error_code"))
        self.assertNotEqual(doc.get("error_code"), "stale_track_id")
        self.assertIsNone(self.store.meta("source_docs/canary.docx").get("stale_track_id"))

    def test_absent_entry_does_not_inherit_another_files_failure(self):
        track_docs = [{"file_path": "input/other.docx", "status": "failed"}]
        doc = self._poll(self._doc("canary.docx", "submitted"), track_docs)
        self.assertEqual(doc["status"], "processing")

    def test_empty_documents_keeps_existing_stale_behaviour(self):
        doc = self._poll(self._doc("canary.docx", "submitted"), [])
        self.assertEqual(doc["status"], "failed")
        self.assertEqual(doc.get("error_code"), "stale_track_id")

    def test_non_scan_track_keeps_aggregate_behaviour(self):
        doc = self._poll(
            {
                "path": "uploads/dropped.docx",
                "status": "submitted",
                "track_id": "upload-track-1",
                "retry_count": 0,
                "warnings_json": json.dumps({"worker": {"track_source": None}}),
            },
            [{"file_path": "uploads/dropped.docx", "status": "processed"}],
        )
        self.assertEqual(doc["status"], "ingested")

    def test_basename_matching_ignores_case_and_whitespace(self):
        track_docs = [{"file_path": "input/Canary Report.docx", "status": "processed"}]
        doc = self._poll(self._doc("canary  report.docx", "submitted"), track_docs)
        self.assertEqual(doc["status"], "ingested")


# ── LightRAG client helper ──────────────────────────────────────────────────

class TestPostScanHelper(unittest.TestCase):
    def test_post_scan_posts_to_documents_scan_with_no_body(self):
        calls: list[tuple[str, dict]] = []

        class _Resp:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"status": "scanning", "message": "scan started", "track_id": "scan-track-1"}

        class _Http:
            def __call__(self, **kwargs):
                return self

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc) -> bool:
                return False

            async def post(self, url, **kwargs):
                calls.append((url, kwargs))
                return _Resp()

        with patch.object(lightrag_module.httpx, "AsyncClient", _Http()):
            payload = _run_async(lightrag_module.client.post_scan())

        self.assertEqual(payload["track_id"], "scan-track-1")
        self.assertEqual(len(calls), 1)
        url, kwargs = calls[0]
        self.assertTrue(url.endswith("/documents/scan"), url)
        for body_key in ("json", "data", "content"):
            self.assertNotIn(body_key, kwargs)
        self.assertIn("X-API-Key", kwargs.get("headers", {}))


if __name__ == "__main__":
    unittest.main()
