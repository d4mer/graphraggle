"""The filename skip rule (GL-07/08 fix 1): `manifest.json` is not a document.

Why this exists
---------------
Every synthetic company folder the GL-07 generator writes carries a `manifest.json`
(the harness's planted-fact index). `.json` is in SUPPORTED and TEXT_EXTENSIONS, so
the worker submitted it as a document and LightRAG failed it in every company folder
on stack2 on 2026-11-12 ("Chunk token length 3431 exceeds chunk_token_size 3060").
The rule refuses such files at the scan door, and writes a terminal `skipped` row
naming the rule instead of a failure row or silence.

What the tests pin
------------------
- a `manifest.json` gets a terminal `skipped` row with the rule as the reason, and
  is never submitted
- a real `.json` document, and any file whose name merely CONTAINS "manifest", still
  ingests: the rule names files, not extensions
- matching is on the basename, case-insensitive
- an empty setting turns the rule off
- a second scan writes nothing (no `updated_at` churn every 30 seconds)
- a row that already has content upstream is not taken down by the rule: the skip
  list is not a deletion mechanism
- the rule covers both doors' scans (source_docs and uploads)

Every fixture is invented text in a temp directory. Nothing here reads the corpus,
and every upstream call is mocked.
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


def _run(coro):
    return asyncio.run(coro)


def _row(path: str) -> dict | None:
    return _run(state_store.get_document_by_path(path))


def _worker_meta(path: str) -> dict:
    row = _row(path) or {}
    meta = worker.parse_warning_metadata(row.get("warnings_json")).get("worker")
    return meta if isinstance(meta, dict) else {}


class _Scanned(unittest.TestCase):
    """Real state_store, real temp filesystem, mocked LightRAG client."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-skiprule-")
        self.base = Path(self._tmp.name)
        self.src = self.base / "source_docs"
        self.uploads = self.base / "uploads"
        self.src.mkdir()
        self.uploads.mkdir()
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir,
                       settings.uploads_dir, settings.ingest_skip_basenames)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.src)
        settings.uploads_dir = str(self.uploads)
        _run(state_store.init_db())
        self.client = AsyncMock()
        self.client.post_json = AsyncMock(return_value={"track_id": "track-1"})
        self.client.post_file = AsyncMock(return_value={"track_id": "track-2"})
        self._client_patch = patch.object(worker, "client", self.client)
        self._client_patch.start()
        # Track polling is covered against the real store in test_lifecycle_cleanup.py;
        # here it would only add fake-HTTP noise to these assertions.
        self._poll_patch = patch.object(worker, "poll_track", AsyncMock())
        self._poll_patch.start()

    def tearDown(self) -> None:
        self._poll_patch.stop()
        self._client_patch.stop()
        settings = state_store.settings
        (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir,
         settings.ingest_skip_basenames) = self._saved
        self._tmp.cleanup()

    def write(self, relative: str, text: str = "invented content\n") -> Path:
        path = self.src / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def scan(self) -> None:
        _run(worker.scan_once())

    def submitted_paths(self) -> list[str]:
        """Every file_source the worker actually sent upstream, in call order."""
        calls = list(self.client.post_json.await_args_list) + list(
            self.client.post_file.await_args_list)
        sources: list[str] = []
        for call in calls:
            if call.args[0] == "/documents/text":
                sources.append(str(call.args[1]["file_source"]))
            else:
                sources.append(str(call.args[1]))
        return sources


