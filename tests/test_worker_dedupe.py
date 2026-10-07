"""Tests for worker content-dedupe hardening (TODO near-term #1).

Covers:
- pick_duplicate_candidate never selects a row that is itself a duplicate
  (error_code == "duplicate_content") as the duplicate_of target
- existing rank behaviour: ingested > processing > submitted > accepted
- failed originals (retry-exhausted or not) are never eligible originals
- handle_candidate re-evaluates an orphaned duplicate every scan and resubmits
  it when no live original remains
- handle_candidate stays idempotent (no DB writes, no submit) while the
  original is still live
- poll_track fall-through: a poll that lands in a non-terminal, non-submitted
  state continues into the retry bookkeeping path

Imports app.worker with the required env vars set, mocks the state_store
functions it uses, and never touches the network or a real database.
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

# app.worker imports app.config (and app.lightrag_client) at import time, and
# those require these settings to exist. Values are fake; nothing connects.
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

import app.worker as worker


# ── Helpers ─────────────────────────────────────────────────────────────────

def _run_async(coro):
    """Helper to run async code from sync test methods."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _FakeStateStore:
    """In-memory stand-in for the app.state_store functions worker.py calls."""

    def __init__(self, docs: list[dict]):
        self.docs = {doc["path"]: dict(doc) for doc in docs}
        self.state_updates: list[tuple[str, dict]] = []
        self.retried: list[str] = []
        self.upserted: list[dict] = []

    async def get_document_by_path(self, path: str):
        doc = self.docs.get(path)
        return dict(doc) if doc else None

    async def get_documents_by_sha256(self, sha256: str) -> list[dict]:
        return [dict(doc) for doc in self.docs.values() if doc.get("sha256") == sha256]

    async def update_document_state(self, path: str, **kwargs) -> None:
        self.state_updates.append((path, kwargs))
        doc = self.docs.setdefault(path, {"path": path})
        doc.update(kwargs)

    async def upsert_document(self, **kwargs) -> None:
        self.upserted.append(kwargs)

    async def increment_retry(self, path: str) -> int:
        self.retried.append(path)
        doc = self.docs.get(path)
        if not doc:
            return 0
        doc["retry_count"] = int(doc.get("retry_count") or 0) + 1
        return doc["retry_count"]

    # Convenience accessors for assertions ------------------------------------

    def updates_for(self, path: str) -> list[dict]:
        return [kwargs for updated_path, kwargs in self.state_updates if updated_path == path]

    def worker_meta(self, path: str) -> dict:
        """Parsed warnings_json.worker block currently held in the fake store."""
        raw = self.docs[path].get("warnings_json") or "{}"
        meta = json.loads(raw)
        worker_block = meta.get("worker")
        return worker_block if isinstance(worker_block, dict) else {}

    def duplicate_of(self, path: str):
        raw = self.docs[path].get("warnings_json") or "{}"
        return json.loads(raw).get("duplicate_of")


def _doc(path: Path, sha256: str, status: str, **extra: object) -> dict:
    doc = {
        "document_id": str(path),
        "path": str(path),
        "source_type": "filesystem",
        "filename": path.name,
        "original_filename": path.name,
        "sha256": sha256,
        "status": status,
        "validation_state": "pass",
        "error_stage": None,
        "error_code": None,
        "error_message": None,
        "error_detail": None,
        "last_error": None,
        "track_id": None,
        "query_ready": False,
        "content_hash": sha256,
        "byte_size": 15,
        "ingested_at": None,
        "retry_count": 0,
        "warnings_json": "{}",
        "updated_at": "2026-01-01 00:00:00",
    }
    doc.update(extra)
    return doc


def _duplicate_meta(original_path: str) -> str:
    return json.dumps(
        {
            "duplicate_of": {
                "path": original_path,
                "status": "ingested",
                "document_id": original_path,
            },
            "worker": {
                "dedupe_action": "skip_duplicate_content",
                "dedupe_basis": "sha256_status_updated_at_path",
                "duplicate_status": "ingested",
                "duplicate_match_count": 1,
                "duplicate_match_paths": [original_path],
            },
        }
    )


