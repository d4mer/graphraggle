"""Lifecycle guards: status transitions in app/state_store.py, track_id
compare-and-set in app/worker.py's poll write-backs (TODO near-term #2).

Unlike the worker tests, these drive the **real** state_store against a
throwaway SQLite file so the SQL guards themselves are exercised. No network,
no LightRAG, no corpus: the worker's HTTP client is replaced by a mock.

Covered behaviours:
- a late poll result for a track that has been replaced is ignored
- an illegal status edge is refused (row untouched, write returns False)
- replaying the same stale-track event twice leaves the row identical
- a re-scan of an unchanged, already-ingested file leaves it ingested
- an ingested row whose file changed on disk and no longer validates settles as
  rejected with the new sha, and a second scan does not touch it (TODO #2 review)
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import app.state_store as state_store
import app.worker as worker

PATH = "/docs/a.txt"


def _run(coro):
    return asyncio.run(coro)


def _row(path: str = PATH) -> dict | None:
    return _run(state_store.get_document_by_path(path))


class _RealStore(unittest.TestCase):
    """Fresh temp DB per test, pointed at by settings.state_db_path."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-lifecycle-")
        self._saved_db = state_store.settings.state_db_path
        state_store.settings.state_db_path = str(Path(self._tmp.name) / "state.db")
        _run(state_store.init_db())

    def tearDown(self) -> None:
        state_store.settings.state_db_path = self._saved_db
        self._tmp.cleanup()

    def seed(self, *, path: str = PATH, retry_count: int | None = None, **overrides) -> dict:
        row = {
            "document_id": "doc-1",
            "path": path,
            "source_type": "filesystem",
            "company": None,
            "filename": "a.txt",
            "original_filename": "a.txt",
            "sha256": "sha-a",
            "status": "submitted",
            "track_id": "track-1",
            "validation_state": "accept",
            "query_ready": False,
        }
        row.update(overrides)
        _run(state_store.upsert_document(**row))
        if retry_count is not None:
            _run(state_store.update_document_state(path, retry_count=retry_count))
        return _row(path)


# ── the transition table itself ─────────────────────────────────────────────

class TestTransitionTable(unittest.TestCase):
    def test_every_status_is_a_key_and_targets_are_known_statuses(self) -> None:
        self.assertEqual(set(state_store.LEGAL_TRANSITIONS), set(state_store.STATUSES))
        for source, targets in state_store.LEGAL_TRANSITIONS.items():
            self.assertIsInstance(targets, frozenset)
            self.assertNotIn(source, targets, "self-transitions are handled by rule, not listed")
            self.assertTrue(targets <= state_store.STATUSES, f"{source}: {targets - state_store.STATUSES}")

    def test_a_status_is_always_reachable_from_itself(self) -> None:
        for status in state_store.STATUSES:
            self.assertTrue(state_store.is_legal_transition(status, status), status)

    def test_force_reindex_can_return_any_status_to_pending(self) -> None:
        # /ingest/reindex with force=true writes status="pending" from any row.
        for status in state_store.STATUSES:
            self.assertTrue(state_store.is_legal_transition(status, "pending"), status)

    def test_a_row_that_never_went_upstream_cannot_become_ingested(self) -> None:
        for status in ("pending", "validating", "accepted", "rejected", "superseded"):
            self.assertFalse(state_store.is_legal_transition(status, "ingested"), status)
        for status in ("submitted", "processing", "failed"):
            self.assertTrue(state_store.is_legal_transition(status, "ingested"), status)

    def test_ingested_can_be_rejected_but_never_failed_or_processing_in_place(self) -> None:
        # A file that changed on disk and no longer validates really is rejected,
        # so the row must be allowed to settle that way (TODO #2 review).
        self.assertTrue(state_store.is_legal_transition("ingested", "rejected"))
        for status in ("failed", "processing"):
            self.assertFalse(state_store.is_legal_transition("ingested", status), status)
        for status in ("pending", "validating", "accepted", "submitted", "superseded"):
            self.assertTrue(state_store.is_legal_transition("ingested", status), status)

    def test_rejected_and_superseded_must_be_revalidated(self) -> None:
        for source in ("rejected", "superseded"):
            for status in ("processing", "ingested", "failed"):
                self.assertFalse(state_store.is_legal_transition(source, status), f"{source}->{status}")

    def test_unknown_target_status_is_never_legal(self) -> None:
        for source in state_store.STATUSES:
            self.assertFalse(state_store.is_legal_transition(source, "ingetsed"))
        self.assertFalse(state_store.is_legal_transition("processing", ""))

    def test_unknown_existing_status_is_allowed_out(self) -> None:
        # A status this build does not know about must not wedge the pipeline.
        self.assertTrue(state_store.is_legal_transition("quarantined", "validating"))


