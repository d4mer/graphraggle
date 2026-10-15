"""Transcript preparation: contextual chunk headers + speaker-turn normalisation.

Covers app/transcript_prep.py (pure), its wiring behind
``TRANSCRIPT_PREP_ENABLED`` (default off) in ``worker.submit_file``, and the
experiment-level ``transcript_prep`` key in scripts/overnight_loop.py (that test
lives in test_overnight_loop.py).

All fixtures are synthetic - this suite must never read real corpus or transcript
content. Everything is offline: the LightRAG client and the state store are fakes.
"""

from __future__ import annotations

import asyncio
import hashlib
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

import app.transcript_prep as prep
import app.worker as worker


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── synthetic fixtures: one per transcript dialect, plus non-transcripts ──────

LABEL_TIMESTAMP = """Design review

Alice Chen  0:12
We should ship the bridge change behind a flag.
Bob Nguyen  0:31
Agreed, but the rollback path needs a test first.
Alice Chen  0:48
I will write it tonight; the plan is to cut on Friday.
Carla Diaz  1:02
Friday works, the freeze starts that night anyway.
"""

NAME_COLON = """ALICE: We should ship the bridge change behind a flag.
BOB: Agreed, but the rollback path needs a test first.
CAROL: The plan is to cut on Friday, not Thursday.
ALICE: Noted. I will own the release note.
BOB: The transcript says thgnolling, keep that word exactly.
"""

BRACKET_TIMESTAMP = """[00:01:02] Alice: Kickoff today.
[00:02:15] Bob: I have three blockers.
[00:03:40] Alice: Name them.
[00:04:10] Bob: Env vars, the flaky test, and the stale index.
"""

MULTILINE_SAME_SPEAKER = """Alice: First thing I want to say,
    and I keep talking on the next line.
Bob: Short reply.
Alice: Back to me again,
   with a wrapped continuation.
"""

WEBVTT_INLINE = """WEBVTT

00:00:01.000 --> 00:00:04.000
Alice: We should ship the bridge change behind a flag.

00:00:04.500 --> 00:00:08.000
Bob: Agreed, but the rollback path needs a test first.

00:00:09.000 --> 00:00:12.000
Alice: The plan is to cut on Friday.
"""

SRT_NO_SPEAKERS = """1
00:00:01,000 --> 00:00:04,000
We should ship the bridge change behind a flag.

2
00:00:04,500 --> 00:00:08,000
Agreed, but the rollback path needs a test first.

3
00:00:09,000 --> 00:00:12,000
The plan is to cut on Friday after the freeze check.
"""

UNICODE_SPEAKERS = """José Álvarez  0:05
Nos vamos a Madrid la próxima semana.
Zoë Nakamura-Li  0:22
Der Plan bleibt, wir schneiden Freitag.
José Álvarez  0:40
Vale, y el informe queda para el lunes.
"""

CRLF_TRANSCRIPT = (
    "Alice: first line.\r\nBob: second line.\r\nAlice: third line here.\r\n"
    "Bob: and a fourth one.\r\n"
)

LONG_TURNS = (
    "Alice: " + " ".join(f"alpha{i}." if (i + 1) % 10 == 0 else f"alpha{i}"
                          for i in range(300)) + "\n"
    "Bob: " + " ".join(f"beta{i}." if (i + 1) % 10 == 0 else f"beta{i}"
                       for i in range(300)) + "\n"
    "Alice: " + " ".join(f"gamma{i}." if (i + 1) % 10 == 0 else f"gamma{i}"
                         for i in range(300)) + "\n"
)

# A wall of words with no sentence terminator anywhere: the worst case for this
# preprocessor. It must not be broken mid-phrase or truncated.
LONG_TURNS_NO_SENTENCES = (
    "Alice: " + " ".join(f"alpha{i}" for i in range(400)) + "\n"
    "Bob: " + " ".join(f"beta{i}" for i in range(400)) + "\n"
    "Alice: " + " ".join(f"gamma{i}" for i in range(400)) + "\n"
)