# ── pick_duplicate_candidate ────────────────────────────────────────────────

class TestPickDuplicateCandidate(unittest.TestCase):
    def _pick(self, docs, current_path):
        return worker.pick_duplicate_candidate(docs, current_path)

    def test_ignores_row_that_is_itself_a_duplicate(self):
        """Bug 1: a duplicate row must never become the duplicate_of target."""
        docs = [
            _doc(
                Path("b.txt"),
                "sha-a",
                "accepted",
                error_code="duplicate_content",
                error_stage="validation",
            ),
        ]
        self.assertIsNone(self._pick(docs, "a.txt"))

    def test_ignores_duplicate_row_even_when_it_ranks_higher(self):
        docs = [
            _doc(
                Path("b.txt"),
                "sha-a",
                "ingested",
                error_code="duplicate_content",
                updated_at="2026-05-01 00:00:00",
            ),
            _doc(Path("c.txt"), "sha-a", "accepted", updated_at="2026-01-01 00:00:00"),
        ]
        picked = self._pick(docs, "a.txt")
        self.assertIsNotNone(picked)
        self.assertEqual(picked["path"], "c.txt")
        self.assertEqual(picked["match_count"], 1)
        self.assertEqual(picked["match_paths"], ["c.txt"])

    def test_rank_prefers_ingested_over_processing_over_submitted_over_accepted(self):
        docs = [
            _doc(Path("d.txt"), "sha-a", "accepted", updated_at="2026-09-09 00:00:00"),
            _doc(Path("c.txt"), "sha-a", "submitted", updated_at="2026-09-09 00:00:00"),
            _doc(Path("b.txt"), "sha-a", "processing", updated_at="2026-09-09 00:00:00"),
            _doc(Path("a.txt"), "sha-a", "ingested", updated_at="2026-01-01 00:00:00"),
        ]
        picked = self._pick(docs, "current.txt")
        self.assertEqual(picked["path"], "a.txt")
        self.assertEqual(picked["match_count"], 4)
        self.assertEqual(
            picked["match_paths"], ["a.txt", "b.txt", "c.txt", "d.txt"]
        )

    def test_excludes_the_row_being_evaluated(self):
        docs = [_doc(Path("a.txt"), "sha-a", "ingested")]
        self.assertIsNone(self._pick(docs, "a.txt"))

    def test_failed_original_is_not_eligible_when_retries_exhausted(self):
        """Bug 4 pin: a retry-exhausted failed row is not an eligible original."""
        docs = [
            _doc(
                Path("b.txt"),
                "sha-a",
                "failed",
                error_code="submit_failed",
                retry_count=worker.settings.ingest_max_retries,
            ),
        ]
        self.assertIsNone(self._pick(docs, "a.txt"))

    def test_failed_original_is_not_eligible_with_retries_remaining(self):
        docs = [_doc(Path("b.txt"), "sha-a", "failed", error_code="submit_failed", retry_count=0)]
        self.assertIsNone(self._pick(docs, "a.txt"))

    def test_rejected_and_validating_rows_are_not_eligible(self):
        docs = [
            _doc(Path("b.txt"), "sha-a", "rejected"),
            _doc(Path("c.txt"), "sha-a", "validating"),
        ]
        self.assertIsNone(self._pick(docs, "a.txt"))


# ── handle_candidate: duplicate rows across repeated scans ──────────────────