# ── update_document_state / update_status guards ────────────────────────────

class TestUpdateGuards(_RealStore):
    def test_illegal_edge_is_refused_and_the_row_is_untouched(self) -> None:
        self.seed(status="ingested", track_id=None, query_ready=True, ingested_at="datetime('now')")
        before = _row()

        self.assertFalse(_run(state_store.update_document_state(PATH, status="failed", error_code="boom")))
        self.assertFalse(_run(state_store.update_status(PATH, "processing")))

        self.assertEqual(_row(), before)

    def test_unknown_status_value_is_refused(self) -> None:
        self.seed(status="processing")
        self.assertFalse(_run(state_store.update_document_state(PATH, status="ingesterd")))
        self.assertEqual(_row()["status"], "processing")

    def test_legal_edges_still_apply(self) -> None:
        self.seed(status="processing", track_id="track-1")
        self.assertTrue(_run(state_store.update_document_state(PATH, status="ingested", query_ready=True)))
        self.assertEqual(_row()["status"], "ingested")
        self.assertEqual(_row()["query_ready"], 1)
        # force reindex from an ingested row
        self.assertTrue(_run(state_store.update_status(PATH, "pending")))
        self.assertEqual(_row()["status"], "pending")
        self.assertEqual(_row()["query_ready"], 0)

    def test_compare_and_set_on_track_id(self) -> None:
        self.seed(status="processing", track_id="track-2")
        self.assertFalse(
            _run(
                state_store.update_document_state(
                    PATH,
                    status="ingested",
                    query_ready=True,
                    expect_track_id="track-1",
                    expect_status="processing",
                )
            )
        )
        self.assertEqual(_row()["status"], "processing")
        self.assertEqual(_row()["query_ready"], 0)

        self.assertTrue(
            _run(
                state_store.update_document_state(
                    PATH,
                    status="ingested",
                    query_ready=True,
                    expect_track_id="track-2",
                    expect_status="processing",
                )
            )
        )
        self.assertEqual(_row()["status"], "ingested")

    def test_compare_and_set_treats_a_missing_track_id_as_null(self) -> None:
        self.seed(status="failed", track_id=None)
        self.assertTrue(_run(state_store.update_document_state(PATH, status="accepted", expect_track_id=None)))
        self.assertEqual(_row()["status"], "accepted")

    def test_upsert_cannot_overwrite_an_ingested_row_along_an_illegal_edge(self) -> None:
        self.seed(status="ingested", track_id=None, query_ready=True)
        self.assertFalse(
            _run(
                state_store.upsert_document(
                    document_id="doc-2",
                    path=PATH,
                    source_type="filesystem",
                    company=None,
                    filename="a.txt",
                    original_filename="a.txt",
                    sha256="sha-b",
                    status="failed",
                    track_id=None,
                    error_stage="track_poll",
                    error_code="late_poll_result",
                )
            )
        )
        row = _row()
        self.assertEqual(row["document_id"], "doc-1")
        self.assertEqual(row["status"], "ingested")
        self.assertEqual(row["sha256"], "sha-a")

    def test_upsert_inserts_and_updates_along_legal_edges(self) -> None:
        self.seed(status="ingested", track_id=None, query_ready=True)
        self.assertTrue(
            _run(
                state_store.upsert_document(
                    document_id="doc-1",
                    path=PATH,
                    source_type="filesystem",
                    company=None,
                    filename="a.txt",
                    original_filename="a.txt",
                    sha256="sha-b",
                    status="submitted",
                    track_id="track-9",
                    validation_state="accept",
                )
            )
        )
        row = _row()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["sha256"], "sha-b")
        self.assertEqual(row["track_id"], "track-9")


# ── worker poll write-backs (compare-and-set on track_id + status) ──────────