POLICY_DOC = """# Deployment policy

Every change ships behind a flag.
Rollback must be rehearsed before release.
Manager: approval is required for emergency deploys.
Note: this document is not a transcript.
"""

README_STEPS = """Install: run the package installer once
Build: run the build target once
Test: run the test target once
Docs: open the readme file for details
"""

INTERVIEW_QA = """Q: Why did the migration fail?
A: The schema check was still manual.
Q: Who owns the fix now?
A: The platform team took it this week.
"""

NO_SPEAKERS_PROSE = """The migration went late because the schema check was manual.
We lost two days on the rollback rehearsal as well.
Action items are tracked in the board, not here.
"""


def _subsequence(needles, haystack) -> bool:
    it = iter(haystack)
    return all(any(needle == item for item in it) for needle in needles)


class TestDetection(unittest.TestCase):
    """Only real transcripts are touched; everything else comes back identical."""

    TRANSCRIPT_DIALECTS = {
        "label+timestamp": LABEL_TIMESTAMP,
        "name-colon": NAME_COLON,
        "bracket+timestamp": BRACKET_TIMESTAMP,
        "multiline-speaker": MULTILINE_SAME_SPEAKER,
        "webvtt-inline-speakers": WEBVTT_INLINE,
        "srt-no-speakers": SRT_NO_SPEAKERS,
        "unicode": UNICODE_SPEAKERS,
        "crlf": CRLF_TRANSCRIPT,
        "long-turns": LONG_TURNS,
    }
    NOT_TRANSCRIPTS = {
        "policy-doc": POLICY_DOC,
        "readme-steps": README_STEPS,
        "interview-qa-labels": INTERVIEW_QA,
        "prose-notes": NO_SPEAKERS_PROSE,
        "empty": "",
        "whitespace": "   \n\n  \n",
        "single-line": "Meeting notes for today.",
    }

    def test_transcript_dialects_are_detected(self):
        for name, text in self.TRANSCRIPT_DIALECTS.items():
            result = prep.prepare_transcript(text, filename=f"{name}_2026-10-09.txt")
            self.assertTrue(result.detected, f"{name}: {result.reason}")
            self.assertGreaterEqual(result.headers_inserted, 1)

    def test_non_transcripts_come_back_byte_identical(self):
        for name, text in self.NOT_TRANSCRIPTS.items():
            result = prep.prepare_transcript(text, filename=f"{name}_2026-10-09.txt")
            self.assertFalse(result.detected, f"{name} was detected")
            self.assertEqual(result.text, text, f"{name} was modified")

    def test_single_voice_recording_is_detected(self):
        text = "\n".join(f"Alice: statement number {i}." for i in range(6)) + "\n"
        result = prep.prepare_transcript(text, filename="standup.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(result.speakers, ("Alice",))


class TestNormalisation(unittest.TestCase):
    """Turn shape, merging, and the promise that words are never changed."""

    def test_turns_become_speaker_colon_text(self):
        result = prep.prepare_transcript(BRACKET_TIMESTAMP, filename="kickoff.txt")
        self.assertIn("Alice: Kickoff today.", result.text)
        self.assertIn("Bob: I have three blockers.", result.text)
        self.assertNotIn("[00:01:02]", result.text, "cue timestamps are stripped")

    def test_consecutive_lines_of_one_speaker_merge_into_one_turn(self):
        result = prep.prepare_transcript(MULTILINE_SAME_SPEAKER, filename="wrap.txt")
        self.assertIn(
            "Alice: First thing I want to say, and I keep talking on the next line.",
            result.text,
        )
        self.assertEqual(result.turns, 3)

    def test_garbled_speech_to_text_words_pass_through(self):
        result = prep.prepare_transcript(NAME_COLON, filename="garbled.txt")
        self.assertIn("thgnolling", result.text)

    def test_crlf_and_unicode_survive(self):
        crlf = prep.prepare_transcript(CRLF_TRANSCRIPT, filename="crlf.txt")
        self.assertNotIn("\r", crlf.text)
        uni = prep.prepare_transcript(UNICODE_SPEAKERS, filename="unicode.txt")
        self.assertIn("José Álvarez:", uni.text)
        self.assertIn("Nos vamos a Madrid la próxima semana.", uni.text)

    def test_every_non_marker_word_survives_in_order(self):
        for name, text in TestDetection.TRANSCRIPT_DIALECTS.items():
            result = prep.prepare_transcript(text, filename=f"{name}_2026-10-09.txt")
            source = prep.word_tokens(text)
            out = prep.word_tokens(result.text) + prep.word_tokens(" ".join(prep.headers_only(result.text)))
            self.assertTrue(
                _subsequence(source, out),
                f"{name}: a word token was lost or reordered",
            )

    def test_idempotent_on_labelled_dialects(self):
        for name, text in TestDetection.TRANSCRIPT_DIALECTS.items():
            if name == "srt-no-speakers":
                continue  # covered below: cue markers are gone, so it is not re-detectable
            once = prep.prepare_transcript(text, filename=f"{name}_2026-10-09.txt",
                                           header_every_tokens=60)
            twice = prep.prepare_transcript(once.text, filename=f"{name}_2026-10-09.txt",
                                            header_every_tokens=60)
            self.assertTrue(twice.detected, f"{name}: second pass declined ({twice.reason})")
            self.assertEqual(twice.text, once.text, f"{name}: not idempotent")
            self.assertEqual(twice.headers_inserted, once.headers_inserted)

    def test_caption_only_output_is_returned_unchanged_on_second_pass(self):
        # Cue markers are stripped, so the prepared text of a speaker-less caption
        # file no longer looks transcript-shaped. The output is still stable - it
        # comes back identical - but it is not re-headered. Documented limitation.
        once = prep.prepare_transcript(SRT_NO_SPEAKERS, filename="captions.txt",
                                       header_every_tokens=60)
        twice = prep.prepare_transcript(once.text, filename="captions.txt",
                                        header_every_tokens=60)
        self.assertTrue(once.detected)
        self.assertFalse(twice.detected)
        self.assertEqual(twice.text, once.text)

    def test_overlong_turn_is_headered_internally(self):
        once = prep.prepare_transcript(LONG_TURNS, filename="long.txt",
                                       header_every_tokens=60)
        headers = prep.headers_only(once.text)
        self.assertGreater(len(headers), 3, "a turn longer than 2 budgets must split at sentences")
        self.assertTrue(_subsequence(prep.word_tokens(LONG_TURNS),
                                     prep.word_tokens(once.text)))
        self.assertIn("Alice: alpha0", once.text)
        # Every piece of a split monologue keeps the real speaker's label; no name
        # is invented and no piece is left anonymous.
        content_lines = [
            line.strip() for line in once.text.splitlines()
            if line.strip() and not prep._HEADER_RE.match(line.strip())
        ]
        self.assertTrue(all(line.startswith("Alice: ") or line.startswith("Bob: ")
                            for line in content_lines), content_lines[:3])
        self.assertGreater(once.text.count("Alice:"), 2)

    def test_turn_with_no_sentence_terminator_is_never_broken_or_truncated(self):
        once = prep.prepare_transcript(LONG_TURNS_NO_SENTENCES, filename="wall.txt",
                                       header_every_tokens=60)
        self.assertEqual(once.headers_inserted, 3, "one header per turn, nothing invented")
        self.assertTrue(_subsequence(prep.word_tokens(LONG_TURNS_NO_SENTENCES),
                                     prep.word_tokens(once.text)))
        self.assertIn("alpha399", once.text, "nothing is truncated")


class TestHeaders(unittest.TestCase):
    def test_title_and_date_come_from_the_file_name(self):
        result = prep.prepare_transcript(NAME_COLON, filename="q4_design_review_2026-10-09.txt")
        self.assertEqual(result.title, "q4 design review")
        self.assertEqual(result.date, "2026-10-09")
        header = prep.headers_only(result.text)[0]
        self.assertIn("Meeting: q4 design review", header)
        self.assertIn("Date: 2026-10-09", header)

    def test_compact_date_in_name_is_normalised(self):
        result = prep.prepare_transcript(NAME_COLON, filename="standup_20261009.txt")
        self.assertEqual(result.date, "2026-10-09")

    def test_absent_fields_are_omitted_never_invented(self):
        result = prep.prepare_transcript(NAME_COLON, filename="no_metadata.txt")
        self.assertIsNone(result.date)
        header = prep.headers_only(result.text)[0]
        self.assertNotIn("Date:", header)
        self.assertIn("Meeting: no metadata", header)

    def test_content_never_supplies_metadata(self):
        text = "Alice: the meeting is called Project Falcon on 2030-01-01.\n" + NAME_COLON
        result = prep.prepare_transcript(text, filename="plain.txt")
        self.assertNotIn("Falcon", prep.headers_only(result.text)[0])
        self.assertNotIn("2030-01-01", prep.headers_only(result.text)[0])

    def test_part_numbering_and_cadence(self):
        result = prep.prepare_transcript(LONG_TURNS, filename="long.txt",
                                         header_every_tokens=100)
        headers = prep.headers_only(result.text)
        self.assertGreaterEqual(len(headers), 3)
        self.assertEqual(headers[0].rsplit("| ", 1)[1], f"Part 1/{len(headers)}]")
        self.assertEqual(headers[-1].rsplit("| ", 1)[1], f"Part {len(headers)}/{len(headers)}]")

    def test_speakers_listed_observed_not_invented(self):
        result = prep.prepare_transcript(NAME_COLON, filename="speakers.txt")
        header = prep.headers_only(result.text)[0]
        self.assertIn("Speakers: ALICE, BOB, CAROL", header)
        self.assertNotIn("Dave", header)

    def test_default_cadence_puts_many_headers_per_light_rag_chunk(self):
        # Production LightRAG chunks at 1200 tokens; the default budget is 200,
        # so a chunk-sized window of prepared text must hold several headers.
        result = prep.prepare_transcript(LONG_TURNS, filename="cadence.txt")
        self.assertGreaterEqual(result.headers_inserted, 4)
        self.assertLess(prep.estimate_tokens(result.text) // result.headers_inserted, 400)

    def test_every_chunk_sized_window_holds_a_header(self):
        """Simulate LightRAG's fixed cuts over prepared text.

        Production LightRAG cuts 1200 tokens with 100 overlap. tiktoken is not in
        this venv, so windows are measured in app.transcript_prep's own
        over-counted estimate (3.0 chars/token): if coverage holds under the
        over-count it holds under the real tokenizer, which produces LARGER
        windows in our units.
        """
        result = prep.prepare_transcript(LONG_TURNS, filename="coverage.txt")
        lines = result.text.splitlines()
        chunk_tokens = 1200
        windows: list[list[str]] = []
        window: list[str] = []
        used = 0
        for line in lines:
            line_tokens = prep.estimate_tokens(line)
            if window and used + line_tokens > chunk_tokens:
                windows.append(window)
                window, used = [], 0
            window.append(line)
            used += line_tokens
        if window:
            windows.append(window)
        self.assertGreater(len(windows), 1, "fixture must span more than one chunk")
        unheadered = [
            window for window in windows
            if not any(prep._HEADER_RE.match(line.strip()) for line in window)
        ]
        self.assertEqual(unheadered, [], "a chunk-sized window was cut with no context header")

    def test_no_transcript_produces_no_headers(self):
        result = prep.prepare_transcript(POLICY_DOC, filename="policy.txt")
        self.assertEqual(prep.headers_only(result.text), [])


class _FakeStateStore:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    async def get_document_by_path(self, path: str):
        doc = self.docs.get(path)
        return dict(doc) if doc else None

    async def get_documents_by_sha256(self, sha256: str) -> list[dict]:
        return [dict(doc) for doc in self.docs.values() if doc.get("sha256") == sha256]

    async def update_document_state(self, path: str, **kwargs) -> None:
        self.docs.setdefault(path, {"path": path}).update(kwargs)

    async def upsert_document(self, **kwargs) -> None:
        self.docs.setdefault(kwargs["path"], {"path": kwargs["path"]}).update(kwargs)

    async def increment_retry(self, path: str, **kwargs) -> int:
        doc = self.docs.get(path)
        if not doc:
            return 0
        doc["retry_count"] = int(doc.get("retry_count") or 0) + 1
        return doc["retry_count"]

    def meta(self, path: str) -> dict:
        block = json.loads(self.docs.get(path, {}).get("warnings_json") or "{}").get("worker")
        return block if isinstance(block, dict) else {}


class TestWorkerWiring(unittest.TestCase):
    """Flag off must be byte-identical; flag on must not change the content hash."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.src = self.root / "source_docs"
        self.src.mkdir(parents=True)
        self.store = _FakeStateStore()
        self.post_json = AsyncMock(return_value={"track_id": "trk-1"})
        self.post_file = AsyncMock(return_value={"track_id": "trk-1"})
        self.patcher = patch.multiple(
            worker.settings,
            source_docs_dir=str(self.src),
            uploads_dir=str(self.root / "uploads"),
            transcript_prep_enabled=False,
            transcript_prep_header_every_tokens=60,
        )
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def _patches(self):
        return patch.multiple(
            worker,
            get_document_by_path=self.store.get_document_by_path,
            get_documents_by_sha256=self.store.get_documents_by_sha256,
            update_document_state=self.store.update_document_state,
            upsert_document=self.store.upsert_document,
            increment_retry=self.store.increment_retry,
        )

    def _submit(self, name: str, content: str) -> dict:
        path = self.src / name
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        self.store.docs[str(path)] = {"path": str(path), "sha256": digest, "status": "accepted",
                                      "company": None, "filename": name,
                                      "original_filename": name, "warnings_json": None}
        with self._patches(), patch.object(worker.client, "post_json", self.post_json), \
             patch.object(worker.client, "post_file", self.post_file):
            _run_async(worker.submit_file(path, "filesystem", digest))
        return {"path": str(path), "digest": digest,
                "payload": self.post_json.await_args.args[1]["text"]}

    def test_flag_off_payload_is_byte_identical_to_today(self):
        expected = worker.normalize_text_submission_payload(LABEL_TIMESTAMP)
        got = self._submit("standup_2026-10-09.txt", LABEL_TIMESTAMP)
        self.assertEqual(got["payload"], expected)
        self.assertNotIn("transcript_prep", self.store.meta(got["path"]))

    def test_flag_on_adds_headers_without_touching_the_dedupe_hash(self):
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            got = self._submit("standup_2026-10-09.txt", LABEL_TIMESTAMP)
        self.assertIn("[Meeting: standup | Date: 2026-10-09", got["payload"])
        self.assertIn("Alice Chen: We should ship the bridge change behind a flag.",
                      got["payload"])
        row = self.store.docs[got["path"]]
        self.assertEqual(row["sha256"], got["digest"], "hash must be the ORIGINAL bytes")
        self.assertEqual(row["content_hash"], got["digest"])

    def test_provenance_is_recorded_on_the_row(self):
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            got = self._submit("standup_2026-10-09.txt", LABEL_TIMESTAMP)
        meta = self.store.meta(got["path"])
        self.assertEqual(meta.get("transcript_prep"), "applied")
        self.assertGreaterEqual(meta.get("transcript_prep_headers"), 1)
        self.assertEqual(meta.get("transcript_prep_speakers"), ["Alice Chen", "Bob Nguyen", "Carla Diaz"])

    def test_non_transcript_with_flag_on_is_submitted_unchanged(self):
        expected = worker.normalize_text_submission_payload(POLICY_DOC)
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            got = self._submit("policy.txt", POLICY_DOC)
        self.assertEqual(got["payload"], expected)
        meta = self.store.meta(got["path"])
        self.assertEqual(meta.get("transcript_prep"), "skipped")
        self.assertTrue(meta.get("transcript_prep_reason"))

    def test_repreparing_an_already_prepared_payload_changes_nothing(self):
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            first = self._submit("standup_2026-10-09.txt", LABEL_TIMESTAMP)
        again = prep.prepare_transcript(first["payload"], filename="standup_2026-10-09.txt",
                                        header_every_tokens=60)
        self.assertEqual(again.text, first["payload"])


if __name__ == "__main__":
    unittest.main()
