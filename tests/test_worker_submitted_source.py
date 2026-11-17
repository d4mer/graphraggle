"""The submitted name: what we send upstream is the name the join later has to recognise.

LightRAG strips directories from `file_source`, so an absolute path becomes a bare basename in its
store - and `Northwind Chemicals/q3.pdf` and `Contoso Pharma/q3.pdf` come back on the query side as the
same `q3.pdf`. `COMPANY_QUALIFIED_FILE_SOURCE` (off by default) makes the name unique for NEW ingests
and records the exact submitted name in `documents.submitted_source`, which the join prefers.

What these tests guard:
- flag off: the submit body is exactly what it was, so an off-state ingest is byte-identical
- flag on: only the submitted name changes - the extracted text, the `chunking` object and therefore
  every piece boundary are identical
- the recorded `submitted_source` is what was sent, never a guess about what LightRAG stored
- a company-less document is not qualified with anything
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

for _key, _value in {
    "RAG_API_KEY": "1234",
    "LIGHTRAG_INTERNAL_API_KEY": "internal",
    "LIGHTRAG_BASE_URL": "http://example:9621",
    "SOURCE_DOCS_DIR": "/tmp/source_docs",
    "UPLOADS_DIR": "/tmp/uploads",
    "STATE_DB_PATH": "/tmp/state.db",
}.items():
    os.environ.setdefault(_key, _value)

import app.document_identity as document_identity
import app.worker as worker

NW = "Northwind Chemicals"


def _run(coro):
    return asyncio.run(coro)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _Store:
    def __init__(self, existing: dict | None = None) -> None:
        self.docs: dict[str, dict] = {}
        if existing:
            self.docs[existing["path"]] = dict(existing)

    async def get_document_by_path(self, path: str):
        doc = self.docs.get(path)
        return dict(doc) if doc else None

    async def get_documents_by_sha256(self, sha256: str) -> list[dict]:
        return [dict(doc) for doc in self.docs.values() if doc.get("sha256") == sha256]

    async def update_document_state(self, path: str, **kwargs) -> None:
        self.docs.setdefault(path, {"path": path}).update(kwargs)

    async def upsert_document(self, **kwargs) -> None:
        self.docs.setdefault(kwargs["path"], {"path": kwargs["path"]}).update(kwargs)

    async def increment_retry(self, path: str) -> int:
        return 0


class _Harness(unittest.TestCase):
    TEXT = ("Quarterly review. The plant freeze under NC-3617-X lasted five hours. "
            "Utilisation held at 82 per cent for the quarter under review here.\n")

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="grg-submitted-"))
        self.src = self.root / "source_docs"
        self.uploads = self.root / "uploads"
        (self.src / NW).mkdir(parents=True)
        self.uploads.mkdir()

    def client(self) -> AsyncMock:
        client = AsyncMock()
        client.post_json = AsyncMock(return_value={"track_id": "t-1", "status": "accepted"})
        client.post_file = AsyncMock(return_value={"track_id": "t-1", "status": "accepted"})
        return client

    def seed_row(self, path: Path, company: str | None) -> None:
        _Store.__init__  # noqa - keep the fake's shape obvious
        self.store = _Store({"path": str(path), "company": company, "status": "validating",
                             "sha256": "pending", "filename": path.name,
                             "original_filename": path.name, "source_type": "scan"})

    def text_file(self, name: str = "q3.txt") -> Path:
        path = self.src / NW / name
        path.write_text(self.TEXT, encoding="utf-8")
        return path

    def submit(self, path: Path, *, qualified: bool, structure: bool = False):
        client = self.client()
        with patch.multiple(worker,
                            get_document_by_path=self.store.get_document_by_path,
                            get_documents_by_sha256=self.store.get_documents_by_sha256,
                            update_document_state=self.store.update_document_state,
                            upsert_document=self.store.upsert_document,
                            increment_retry=self.store.increment_retry), \
                patch.object(worker, "client", client), \
                patch.multiple(worker.settings, source_docs_dir=str(self.src),
                               uploads_dir=str(self.uploads),
                               company_qualified_file_source=qualified,
                               structure_chunking_enabled=structure):
            _run(worker.submit_file(path, "scan", _digest(path)))
        return client


class TestSubmittedSourceBody(unittest.TestCase):
    def setUp(self):
        self.harness = _Harness()
        self.harness.setUp()

    # A .txt is submitted through /documents/text on every setting, so these tests exercise the text
    # door without depending on which PDF extractor the host has.
        path = self.harness.text_file()
        self.harness.seed_row(path, NW)
        client = self.harness.submit(path, qualified=False)
        args = client.post_json.await_args.args
        self.assertEqual(args[0], "/documents/text")
        self.assertEqual(args[1]["file_source"], str(path))
        client.post_file.assert_not_awaited()
        row = self.harness.store.docs[str(path)]
        self.assertEqual(row["submitted_source"], str(path),
                         "what we sent is what we record, even when LightRAG will shorten it")

    def test_flag_on_qualifies_the_name_with_the_company(self):
        path = self.harness.text_file()
        self.harness.seed_row(path, NW)
        client = self.harness.submit(path, qualified=True)
        payload = client.post_json.await_args.args[1]
        self.assertEqual(payload["file_source"], "northwind-chemicals--q3.txt")
        row = self.harness.store.docs[str(path)]
        self.assertEqual(row["submitted_source"], "northwind-chemicals--q3.txt")

    def test_a_document_with_no_company_is_not_qualified_with_anything(self):
        path = self.harness.text_file()
        self.harness.seed_row(path, None)
        client = self.harness.submit(path, qualified=True)
        self.assertEqual(client.post_json.await_args.args[1]["file_source"], str(path))

    def test_the_qualifying_name_does_not_touch_the_text_or_the_chunking(self):
        """The one thing that must stay identical: the payload the chunker produced."""
        path = self.harness.text_file()
        self.harness.seed_row(path, NW)
        off = self.harness.submit(path, qualified=False, structure=True)
        on = self.harness.submit(path, qualified=True, structure=True)
        body_off = off.post_json.await_args.args[1]
        body_on = on.post_json.await_args.args[1]
        self.assertEqual(body_off["text"], body_on["text"],
                         "the submitted name is a label; it must not enter the chunk text")
        self.assertEqual(body_off.get("chunking"), body_on.get("chunking"))
        self.assertNotIn(str(path), body_on["text"],
                         "the absolute path is not in the text either, so no header changed")

    def test_the_upload_door_records_the_name_lightrag_keeps(self):
        # A binary submit goes to /documents/upload, where LightRAG keeps the uploads-directory name.
        path = self.harness.uploads / "6f2a--q3.pdf"
        path.write_bytes(b"%PDF-1.4\n%\xc2\xa0\ntrailer\n%%EOF\n")
        self.harness.seed_row(path, NW)
        client = self.harness.submit(path, qualified=True)
        client.post_file.assert_awaited_once()
        client.post_json.assert_not_awaited()
        row = self.harness.store.docs[str(path)]
        self.assertEqual(row["submitted_source"], path.name,
                         "recorded, not invented: this is the basename upstream was given")


class TestSlugRule(unittest.TestCase):
    def test_the_slug_is_filesystem_safe_and_joinable(self):
        for company, expected in ((NW, "northwind-chemicals"),
                                  ("Solvay & Cie.", "solvay-cie"),
                                  ("Média  Nord", "média-nord"),
                                  ("  Spaced   Out  ", "spaced-out")):
            with self.subTest(company=company):
                self.assertEqual(document_identity.company_slug(company), expected)

    def test_two_companies_that_slug_alike_are_visible_as_a_collision(self):
        # Not silenced here: the audit script counts these, because a collided prefix reproduces the
        # exact ambiguity the qualified name exists to remove.
        self.assertEqual(document_identity.company_slug("Acme Corp"),
                         document_identity.company_slug("acme-corp"))

    def test_qualifying_an_empty_slug_changes_nothing(self):
        self.assertEqual(document_identity.qualified_submitted_source("q3.pdf", "!!!"), "q3.pdf")
        self.assertEqual(document_identity.qualified_submitted_source("q3.pdf", None), "q3.pdf")


if __name__ == "__main__":
    unittest.main()