class TestHandleCandidateDuplicates(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "source_docs"
        self.company, self.company_source, self.company_source_detail = (
            worker.infer_company_from_path(self.root / "acme" / "a.txt", self.root, "filesystem")
        )

    def _file(self, name: str, content: bytes = b"identical body\n") -> Path:
        path = self.root / "acme" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def _attributed(self, doc: dict) -> dict:
        doc["company"] = self.company
        doc["company_source"] = self.company_source
        doc["company_source_detail"] = self.company_source_detail
        return doc

    def _handle(self, store: _FakeStateStore, path: Path, *, poll_track=None) -> AsyncMock:
        submit_file = AsyncMock()
        with patch.multiple(
            worker,
            get_document_by_path=store.get_document_by_path,
            get_documents_by_sha256=store.get_documents_by_sha256,
            update_document_state=store.update_document_state,
            upsert_document=store.upsert_document,
            increment_retry=store.increment_retry,
            submit_file=submit_file,
            poll_track=poll_track or AsyncMock(),
        ):
            _run_async(worker.handle_candidate(path, "filesystem", self.root))
        return submit_file

    def test_orphaned_duplicate_is_resubmitted_and_markers_cleared(self):
        """Bug 2: original deleted/changed/rejected -> duplicate must be ingested."""
        dup = self._file("dup.txt")
        digest = worker.sha256_of(dup)
        store = _FakeStateStore([
            self._attributed(
                _doc(
                    dup,
                    digest,
                    "accepted",
                    validation_state="warn",
                    error_stage="validation",
                    error_code="duplicate_content",
                    error_message="Duplicate content already represented by another document",
                    warnings_json=_duplicate_meta(str(self.root / "acme" / "orig.txt")),
                )
            )
        ])

        submit_file = self._handle(store, dup)

        submit_file.assert_awaited_once()
        submitted_path = submit_file.await_args.args[0]
        self.assertEqual(submitted_path, dup)
        self.assertEqual(submit_file.await_args.args[2], digest)

        updates = store.updates_for(str(dup))
        self.assertTrue(updates, "expected duplicate markers to be cleared")
        clearing = updates[0]
        self.assertIsNone(clearing.get("error_code"))
        self.assertIsNone(clearing.get("error_stage"))
        self.assertIsNone(clearing.get("error_message"))
        self.assertIsNone(store.duplicate_of(str(dup)))
        meta = store.worker_meta(str(dup))
        for key in ("dedupe_action", "dedupe_basis", "duplicate_status", "duplicate_match_count"):
            self.assertNotIn(key, meta)

    def test_duplicate_with_live_original_is_idempotent(self):
        """Repeated scans must not re-write or re-submit a still-duplicate row."""
        dup = self._file("dup.txt")
        original = self._file("orig.txt")
        digest = worker.sha256_of(dup)
        self.assertEqual(worker.sha256_of(original), digest)
        store = _FakeStateStore([
            self._attributed(
                _doc(
                    dup,
                    digest,
                    "accepted",
                    validation_state="warn",
                    error_stage="validation",
                    error_code="duplicate_content",
                    warnings_json=_duplicate_meta(str(original)),
                )
            ),
            self._attributed(_doc(original, digest, "ingested", query_ready=True)),
        ])

        submit_file = self._handle(store, dup)

        submit_file.assert_not_awaited()
        self.assertEqual(store.state_updates, [])
        self.assertEqual(store.docs[str(dup)]["error_code"], "duplicate_content")

    def test_mutual_duplicate_original_is_resubmitted_not_remarked(self):
        """Bug 1 end-to-end: retried original must not be marked duplicate of its own copy."""
        original = self._file("a.txt")
        copy = self._file("b.txt")
        digest = worker.sha256_of(original)
        self.assertEqual(worker.sha256_of(copy), digest)
        store = _FakeStateStore([
            self._attributed(
                _doc(original, digest, "failed", error_code="submit_failed", retry_count=0)
            ),
            self._attributed(
                _doc(
                    copy,
                    digest,
                    "accepted",
                    validation_state="warn",
                    error_stage="validation",
                    error_code="duplicate_content",
                    warnings_json=_duplicate_meta(str(original)),
                )
            ),
        ])

        submit_file = self._handle(store, original)

        submit_file.assert_awaited_once()
        self.assertEqual(submit_file.await_args.args[0], original)
        # the original must not be re-marked as a duplicate of its own copy
        self.assertNotEqual(store.docs[str(original)]["error_code"], "duplicate_content")
        self.assertIsNone(store.duplicate_of(str(original)))
        self.assertNotIn("dedupe_action", store.worker_meta(str(original)))
        # the duplicate row is left alone
        self.assertEqual(store.docs[str(copy)]["error_code"], "duplicate_content")

    def test_duplicate_row_with_only_failed_copy_is_resubmitted(self):
        dup = self._file("dup.txt")
        other = self._file("other.txt")
        digest = worker.sha256_of(dup)
        store = _FakeStateStore([
            self._attributed(
                _doc(
                    dup,
                    digest,
                    "accepted",
                    validation_state="warn",
                    error_stage="validation",
                    error_code="duplicate_content",
                    warnings_json=_duplicate_meta(str(other)),
                )
            ),
            self._attributed(_doc(other, digest, "failed", error_code="track_status_failed")),
        ])

        submit_file = self._handle(store, dup)

        submit_file.assert_awaited_once()
        self.assertEqual(submit_file.await_args.args[0], dup)


# ── handle_candidate: poll_track fall-through ───────────────────────────────

class TestHandleCandidatePollFallThrough(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "source_docs"
        self.path = self.root / "acme" / "a.txt"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_bytes(b"body\n")
        self.digest = worker.sha256_of(self.path)
        company, company_source, company_source_detail = worker.infer_company_from_path(
            self.path, self.root, "filesystem"
        )
        self.company = (company, company_source, company_source_detail)

    def _poll_moves_to_failed(self, store: _FakeStateStore):
        async def _poll(doc: dict) -> None:
            await store.update_document_state(
                str(self.path), status="failed", error_code="submit_failed"
            )

        return _poll

    def test_poll_ending_in_failed_state_continues_retry_bookkeeping(self):
        """Bug 3: the lines after the second return in the poll block were dead."""
        doc = _doc(
            self.path,
            self.digest,
            "submitted",
            track_id="track-1",
        )
        doc["company"], doc["company_source"], doc["company_source_detail"] = self.company
        store = _FakeStateStore([doc])
        store.state_updates.clear()

        with patch.multiple(
            worker,
            get_document_by_path=store.get_document_by_path,
            get_documents_by_sha256=store.get_documents_by_sha256,
            update_document_state=store.update_document_state,
            upsert_document=store.upsert_document,
            increment_retry=store.increment_retry,
            submit_file=AsyncMock(),
            poll_track=self._poll_moves_to_failed(store),
        ):
            _run_async(worker.handle_candidate(self.path, "filesystem", self.root))

        self.assertEqual(store.retried, [str(self.path)])
        self.assertEqual(store.docs[str(self.path)]["retry_count"], 1)

    def test_poll_ending_in_failed_state_resubmits_with_retry_reason(self):
        doc = _doc(self.path, self.digest, "submitted", track_id="track-1")
        doc["company"], doc["company_source"], doc["company_source_detail"] = self.company
        store = _FakeStateStore([doc])
        store.state_updates.clear()
        submit_file = AsyncMock()

        with patch.multiple(
            worker,
            get_document_by_path=store.get_document_by_path,
            get_documents_by_sha256=store.get_documents_by_sha256,
            update_document_state=store.update_document_state,
            upsert_document=store.upsert_document,
            increment_retry=store.increment_retry,
            submit_file=submit_file,
            poll_track=self._poll_moves_to_failed(store),
        ):
            _run_async(worker.handle_candidate(self.path, "filesystem", self.root))

        submit_file.assert_awaited_once()
        self.assertEqual(
            submit_file.await_args.kwargs.get("retry_reason"), "submit_failed"
        )

    def test_poll_still_processing_returns_without_submit(self):
        async def _noop_poll(doc: dict) -> None:
            return None

        doc = _doc(self.path, self.digest, "processing", track_id="track-1")
        doc["company"], doc["company_source"], doc["company_source_detail"] = self.company
        store = _FakeStateStore([doc])
        store.state_updates.clear()
        submit_file = AsyncMock()

        with patch.multiple(
            worker,
            get_document_by_path=store.get_document_by_path,
            get_documents_by_sha256=store.get_documents_by_sha256,
            update_document_state=store.update_document_state,
            upsert_document=store.upsert_document,
            increment_retry=store.increment_retry,
            submit_file=submit_file,
            poll_track=_noop_poll,
        ):
            _run_async(worker.handle_candidate(self.path, "filesystem", self.root))

        submit_file.assert_not_awaited()
        self.assertEqual(store.retried, [])


if __name__ == "__main__":
    unittest.main()