class TestPollWriteBacks(_RealStore):
    def _poll(self, snapshot: dict, *, result=None, exc: Exception | None = None) -> None:
        client = AsyncMock()
        if exc is not None:
            client.get = AsyncMock(side_effect=exc)
        else:
            client.get = AsyncMock(return_value=result)
        with patch.object(worker, "client", client):
            _run(worker.poll_track(snapshot))

    def test_late_poll_after_a_resubmit_is_ignored(self) -> None:
        self.seed(status="processing", track_id="track-new")
        current = _row()
        stale_snapshot = dict(current, track_id="track-old")

        self._poll(stale_snapshot, result={"documents": [{"status": "processed"}]})

        self.assertEqual(_row(), current)
        self.assertEqual(_row()["track_id"], "track-new")
        self.assertEqual(_row()["status"], "processing")

    def test_late_failure_for_a_replaced_track_does_not_wreck_the_new_submission(self) -> None:
        self.seed(status="processing", track_id="track-new")
        current = _row()
        stale_snapshot = dict(current, track_id="track-old")

        self._poll(stale_snapshot, result={"documents": [{"status": "failed"}]})

        self.assertEqual(_row(), current)

    def test_matching_poll_still_applies(self) -> None:
        self.seed(status="processing", track_id="track-1")
        self._poll(_row(), result={"documents": [{"status": "processed"}]})
        row = _row()
        self.assertEqual(row["status"], "ingested")
        self.assertEqual(row["query_ready"], 1)
        self.assertEqual(row["retry_count"], 0)

    def test_replaying_the_same_stale_event_is_a_noop(self) -> None:
        self.seed(status="submitted", track_id="track-1", retry_count=1)
        snapshot = _row()

        self._poll(snapshot, result={"documents": []})
        first = _row()
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error_code"], "stale_track_id")
        self.assertIsNone(first["track_id"])
        self.assertEqual(first["retry_count"], 1)  # the poll itself never burns a retry

        # Same event, same snapshot: a second scan, or a retried handle.
        # The write must be refused outright (and said so), not re-applied.
        with self.assertLogs("gateway.state_store", level="WARNING") as logs:
            self._poll(snapshot, result={"documents": []})
        self.assertTrue(any(PATH in line for line in logs.output), logs.output)

        self.assertEqual(_row(), first)  # identical, updated_at included: nothing was written

    def test_stale_replacement_then_resubmit_is_not_overwritten_by_the_old_track(self) -> None:
        self.seed(status="submitted", track_id="track-1")
        self._poll(_row(), exc=_http_404())
        self.assertIsNone(_row()["track_id"])

        # worker resubmits under a new track, then the old poll lands late
        _run(state_store.update_document_state(PATH, status="submitted", track_id="track-2", expect_track_id=None))
        self._poll({"path": PATH, "track_id": "track-1", "status": "submitted", "warnings_json": None},
                   exc=_http_404())

        row = _row()
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["track_id"], "track-2")


def _http_404() -> Exception:
    class _Response:
        status_code = 404

    exc = Exception("404 Not Found")
    exc.response = _Response()  # type: ignore[attr-defined]
    return exc


# ── re-scan of an unchanged ingested file ───────────────────────────────────

class TestRescanKeepsIngested(_RealStore):
    def test_unchanged_ingested_file_stays_ingested_and_is_not_resubmitted(self) -> None:
        root = Path(self._tmp.name) / "src"
        root.mkdir()
        path = root / "a.txt"
        path.write_text("the same content as before\n", encoding="utf-8")
        digest = worker.sha256_of(path)

        self.seed(path=str(path), status="ingested", sha256=digest, content_hash=digest,
                  track_id=None, query_ready=True, ingested_at="datetime('now')")
        before = _row(str(path))

        client = AsyncMock()
        with patch.object(worker, "client", client):
            _run(worker.handle_candidate(path, "filesystem", root))

        after = _row(str(path))
        for field in ("status", "query_ready", "track_id", "sha256", "content_hash",
                      "error_code", "error_stage", "retry_count", "ingested_at"):
            self.assertEqual(after[field], before[field], field)
        client.get.assert_not_called()
        client.post_json.assert_not_called()
        client.post_file.assert_not_called()

    def test_illegal_writeback_still_cannot_downgrade_it_even_if_a_rescan_submits(self) -> None:
        # Second line of defence: even if something does write to an ingested
        # row along a downgrade edge, the store refuses it.
        root = Path(self._tmp.name) / "src"
        root.mkdir()
        path = root / "a.txt"
        path.write_text("content\n", encoding="utf-8")
        self.seed(path=str(path), status="ingested", sha256=worker.sha256_of(path),
                  track_id=None, query_ready=True)
        self.assertFalse(_run(state_store.update_document_state(str(path), status="failed", error_code="late")))
        self.assertEqual(_row(str(path))["status"], "ingested")