class TestSkipRule(_Scanned):
    MANIFEST = (
        '{"company": "Northwind Chemicals", "facts": [{"id": "nc-f1",'
        ' "sentence": "Invented sentence about an invented boiler."}]}\n'
    )

    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.write("Northwind Chemicals/manifest.json", self.MANIFEST)
        self.document = self.write("Northwind Chemicals/expansion-plan.md",
                                   "Invented plan text.\n")

    def test_manifest_json_is_skipped_not_submitted(self) -> None:
        self.scan()

        row = _row(str(self.manifest))
        self.assertIsNotNone(row, "a skipped file still gets a row an operator can see")
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["error_code"], worker.SKIP_RULE_ERROR_CODE)
        self.assertEqual(row["error_stage"], "validation")
        self.assertFalse(row["query_ready"])
        self.assertIsNone(row["track_id"])
        self.assertEqual(row["company"], "Northwind Chemicals")
        self.assertEqual(_worker_meta(str(self.manifest)).get("skip_rule_basename"),
                         "manifest.json")
        self.assertNotIn(str(self.manifest), self.submitted_paths())

    def test_a_real_json_document_still_ingests(self) -> None:
        """The rule names files. It must not switch `.json` off wholesale."""
        orders = self.write("Northwind Chemicals/orders.json", '{"orders": []}\n')
        self.scan()

        self.assertIn(str(orders), self.submitted_paths())
        self.assertEqual(_row(str(orders))["status"], "submitted")
        self.assertEqual(_row(str(self.manifest))["status"], "skipped",
                         "both files are scanned in the same pass")

    def test_readiness_and_summary_say_skipped(self) -> None:
        self.scan()

        [annotated] = [doc for doc in _run(state_store.list_documents())
                       if doc["path"] == str(self.manifest)]
        self.assertFalse(annotated["query_ready"])
        self.assertEqual(annotated["readiness_reason"], "skipped")
        summary = _run(state_store.get_summary())
        self.assertEqual(summary["by_status"]["skipped"], 1)
        self.assertEqual(summary["by_readiness_reason"]["skipped"], 1)

    def test_second_scan_writes_nothing(self) -> None:
        self.scan()
        first = _row(str(self.manifest))
        self.assertEqual(first["status"], "skipped")
        self.scan()

        self.assertEqual(_row(str(self.manifest)), first,
                         "a settled skipped row must not be rewritten every scan")
        self.assertNotIn(str(self.manifest), self.submitted_paths())

    def test_empty_setting_disables_the_rule(self) -> None:
        state_store.settings.ingest_skip_basenames = ""
        self.scan()

        self.assertIn(str(self.manifest), self.submitted_paths())

    def test_the_rule_is_case_insensitive_and_exact(self) -> None:
        upper = self.write("Acme/MANIFEST.JSON", "{}\n")
        suffixed = self.write("Acme/manifest.json.bak", "not a manifest\n")
        prefixed = self.write("Acme/company_manifest.json", "{}\n")
        self.scan()

        self.assertEqual(_row(str(upper))["status"], "skipped")
        self.assertNotIn(str(upper), self.submitted_paths())
        # `.bak` is not a supported extension: it must land in the normal
        # unsupported-extension row, not the skip rule's.
        self.assertEqual(_row(str(suffixed))["error_code"], "unsupported_extension")
        self.assertEqual(_row(str(prefixed))["status"], "submitted")
        self.assertIn(str(prefixed), self.submitted_paths())

    def test_a_manifest_file_sitting_in_uploads_is_skipped_too(self) -> None:
        stray = self.uploads / "manifest.json"
        stray.write_text(self.MANIFEST, encoding="utf-8")
        self.scan()

        row = _row(str(stray))
        self.assertEqual(row["status"], "skipped")
        self.assertEqual(row["source_type"], "upload")
        self.assertNotIn(str(stray), self.submitted_paths())

    def test_the_upload_door_renames_so_the_basename_rule_cannot_see_it(self) -> None:
        """LIMITATION, stated as a test: /upload stages files as ``<id>--<name>``.

        The rule matches basenames, which is what the brief asked for and what the
        GL-07/08 defect needed (the manifests arrive on the filesystem). A file that
        arrives through POST /upload keeps its id prefix on disk, so its basename is
        ``1111--manifest.json`` and it is ingested like any other document. Closing
        that would need a rule on the caller's original_filename at the door, which
        is an api.py change: reported to the lead, not decided here.
        """
        staged = self.uploads / "1111--manifest.json"
        staged.write_text(self.MANIFEST, encoding="utf-8")
        self.scan()

        self.assertIn(str(staged), self.submitted_paths())
        self.assertNotEqual(_row(str(staged))["status"], "skipped")

    def test_an_already_indexed_row_is_not_taken_down(self) -> None:
        """The skip list is not a deletion mechanism."""
        digest = worker.sha256_of(self.manifest)
        _run(state_store.upsert_document(
            document_id="doc-indexed", path=str(self.manifest), source_type="filesystem",
            company="Northwind Chemicals", filename="manifest.json",
            original_filename="manifest.json", sha256=digest, status="ingested",
            track_id=None, validation_state="accept", query_ready=True,
        ))

        self.scan()

        row = _row(str(self.manifest))
        self.assertEqual(row["status"], "ingested")
        self.assertTrue(row["query_ready"])
        self.assertNotIn(str(self.manifest), self.submitted_paths())

    def test_skipped_row_carries_the_bytes_it_refused(self) -> None:
        self.scan()

        row = _row(str(self.manifest))
        self.assertEqual(row["sha256"], worker.sha256_of(self.manifest))
        self.assertEqual(row["content_hash"], worker.sha256_of(self.manifest))
        self.assertGreater(row["byte_size"], 0)


class TestHelpers(unittest.TestCase):
    def test_matching_is_on_the_basename(self) -> None:
        original = state_store.settings.ingest_skip_basenames
        state_store.settings.ingest_skip_basenames = " manifest.json , , NOTES.txt ,"
        try:
            self.assertEqual(worker.configured_skip_basenames(),
                             frozenset({"manifest.json", "notes.txt"}))
            self.assertEqual(worker.skipped_basename_match(Path("/a/b/Manifest.JSON")),
                             "Manifest.JSON")
            self.assertIsNone(worker.skipped_basename_match(Path("/a/b/manifest.jsonl")))
            self.assertIsNone(worker.skipped_basename_match(Path("/manifest.json/x.txt")))
        finally:
            state_store.settings.ingest_skip_basenames = original

    def test_skip_rule_does_not_suppress_the_unsupported_extension_row(self) -> None:
        """`should_skip_path` stays untouched: it writes no row at all."""
        self.assertFalse(worker.should_skip_path(Path("/a/manifest.json"), Path("/a"),
                                                "filesystem"))


if __name__ == "__main__":
    unittest.main()
