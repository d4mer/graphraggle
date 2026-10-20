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
import re
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

# A key/value list: field names head the lines, values follow them, and nobody is
# speaking. Grouped by field, which is how such a list is written by hand.
KEY_VALUE_GROUPED = """Vendor: depot 0.
Vendor: depot 1.
Vendor: depot 2.
Contact: Maria Lopez.
Contact: Peter Hall.
Status: closed.
Status: open.
"""

# The same list with one field, which is the shape that reads as a one-voice
# recording to a label grammar.
KEY_VALUE_ONE_FIELD = "".join(f"Vendor: depot {index}.\n" for index in range(6))

# Field names no lexicon knows, so only the shape can give it away: every field's
# lines sit in one block, and no field ever comes back after another spoke.
KEY_VALUE_UNKNOWN_FIELDS = """Depota: north of the river.
Depota: south of the river.
Lanea: east gate.
Lanea: west gate.
Tariffa: flat per mile.
Tariffa: flat per lane.
"""


def _content_tokens(text: str) -> list[str]:
    """Words of the content lines, with each line's own speaker label removed.

    Used to prove nothing is split, lost, reordered or truncated: a split turn
    repeats its label on every piece, so dropping the label keeps the comparison
    honest (extra labels cannot mask a missing word).
    """
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or prep._HEADER_RE.match(line):
            continue
        match = (re.match(r"^\(([^()]+)\)[ \t]+(.*)$", line)
                 or re.match(r"^([^\W\d_][\w\u2019.\-]*(?:[ \t]+[^\W\d_][\w\u2019.\-]*){0,2})[ \t]*:[ \t](.*)$", line))
        out.extend(prep.word_tokens(match.group(2) if match else line))
    return out


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

    def test_a_key_value_list_is_left_untouched(self):
        """Field names are not speakers: nothing is prepared, and the reason says why."""
        for name, text in (("grouped", KEY_VALUE_GROUPED),
                           ("one-field", KEY_VALUE_ONE_FIELD),
                           ("unknown-fields", KEY_VALUE_UNKNOWN_FIELDS)):
            result = prep.prepare_transcript(text, filename="Depot_list.txt")
            with self.subTest(name):
                self.assertFalse(result.detected, result.reason)
                self.assertFalse(result.applied)
                self.assertEqual(result.text, text, "the file was modified")
                self.assertTrue("key-value-list" in result.reason
                                or "field-key-labels" in result.reason,
                                f"reason did not name it: {result.reason}")

    def test_a_grouped_field_list_is_named_as_a_list_even_when_repeated(self):
        result = prep.prepare_transcript(KEY_VALUE_UNKNOWN_FIELDS,
                                         filename="Depot_list.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertEqual(result.labels_parsed, 6)
        self.assertIn("key-value-list", result.reason)

    def test_field_key_labels_are_counted_for_the_diagnosis(self):
        result = prep.prepare_transcript(KEY_VALUE_ONE_FIELD, filename="Depot_list.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertEqual(result.labels_parsed, 0)
        self.assertEqual(result.speakers, ())
        self.assertIn("field-key-labels", result.reason)

    def test_a_timestamped_key_value_list_is_named_for_its_field_keys(self):
        """The diagnosis pattern must interpolate _TS, not carry a literal "{_TS}"."""
        text = "".join(f"[00:{index:02d}] Vendor: depot {index}.\n" for index in range(6))
        result = prep.prepare_transcript(text, filename="Depot_list.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertFalse(result.applied)
        self.assertEqual(result.text, text, "the file was modified")
        self.assertEqual(result.labels_parsed, 0)
        self.assertIn("field-key-labels", result.reason)
        for line in ("Vendor: depot 0.", ">> Vendor: depot 0.", "   Vendor: depot 0.",
                     "[00:01] Vendor: depot 0.", "[00:01:04.500] Vendor: depot 0.",
                     "00:01 Vendor: depot 0."):
            with self.subTest(line=line):
                match = prep._FIELD_KEY_LINE.match(line)
                self.assertIsNotNone(match, "the line shape is not recognised")
                self.assertEqual(match.group("name"), "Vendor")

    def test_a_voice_that_returns_after_another_still_makes_a_transcript(self):
        """The list rule must not eat a transcript where each speaker has a block."""
        text = """Alice: We should ship the bridge change behind a flag.
Alice: The rollback path needs a test before the freeze.
Alice: I will write it tonight, the plan is to cut on Friday.
Bob: Agreed on the flag, and the freeze starts that night anyway.
Bob: I will own the release note and the rehearsal on Monday.
Bob: Nothing else blocks the cut as far as I can tell today.
"""
        result = prep.prepare_transcript(text, filename="standup_2026-10-09.txt")
        self.assertTrue(result.detected, result.reason)


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

    def test_terminator_free_turn_is_broken_at_whitespace_not_left_header_free(self):
        """Changed on purpose: a header-free 7,000-token wall was the coverage hole.

        The old rule ("never break a turn that has no sentence terminator") left
        every chunk inside such a turn without any context. Now the break falls at
        whitespace between whole words: words are still never split, lost,
        reordered or truncated, and a header lands inside the run.
        """
        once = prep.prepare_transcript(LONG_TURNS_NO_SENTENCES, filename="wall.txt",
                                       header_every_tokens=60)
        self.assertGreater(once.headers_inserted, 3, "headers must land inside the run")
        self.assertTrue(_subsequence(prep.word_tokens(LONG_TURNS_NO_SENTENCES),
                                     prep.word_tokens(once.text)))
        self.assertIn("alpha399", once.text, "nothing is truncated")
        # No word was cut in half: strip headers and the leading label from every
        # content line and the remaining words must be exactly the source words.
        self.assertEqual(_content_tokens(once.text), _content_tokens(LONG_TURNS_NO_SENTENCES))
        # Coverage: no header-free stretch bigger than the budget.
        gaps, current = [], 0
        for line in once.text.splitlines():
            if prep._HEADER_RE.match(line.strip()):
                gaps.append(current)
                current = 0
            else:
                current += prep.estimate_tokens(line)
        gaps.append(current)
        self.assertLessEqual(max(gaps[1:-1]), 2 * 60, "a chunk-sized gap survived")


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

    def test_the_paren_dialect_keeps_its_shape_through_the_worker(self):
        """The corpus dialect is not rewritten to "Name:" on the production path."""
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            got = self._submit("Kickoff_2026-10-09-transcript.txt", PAREN_LABELS)
        self.assertIn("[Meeting: Kickoff | Date: 2026-10-09", got["payload"])
        self.assertIn("(Ana) We should ship the bridge change behind a flag.", got["payload"])
        self.assertNotIn("Ana:", got["payload"])
        row = self.store.docs[got["path"]]
        self.assertEqual(row["sha256"], got["digest"], "hash must be the ORIGINAL bytes")
        self.assertEqual(row["content_hash"], got["digest"])
        self.assertEqual(self.store.meta(got["path"]).get("transcript_prep"), "applied")

    def test_flag_off_sends_a_paren_dialect_file_byte_identical(self):
        expected = worker.normalize_text_submission_payload(PAREN_LABELS)
        got = self._submit("Kickoff_2026-10-09-transcript.txt", PAREN_LABELS)
        self.assertEqual(got["payload"], expected)
        self.assertNotIn("transcript_prep", self.store.meta(got["path"]))



# ── the real corpus shape: "(Label) text", no timestamps, no colons ───────────
# Hermes' content-free report on the actual corpus: every transcript uses a
# parenthesised label, no timestamps, long-tailed turns (p90 65-155 tokens, max
# 2201, one file with a single 7,661-token turn), 40-51% of turns under 10 tokens.
# These fixtures are synthetic but shaped to that description.

PAREN_LABELS = """(Ana) We should ship the bridge change behind a flag.
(Ben) Agreed, but the rollback path needs a test first.
(Ana) The rehearsal is Thursday, (see above) for the room.
(Speaker 1) Thursday afternoon works for me.
(Dr Smith) Then the freeze window is the only risk left.
(Ana) Noted, I will write the announcement tonight.
(Ben) Thursday then.
"""

PAREN_CONTINUATION = """(Ana) First thing I want to say,
and I keep talking on the next line.
(Ben) Short reply here.
(Ana) Another turn from Ana,
still the same turn, no label.
(Ben) Understood.
(Ana) One more from Ana.
(Cara) I have nothing to add.
"""

NON_TRANSCRIPT_PARENS = {
    "prose cross-reference": """The result (see above) is stable under the freeze.
See (see above) for the rehearsal plan of the same week.
Option (1) is cheaper and option (2) is safer for us.
We shipped (1) on Friday and rolled (2) back on Monday.
""",
    "enumerated list": "(a) first item of the list\n(b) second item of the list\n"
                       "(c) third item of the list\n(d) fourth item of the list\n"
                       "(e) fifth item of the list\n",
    "figure reference": "(Fig 3) shows the layout of the deck.\n(Fig 3) is cited twice below.\n"
                        "(Fig 3) again here for the same point.\n(Fig 3) and the notes agree.\n",
    "parenthetical asides": """Deploy Friday (and then we agreed on the rollback).
Scope Friday (the plan is unchanged, see the note above).
Deploy Friday (same as last week, nothing new here).
Scope Friday (we both said yes to this one).
""",
}

# Stage directions are content, not voices. This fixture IS a two-voice recording
# (Ana speaks twice), so it is now detected - the guarantee is that the directions
# never become speakers and their words survive.
STAGE_DIRECTIONS = """(Ana) Hello everyone.
(laughter)
(Ben) Nice to see you.
(silence)
(Ana) Thanks for coming.
(laughter)
"""


class TestParenthesisDialect(unittest.TestCase):
    """`(Label) text`: detected, kept in its own shape, prose parens refused."""

    def test_detected_and_written_back_in_the_same_shape(self):
        result = prep.prepare_transcript(PAREN_LABELS, filename="Kickoff_2026-10-09-transcript.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertIn("paren", result.reason)
        self.assertIn("Ana", result.speakers)
        self.assertIn("Speaker 1", result.speakers)
        self.assertIn("Dr Smith", result.speakers)
        for line in result.text.splitlines():
            stripped = line.strip()
            if not stripped or prep._HEADER_RE.match(stripped):
                continue
            self.assertTrue(stripped.startswith("("), f"label rewritten: {stripped[:40]}")
            self.assertNotIn(": ", stripped.split(") ", 1)[-1][:20])
        self.assertNotIn("Ana:", result.text, "the corpus form must not be rewritten to Name:")
        self.assertIn("(Ana) The rehearsal is Thursday, (see above) for the room.", result.text,
                      "a mid-line prose paren survives as content")
        self.assertTrue(_subsequence(prep.word_tokens(PAREN_LABELS), prep.word_tokens(result.text)))

    def test_idempotent_on_the_paren_shape(self):
        once = prep.prepare_transcript(PAREN_LABELS, filename="Kickoff_2026-10-09-transcript.txt")
        twice = prep.prepare_transcript(once.text, filename="Kickoff_2026-10-09-transcript.txt")
        self.assertTrue(twice.detected, "the output must still be transcript-shaped")
        self.assertEqual(twice.text, once.text)
        self.assertEqual(twice.headers_inserted, once.headers_inserted)

    def test_label_less_continuation_belongs_to_the_previous_turn(self):
        result = prep.prepare_transcript(PAREN_CONTINUATION, filename="sync.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertIn("(Ana) First thing I want to say, and I keep talking on the next line.",
                      result.text)
        self.assertEqual(_content_tokens(result.text), _content_tokens(PAREN_CONTINUATION))

    def test_prose_parentheses_are_never_treated_as_speakers(self):
        for name, text in NON_TRANSCRIPT_PARENS.items():
            with self.subTest(name):
                result = prep.prepare_transcript(text, filename="notes.txt")
                self.assertFalse(result.detected, f"{name}: {result.reason}")
                self.assertEqual(result.text, text, "byte-identical when unsure")

    def test_stage_directions_stay_content_even_when_the_file_is_detected(self):
        """Deliberate change (brief item 2): two repeats of one label now detect.

        The fixture has two real voices, so preparing it is correct; the guarantee
        that matters is that (laughter)/(silence) never become speakers.
        """
        result = prep.prepare_transcript(STAGE_DIRECTIONS, filename="sync.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(set(result.speakers), {"Ana", "Ben"})
        for direction in ("laughter", "silence"):
            self.assertNotIn(direction, [speaker.lower() for speaker in result.speakers])
            self.assertIn(f"({direction})", result.text, "the direction's words survive")
        self.assertEqual(result.labels_parsed, 3)
        self.assertEqual(result.labels_rejected, 3)
        self.assertEqual(result.labels_unparseable, 0)

    def test_a_paren_label_needs_two_repeats_not_three(self):
        """F3: the old bar of three discarded real two-voice recordings."""
        text = """(Ann) First point from Ann about the deploy.
(Bob) Reply to Ann about the same deploy.
(Ann) Second point from Ann about the freeze.
(Bob) Second reply from Bob about it.
(Cara) A third voice says something else.
"""
        result = prep.prepare_transcript(text, filename="notes.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertIn("paren", result.reason)
        self.assertEqual(result.labels_parsed, 5)
        self.assertEqual(set(result.speakers), {"Ann", "Bob", "Cara"})

    def test_a_single_recurring_parenthetical_is_still_not_a_transcript(self):
        """Widening the grammar must not turn a writing habit into speakers."""
        text = """(ibid) the same source as before.
(ibid) repeated citation in this note.
(ibid) once more for the third claim.
(ibid) and again for the closing line.
"""
        result = prep.prepare_transcript(text, filename="notes.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertEqual(result.labels_parsed, 0)
        self.assertEqual(result.labels_rejected, 4)

    def test_titles_come_from_the_stem_and_never_invent_a_date(self):
        cases = {
            "Project_Kickoff_2026-10-09-transcript.txt": ("Project Kickoff", "2026-10-09"),
            "why_the_deploy_failed-transcript.txt": ("why the deploy failed", None),
            "Standup_20261009_transcript.txt": ("Standup", "2026-10-09"),
            "transcript.txt": (None, None),
        }
        for filename, (title, date) in cases.items():
            with self.subTest(filename):
                self.assertEqual(prep._metadata_from_name(filename), (title, date))
                result = prep.prepare_transcript(PAREN_LABELS, filename=filename)
                header = prep.headers_only(result.text)[0]
                if title:
                    self.assertIn(f"Meeting: {title}", header)
                else:
                    self.assertNotIn("Meeting:", header)
                self.assertEqual("Date: " in header, date is not None)


class TestRealisticTurnShapes(unittest.TestCase):
    """40-51% tiny turns and one 7,000-token turn, from Hermes' shape report."""

    @staticmethod
    def _mix(turns: int = 150) -> str:
        """Turn lengths from Hermes: 40-51% under 10 tokens, p50 12-18, p90 65-155."""
        speakers = ["(Ana)", "(Ben)", "(Cara)"]
        lines = []
        for index in range(turns):
            speaker = speakers[index % len(speakers)]
            if index % 5 == 0:
                lines.append(f"{speaker} " + " ".join(f"point{index}item{j}" for j in range(24)))
            elif index % 2 == 0:
                lines.append(f"{speaker} " + " ".join(f"note{index}item{j}" for j in range(5)))
            else:
                lines.append(f"{speaker} ok, sure, thanks, fine, yeah.")
        return "\n".join(lines) + "\n"

    def test_headers_follow_the_token_cadence_not_the_turn_count(self):
        text = self._mix()
        cadence = 200
        result = prep.prepare_transcript(text, filename="standup.txt", header_every_tokens=cadence)
        self.assertTrue(result.detected, result.reason)
        total = prep.estimate_tokens(text)
        self.assertGreater(result.turns, 140)
        self.assertLess(result.headers_inserted, result.turns / 3,
                        "a header per sub-ten-token turn would be noise")
        self.assertGreaterEqual(result.headers_inserted, total // (3 * cadence),
                                "far fewer headers than the cadence means uncovered chunks")
        self.assertLessEqual(result.headers_inserted, total // (cadence // 3),
                             "far more headers than the cadence wastes payload")
        # Tiny turns are grouped: one header covers several labelled lines.
        segment, widest = 0, 0
        for line in result.text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if prep._HEADER_RE.match(stripped):
                widest = max(widest, segment)
                segment = 0
            else:
                segment += 1
        self.assertGreaterEqual(max(widest, segment), 3, "tiny turns were not grouped")
        self.assertEqual(_content_tokens(result.text), _content_tokens(text))

    @staticmethod
    def _wall(words: int = 7000) -> str:
        # One turn of `words` words with no sentence terminator at all, spread over
        # two lines, plus enough labelled lines from two speakers to be detected.
        run = " ".join(f"fillerword{n}" for n in range(words))
        head, tail = run[: len(run) // 2], run[len(run) // 2:]
        return ("(Ana) Let me explain the whole thing now " + head + "\n" + tail + "\n"
                "(Ben) Short reply.\n(Ana) More from Ana.\n(Ben) And from Ben.\n"
                "(Ana) Closing from Ana.\n")

    def test_a_terminator_free_wall_of_words_still_gets_headers(self):
        text = self._wall()
        cadence = 200
        result = prep.prepare_transcript(text, filename="wall.txt", header_every_tokens=cadence)
        self.assertTrue(result.detected, result.reason)
        total = prep.estimate_tokens(text)
        self.assertGreater(total, 5000, "fixture must be the degenerate size")
        self.assertGreater(result.headers_inserted, total // (4 * cadence),
                           "headers must land inside the run, not only before it")
        # Words intact, in order, none split, nothing dropped.
        self.assertEqual(_content_tokens(result.text), _content_tokens(text))
        for line in result.text.splitlines():
            stripped = line.strip()
            if not stripped or prep._HEADER_RE.match(stripped):
                continue
            self.assertTrue(stripped.startswith("(Ana) ") or stripped.startswith("(Ben) "),
                            f"a piece lost its speaker label: {stripped[:40]}")
            self.assertLessEqual(prep.estimate_tokens(stripped), 2 * cadence + 40,
                                 "a piece is far bigger than the cadence")
        gaps, current = [], 0
        for line in result.text.splitlines():
            if prep._HEADER_RE.match(line.strip()):
                gaps.append(current)
                current = 0
            else:
                current += prep.estimate_tokens(line)
        gaps.append(current)
        self.assertLessEqual(max(gaps[1:-1]), 2 * cadence, "a header-free stretch survived")



class TestLabelGrammarCoversTheCorpus(unittest.TestCase):
    """F3: the parser was narrower than the dialect, so whole documents were skipped."""

    DIALECT = """(Dr. Lee) We need the rollback tested before Friday.
(Ana, Acme Legal) The contract wording is the only blocker.
(Ben (Acme Finance)) The numbers are agreed on my side.
(Speaker 2) I will take the announcement.
00:12 (Ana) Noted, I will write it tonight.
7 (Ben) Thursday then, with the freeze after.
(J. Smith) One correction on the meeting date.
(Dr. Lee) Good, closing the item now.
"""

    def test_every_shape_the_corpus_uses_becomes_a_speaker(self):
        result = prep.prepare_transcript(self.DIALECT, filename="Vendor_ sync_2026-10-13.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(result.labels_parsed, 8)
        self.assertEqual(result.labels_unparseable, 0)
        for name in ("Dr. Lee", "Ana, Acme Legal", "Ben (Acme Finance)", "Speaker 2",
                     "Ana", "Ben", "J. Smith"):
            self.assertIn(name, result.speakers, f"{name} was not read as a speaker")
        self.assertIn("(Dr. Lee) We need the rollback tested before Friday.", result.text)
        self.assertIn("(Ana, Acme Legal) The contract wording is the only blocker.", result.text)
        self.assertIn("(Ana) Noted, I will write it tonight.", result.text,
                      "a timestamp before the label is cue noise: the words stay, the cue goes")
        self.assertTrue(_subsequence(prep.word_tokens(self.DIALECT), prep.word_tokens(result.text)))
        self.assertEqual(prep.prepare_transcript(result.text, filename="Vendor_ sync_2026-10-13.txt").text,
                         result.text, "the widened shape must still be idempotent")

    def test_labels_differing_only_by_a_role_suffix_stay_distinct_people(self):
        """A fixture whose labels differ only by the role suffix after the comma."""
        doc = """(Ana Reyes, Acme Legal) The clause needs a carve-out.
(Ben Ortiz, Acme Legal) Agreed, and the same for the vendor schedule.
(Ana Reyes, Acme Finance) On my side the numbers are already agreed.
(Cara Ito, Acme Legal) Then we only need the announcement.
(Ben Ortiz, Acme Legal) Thursday works for the schedule.
(Ana Reyes, Acme Legal) Closing it tonight.
"""
        result = prep.prepare_transcript(doc, filename="legal-sync.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(result.labels_unparseable, 0)
        self.assertEqual(result.labels_parsed, 6)
        self.assertEqual(set(result.speakers), {"Ana Reyes, Acme Legal", "Ben Ortiz, Acme Legal",
                                                "Ana Reyes, Acme Finance", "Cara Ito, Acme Legal"})
        labels = [line.split(")", 1)[0][1:] for line in result.text.splitlines()
                  if line.startswith("(")]
        self.assertEqual(set(labels), {"Ana Reyes, Acme Legal", "Ben Ortiz, Acme Legal",
                                      "Ana Reyes, Acme Finance", "Cara Ito, Acme Legal"},
                         "labels collapsed onto one speaker or split apart")

    def test_a_timestamp_or_index_before_the_label_is_parsed_not_dropped(self):
        """The reproduction in F3: anchored parsing rejected '00:12 (Ana) ...'."""
        for prefix in ("00:12 ", "[00:01:02] ", "7 ", "00:01:02,123 "):
            with self.subTest(prefix=prefix):
                doc = (f"{prefix}(Ana) First line here.\n{prefix}(Ben) Second line here.\n"
                       f"{prefix}(Ana) Third line here.\n{prefix}(Ben) Fourth line here.\n")
                result = prep.prepare_transcript(doc, filename="sync.txt")
                self.assertTrue(result.detected, result.reason)
                self.assertEqual(result.labels_parsed, 4)

    def test_prose_parentheticals_are_still_not_speakers_after_widening(self):
        for label in ("(and then we agreed)", "(see the note above)", "(e.g. the plan)",
                      "(Fig. 3)", "(Table 2.)", "(yes)", "(ibid)", "(what did we decide)",
                      "(a) first", "(1) first"):
            with self.subTest(label=label):
                self.assertEqual(prep._classify_paren_label(label[1:-1])[0], None,
                                 f"{label} became a speaker")

    def test_an_eight_word_label_is_the_limit(self):
        eight = "von der Leyen Sanchez Gomez de la Cruz"
        self.assertEqual(prep._classify_paren_label(eight)[1], "ok")
        self.assertEqual(prep._classify_paren_label(eight + " Moreno")[1], "unparseable",
                         "nine name-like words is past the label budget")
        self.assertEqual(prep._classify_paren_label("Ana and Ben")[1], "non-speaker",
                         "a conjunction inside the brackets is prose, not a name")


class TestUnreadableLabelsNeverBorrowSomeoneElsesTurn(unittest.TestCase):
    """F2: a (Label) line that failed validation was appended to the PREVIOUS
    speaker's turn, writing the wrong person into every chunk that covers it."""

    def one_line(self, label: str) -> str:
        return f"({label})"

    def test_an_unreadable_label_stands_as_its_own_turn(self):
        doc = """(Ana) We ship the bridge change behind a flag.
(Dr. Lee.) That label has a stray period, so this parser cannot read it.
(Ben) The rollback path needs a test first.
(Ana) Noted, I will own the release note.
(Ben) Thursday then.
(Ana) Closing tonight.
"""
        result = prep.prepare_transcript(doc, filename="sync.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(result.labels_unparseable, 1)
        self.assertIn("unparseable(1)", result.reason)
        blocks = [block for block in result.text.split("\n\n") if block.strip()]
        offender = [block for block in blocks if "stray period" in block]
        self.assertEqual(len(offender), 1)
        self.assertTrue(offender[0].startswith("(Dr. Lee.)"),
                        "the line must keep its own words, not inherit Ana's label")
        self.assertNotIn("(Ana) (Dr. Lee.)", result.text)
        self.assertNotIn("Dr. Lee", result.speakers, "an unreadable label claims no speaker")
        self.assertTrue(_subsequence(prep.word_tokens(doc), prep.word_tokens(result.text)),
                        "the label's own words still survive")

    def test_a_file_whose_labels_cannot_be_read_is_left_untouched(self):
        doc = """(Ana) First line here.
(Ana/Ben) Two names in one label, unreadable.
(Dr. Lee.) A stray period, unreadable.
(Ana, Acme, Legal, Berlin, Europe) Too many parts, unreadable.
(Ben) Second real line here.
(Ana) Third line from Ana.
(Ben) Fourth line, still readable.
"""
        result = prep.prepare_transcript(doc, filename="sync.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertEqual(result.reason, "unparseable-labels(3)")
        self.assertEqual(result.text, doc, "byte-identical: the safer failure is doing nothing")
        self.assertEqual(result.labels_unparseable, 3)
        self.assertEqual(result.labels_parsed, 4)

    def test_a_couple_of_unreadable_lines_do_not_block_the_whole_file(self):
        doc = """(Ana) We ship the bridge change behind a flag.
(Ben) Rollback needs a test first.
(Ana/Ben) Two names in one label here.
(Ana) Noted, writing the note tonight.
(Ben) Thursday then.
(Ana) Closing tonight.
"""
        result = prep.prepare_transcript(doc, filename="sync.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertEqual(result.labels_unparseable, 1)
        self.assertIn("unparseable(1)", result.reason)

    def test_the_counts_a_skip_is_diagnosed_by_are_on_the_result(self):
        """F3: 'no-recurring-speaker(2 labels)' explained nothing before."""
        doc = """(Ana) One labelled line.
(Ben) Two labelled lines.
(see above) a parenthetical that is not a speaker.
(a) an enumerated item.
(Fig 3) a numbered cross-reference.
""" + "\n".join(f"Continuation prose line {index} with no label at all."
                     for index in range(12)) + "\n"
        result = prep.prepare_transcript(doc, filename="mixed.txt")
        self.assertFalse(result.detected, result.reason)
        self.assertEqual(result.labels_parsed, 2)
        self.assertEqual(result.labels_rejected, 3)
        self.assertEqual(result.labels_unparseable, 0)
        self.assertEqual(result.lines_nonempty, 17)


class TestPrepCountsReachTheWorkerRow(unittest.TestCase):
    def test_label_counts_are_recorded_without_any_text_from_the_file(self):
        text = """(Ana) Ship it behind a flag.
(Dr. Lee.) Unreadable label here.
(Ben) Rollback needs a test.
(Ana) Noted tonight.
(Ben) Thursday then.
(Ana) Closing tonight.
"""
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            prepared, updates = worker.prepare_transcript_payload(Path("Transcripts/x/sync.txt"), text)
        self.assertIn("(Dr. Lee.)", prepared)
        self.assertEqual(updates["transcript_prep"], "applied")
        self.assertEqual(updates["transcript_prep_labels_parsed"], 5)
        self.assertEqual(updates["transcript_prep_labels_unparseable"], 1)
        self.assertEqual(updates["transcript_prep_lines"], 6)
        for value in (updates["transcript_prep_labels_parsed"],
                      updates["transcript_prep_labels_rejected"],
                      updates["transcript_prep_labels_unparseable"],
                      updates["transcript_prep_lines"]):
            self.assertIsInstance(value, int)
        self.assertNotIn("Ship it behind a flag", json.dumps(updates))

    def test_a_skipped_file_still_reports_its_counts(self):
        text = "(Ana) one line\n(Ben) two lines\n" + "prose with no label at all here.\n" * 12
        with patch.object(worker.settings, "transcript_prep_enabled", True):
            _prepared, updates = worker.prepare_transcript_payload(Path("notes.txt"), text)
        self.assertEqual(updates["transcript_prep"], "skipped")
        self.assertEqual(updates["transcript_prep_labels_parsed"], 2)
        self.assertIsInstance(updates["transcript_prep_lines"], int)




class InlineLabelShapes(unittest.TestCase):
    """Labels in the MIDDLE of a line: the monolithic-minutes shape."""

    MINUTES = ("Ana: we renewed the depot lease for another two years. Liang: freight moved "
               "twice and the carrier kept the same lane. Ana: then the tariff question came "
               "back, and the customs desk asked for the origin papers again. Liang: the "
               "papers are with the broker already.")

    def test_a_single_line_of_minutes_becomes_turns(self):
        segments = prep.split_inline_turns(self.MINUTES)
        self.assertIsNotNone(segments)
        self.assertEqual(len(segments), 4)
        self.assertTrue(all(segment.startswith(("Ana:", "Liang:")) for segment in segments))

    def test_the_segments_are_exact_substrings_so_no_word_moves(self):
        segments = prep.split_inline_turns(self.MINUTES)
        self.assertEqual(" ".join(self.MINUTES.split()),
                         " ".join(" ".join(segments).split()))
        for segment in segments:
            self.assertIn(segment, self.MINUTES)

    def test_parenthesised_minutes_split_too(self):
        line = ("(Ana) we renewed the depot lease for two more years. (Liang) freight moved "
                "twice and the carrier kept the lane. (Ana) the tariff came back at the review.")
        segments = prep.split_inline_turns(line)
        self.assertIsNotNone(segments)
        self.assertEqual(len(segments), 3)

    def test_prose_that_happens_to_use_colons_is_left_alone(self):
        shapes = {
            "prose": ("The report: the numbers were flat, and the review: nothing changed at "
                      "all this quarter, despite the note: we asked twice about it."),
            "key_values": ("Cost: 12000. Note: the vendor: depot nine, and the term: 24 "
                           "months of service for the same site."),
            "mid_sentence": ("We agreed that freight: the carrier would hold the lane, and "
                             "that customs: the broker would file on time for the depot."),
            "one_voice": ("Ana: we renewed the depot lease for another two years, and Ana: we "
                          "kept the carrier on the same lane again this year, and Ana: the "
                          "tariff stayed flat afterwards for two more years."),
        }
        for label, text in shapes.items():
            with self.subTest(shape=label):
                self.assertIsNone(prep.split_inline_turns(text))

    def test_a_short_line_is_never_split(self):
        self.assertIsNone(prep.split_inline_turns("Ana: here. Liang: there."))

    def test_a_label_must_not_eat_the_words_in_front_of_the_colon(self):
        # "two years. Liang:" is the trap: a three-word name pattern reads "years. Liang"
        # as the speaker and swallows the sentence.
        line = ("Ana: we renewed the depot lease for another two years. Liang: freight moved "
                "twice and the carrier kept the same lane again this quarter, and Ana: the "
                "tariff came back at the review of the customs papers.")
        segments = prep.split_inline_turns(line)
        self.assertTrue(all(segment.split(":")[0] in ("Ana", "Liang")
                            for segment in segments or [""]))

    def test_the_colon_after_a_parenthesised_name_is_punctuation_not_a_word(self):
        parsed = prep._parse_turn("(Ana): we renewed the depot lease.")
        self.assertIsNotNone(parsed)
        speaker, body, style = parsed
        self.assertEqual((speaker, style), ("Ana", "paren"))
        self.assertEqual(body, "we renewed the depot lease.")

    def test_a_bare_timestamp_label_is_a_turn(self):
        parsed = prep._parse_turn("Ana 12:34")
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed[0], "Ana")




class TestPerDocumentSpeakerSet(unittest.TestCase):
    """A name grammar is per-word; the speaker set is per-document.

    Hermes' unclassified transcripts use "(They)" / "(You)" / "(There)" at line starts,
    strictly alternating. The words must stay rejected in general - "(they) said" is a
    sentence fragment - and be accepted in the file that behaves like a transcript.
    """

    def lines(self, first: str, second: str, count: int = 12) -> list[str]:
        return [f"({first if index % 2 == 0 else second}) the depot carrier lane {index} "
                f"held while the freight tariff moved twice in the customs review"
                for index in range(count)]

    def test_an_alternating_pronoun_pair_is_a_speaker_set(self):
        self.assertEqual(prep.speaker_set_from_alternation(self.lines("They", "You")),
                         frozenset({"they", "you"}))

    def test_a_real_name_paired_with_a_pronoun_rescues_only_the_pronoun(self):
        sequence = prep.paren_label_sequence(self.lines("Ana", "They"))
        kinds = {label: kind for label, kind, _chars in sequence}
        self.assertEqual(kinds["ana"], "ok")
        self.assertEqual(kinds["they"], "non-speaker")
        self.assertEqual(prep.speaker_set_from_alternation(self.lines("Ana", "They")),
                         frozenset({"ana", "they"}))

    def test_stage_directions_alternating_are_not_people(self):
        for pair in (("Cross-talk", "Inaudible"), ("Background noise", "Silence")):
            with self.subTest(labels=pair):
                self.assertEqual(prep.speaker_set_from_alternation(self.lines(*pair)),
                                 frozenset())

    def test_no_pattern_no_rescue(self):
        cases = {
            "too few": self.lines("They", "You", 3),
            "one voice repeating": ["(They) a", "(They) b", "(They) c", "(They) d"],
            "three labels": ["(They) a", "(You) b", ("(There) c"), "(They) d",
                             "(You) e", "(There) f", "(They) g", "(You) h"],
            "no parens at all": ["the depot lane held", "and the tariff moved"],
        }
        for label, lines in cases.items():
            with self.subTest(case=label):
                self.assertEqual(prep.speaker_set_from_alternation(lines), frozenset())

    def test_the_preparer_then_reads_the_pronoun_transcript(self):
        text = "\n".join(self.lines("They", "You")) + "\n"
        result = prep.prepare_transcript(text, filename="Call_2026-04-11.txt")
        self.assertTrue(result.detected, result.reason)
        self.assertIn("They", result.speakers)
        self.assertIn("You", result.speakers)
        def without_headers(body: str) -> str:
            return "\n".join(line for line in body.splitlines()
                              if line.strip() and not prep._HEADER_RE.match(line.strip()))
        self.assertEqual(" ".join(prep.word_tokens(without_headers(text))),
                         " ".join(prep.word_tokens(without_headers(result.text))),
                         "the headers are added; no word of the meeting is changed")

    def test_the_prose_with_parenthetical_pronouns_is_still_not_a_transcript(self):
        text = "\n\n".join(
            f"(they said the depot carrier lane {index} would hold, and the tariff moved) "
            f"the review closed with the same figure twice for period {index}."
            for index in range(10))
        result = prep.prepare_transcript(text, filename="notes.txt")
        self.assertFalse(result.detected, result.reason)

    def test_a_monolithic_pair_of_giant_turns_gets_a_speaker_set(self):
        first = "(They) " + " ".join(f"freight_{index} carrier depot tariff"
                                     for index in range(200))
        second = "(You) " + " ".join(f"wage_{index} shift roster margin"
                                     for index in range(200))
        self.assertGreater(len(first), 2500)
        self.assertEqual(prep.monolithic_speaker_set([first, second]),
                         frozenset({"they", "you"}))
        self.assertEqual(prep.monolithic_speaker_set([first[:400], second[:400]]),
                         frozenset(), "short lines are not a monolithic transcript")



if __name__ == "__main__":
    unittest.main()