# ── an ingested row whose file no longer validates ─────────────────────────

class TestIngestedRowCanBeRejected(_RealStore):
    def test_ingested_row_can_be_rejected_with_the_new_sha(self) -> None:
        """Store level: the edge itself must be writable (TODO #2 review)."""
        self.seed(status="ingested", sha256="sha-old", track_id=None, query_ready=True,
                  ingested_at="datetime('now')")
        self.assertTrue(
            _run(
                state_store.update_document_state(
                    PATH,
                    sha256="sha-new",
                    status="rejected",
                    validation_state="reject",
                    error_stage="validation",
                    error_code="empty_file",
                    error_message="File is empty",
                    last_error="File is empty",
                    track_id=None,
                    query_ready=False,
                    ingested_at=None,
                    content_hash="sha-new",
                )
            )
        )
        row = _row()
        self.assertEqual(row["status"], "rejected")
        self.assertEqual(row["sha256"], "sha-new")
        self.assertEqual(row["query_ready"], 0)
        self.assertEqual(row["error_code"], "empty_file")

    def test_changed_and_now_invalid_file_settles_as_rejected_and_stays_there(self) -> None:
        """Whole worker path: ingest, edit the file to something invalid, scan.

        The row must end rejected with the *new* sha and not query-ready, and a
        second scan must leave it untouched (no re-write, no submit).
        """
        root = Path(self._tmp.name) / "src"
        root.mkdir()
        path = root / "a.txt"
        path.write_text("content that was fine\n", encoding="utf-8")
        old_sha = worker.sha256_of(path)
        self.seed(path=str(path), status="ingested", sha256=old_sha, content_hash=old_sha,
                  track_id=None, query_ready=True, ingested_at="datetime('now')")

        path.write_text("", encoding="utf-8")  # now empty -> validation reject
        new_sha = worker.sha256_of(path)

        client = AsyncMock()
        client.post_json = AsyncMock(side_effect=AssertionError("must not submit an invalid file"))
        client.post_file = AsyncMock(side_effect=AssertionError("must not submit an invalid file"))
        with patch.object(worker, "client", client):
            _run(worker.handle_candidate(path, "filesystem", root))

        settled = _row(str(path))
        self.assertEqual(settled["status"], "rejected")
        self.assertEqual(settled["sha256"], new_sha)
        self.assertEqual(settled["validation_state"], "reject")
        self.assertEqual(settled["error_code"], "empty_file")
        self.assertEqual(settled["query_ready"], 0)
        self.assertIsNone(settled["track_id"])
        client.post_json.assert_not_called()
        client.post_file.assert_not_called()

        with patch.object(worker, "client", client):
            _run(worker.handle_candidate(path, "filesystem", root))

        self.assertEqual(_row(str(path)), settled)  # identical, updated_at included

    def test_submit_file_never_writes_onto_an_ingested_row(self) -> None:
        """Why ingested -> failed stays denied: by the time submit_file runs, the
        scan has already moved the row to ``accepted``.
        """
        root = Path(self._tmp.name) / "src"
        root.mkdir()
        path = root / "a.txt"
        path.write_text("first content\n", encoding="utf-8")
        old_sha = worker.sha256_of(path)
        self.seed(path=str(path), status="ingested", sha256=old_sha, content_hash=old_sha,
                  track_id=None, query_ready=True, ingested_at="datetime('now')")
        path.write_text("changed but still valid content\n", encoding="utf-8")

        seen: list[str] = []
        real_submit_file = worker.submit_file

        async def _spy_submit(candidate: Path, source_type: str, digest: str, *, retry_reason=None):
            # The row as it stands when submit_file starts, i.e. what its writes
            # (including the failure paths) will land on.
            row = await state_store.get_document_by_path(str(candidate))
            seen.append((row or {}).get("status"))
            return await real_submit_file(candidate, source_type, digest, retry_reason=retry_reason)

        client = AsyncMock()
        client.post_json = AsyncMock(return_value={"track_id": "track-new"})
        with patch.object(worker, "client", client), patch.object(worker, "submit_file", _spy_submit):
            _run(worker.handle_candidate(path, "filesystem", root))

        self.assertEqual(seen, ["accepted"])
        row = _row(str(path))
        self.assertEqual(row["status"], "submitted")
        self.assertEqual(row["track_id"], "track-new")
        self.assertEqual(row["sha256"], worker.sha256_of(path))


if __name__ == "__main__":
    unittest.main()
