"""Investigation tests for the unanswerable-probe failure shape (GL-08 baseline: 4 of 12
unanswerable probes refused, 8 hallucinated).

Nothing in this file changes gateway behaviour. It pins three things:

1. REPRODUCTION at the gateway stage: an answer that asserts a code present in no
   evidence is returned unchanged, with no support signal anywhere in the response, and
   the payload the gateway sends upstream carries no threshold, no context-only mode and
   no grounding instruction. If a mitigation lands, INVERT ``TestGatewayPassesAnUngroundedAnswerThrough``.
2. The candidate rule (identifier support) and the traps in it: substring matching
   silently passes a code that only appears inside a longer identifier; our own chunker
   re-wraps lines so SENTENCE-level verbatim support is unavailable while identifier-level
   support is; an invisible character in the user's question would false-refuse.
3. The shape of the generated synthetic probe set, including the fact that manifest.json
   carries the answer key and the unanswerable codes -- which matters because that file
   used to be ingested as an ordinary document.

Corpora here are invented by scripts/make_synthetic_company.py into a temp directory. No
test reads source_docs/, lightrag_store/, state/ or any real probe set.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]

_SPEC = importlib.util.spec_from_file_location(
    "unanswerable_grounding", ROOT_DIR / "scripts" / "unanswerable_grounding.py")
harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = harness          # dataclasses resolve annotations via sys.modules
_SPEC.loader.exec_module(harness)

_GEN_SPEC = importlib.util.spec_from_file_location(
    "make_synthetic_company_for_grounding", ROOT_DIR / "scripts" / "make_synthetic_company.py")
gen = importlib.util.module_from_spec(_GEN_SPEC)
assert _GEN_SPEC and _GEN_SPEC.loader
sys.modules[_GEN_SPEC.name] = gen
_GEN_SPEC.loader.exec_module(gen)

from app import api, state_store                           # noqa: E402
from app import lightrag_text                              # noqa: E402
from app.models import QueryRequest                        # noqa: E402

HAS_PPTX = importlib.util.find_spec("pptx") is not None

COMPANY_A = "Northwind Chemicals"
COMPANY_B = "Contoso Pharma"
SEED = 20261101

ABSENT_CODE = "NC-3617-NA-01"
NEAR_MISS_CODES = ["NC-3617-014", "NC-3617-022", "NC-3617-D07"]


def _run(coro):
    return asyncio.run(coro)


def _upstream_response(answer: str, codes_by_reference: list[str]) -> dict:
    """A LightRAG-shaped /query response: an answer plus references that carry content
    (include_chunk_content is True on this path) and no scores."""
    return {
        "response": answer,
        "references": [
            {
                "reference_id": str(index + 1),
                "file_path": f"Northwind Chemicals/transcripts/t{index:02d}.txt",
                "content": f"Reviewer: entry {code} closed with 3 blockers outstanding.",
            }
            for index, code in enumerate(codes_by_reference)
        ],
    }


class TestGatewayPassesAnUngroundedAnswerThrough(unittest.TestCase):
    """REPRODUCTION: today the gateway is a courier between LightRAG and the caller.

    It asks for the chunk content (so it COULD check support), never checks it, and sends
    no gate upstream. This test must be inverted when a grounding gate CHANGES the answer.

    M1 (``GROUNDING_CHECK_ASKED_IDENTIFIER``) has since landed as a flag-only report, so the
    word "grounding" can now appear in a response - but it ships off, and with it off this
    reproduction holds end to end. See tests/test_grounding_gate.py for what the flag does
    and does not do.
    """

    def setUp(self) -> None:
        # The query path reads the state store (company scoping, and the query log).
        # conftest gives STATE_DB_PATH a throwaway value but no schema, so create one.
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-grounding-")
        self._saved_db = state_store.settings.state_db_path
        state_store.settings.state_db_path = str(Path(self._tmp.name) / "state.db")
        _run(state_store.init_db())

    def tearDown(self) -> None:
        state_store.settings.state_db_path = self._saved_db
        self._tmp.cleanup()

    def _call(self, answer: str) -> tuple[object, list[dict]]:
        payloads: list[dict] = []

        async def post_json(path, payload):
            payloads.append(payload)
            return _upstream_response(answer, NEAR_MISS_CODES)

        client = AsyncMock()
        client.post_json = AsyncMock(side_effect=post_json)
        with patch.object(api, "client", client):
            envelope = _run(api.query(QueryRequest(
                query=f"What outcome was recorded under {ABSENT_CODE}?", mode="mix")))
        return envelope, payloads

    def test_the_hallucinated_answer_is_returned_verbatim(self) -> None:
        answer = (f"{ABSENT_CODE} closed at 4 hours 10 minutes with 12 blockers, "
                  f"in line with {NEAR_MISS_CODES[0]}.")
        envelope, _payloads = self._call(answer)
        self.assertTrue(envelope.ok)
        # The asserted code is in no citation, and it still reaches the caller.
        self.assertIn(ABSENT_CODE, envelope.data["answer"])
        self.assertNotIn(ABSENT_CODE, json.dumps(envelope.data["citations"]))

    def test_no_response_field_reports_support(self) -> None:
        envelope, _payloads = self._call(f"{ABSENT_CODE} closed on time.")
        blob = json.dumps({"data": envelope.data, "meta": envelope.meta}, default=str).lower()
        for needle in ("grounding", "unsupported", "unverified", "support_check"):
            self.assertNotIn(needle, blob)
        # The reproduction is the SHIPPED state, not merely the default. If the flag ever
        # ships on, this test must be re-read rather than left green by accident.
        from app.config import Settings
        self.assertIs(Settings().grounding_check_asked_identifier, False,
                      "M1 ships dark; with it on this test's 'grounding' needle must move")

    def test_the_upstream_payload_sets_no_gate(self) -> None:
        """The gateway sends no similarity threshold, no only_need_context, no prompt."""
        _envelope, payloads = self._call(f"{ABSENT_CODE} closed on time.")
        self.assertEqual(len(payloads), 1, "one retrieval pass expected (2 citations, not weak)")
        payload = payloads[0]
        self.assertEqual(payload["mode"], "mix")
        self.assertTrue(payload["include_references"])
        self.assertTrue(payload["include_chunk_content"],
                        "the gateway already asks for the evidence text it never inspects")
        for gate in ("threshold", "score_threshold", "min_score", "only_need_context",
                     "user_prompt", "system_prompt"):
            self.assertNotIn(gate, payload)

    def test_todays_only_refusal_detector_does_not_fire(self) -> None:
        """WEAK_ANSWER_MARKERS is two literal phrases; a confident wrong answer passes."""
        self.assertFalse(api.has_weak_answer_signal(f"{ABSENT_CODE} closed at 4 hours."))
        self.assertTrue(api.has_weak_answer_signal("I do not have enough information to answer."))
        self.assertEqual(len(api.WEAK_ANSWER_MARKERS), 2)

    def test_no_reference_carries_a_score_the_gateway_could_have_used(self) -> None:
        envelope, _payloads = self._call(f"{ABSENT_CODE} closed on time.")
        citations = envelope.data["citations"]
        self.assertTrue(citations, "the near-miss citations are handed to the caller")
        self.assertEqual(api.get_top_retrieval_confidence(citations), 0.0,
                         "citations carry no relevance score, and nothing gates on it anyway")


class TestIdentifierSupportRule(unittest.TestCase):
    """The candidate rule and the ways it can be quietly wrong."""

    def test_fires_when_the_asked_code_is_absent_from_the_evidence(self) -> None:
        evidence = [f"entry {code} closed with 3 blockers" for code in NEAR_MISS_CODES]
        verdict = harness.support_gate(f"What happened under {ABSENT_CODE}?",
                                       f"{ABSENT_CODE} closed with 3 blockers.", evidence)
        self.assertEqual(verdict["verdict"], "no_support_for_asked_identifier")
        self.assertEqual(verdict["asked"], [ABSENT_CODE])
        self.assertEqual(verdict["asserted"], [ABSENT_CODE])

    def test_fires_when_the_answer_asserts_a_code_the_question_never_asked_for(self) -> None:
        evidence = ["the freeze window was reduced to 48 hours"]
        verdict = harness.support_gate("Was the freeze window reduced?",
                                       f"Yes; see {ABSENT_CODE}.", evidence)
        self.assertEqual(verdict["verdict"], "unsupported_identifiers_in_answer")
        self.assertEqual(verdict["asserted_unsupported"], [ABSENT_CODE])

    def test_passes_a_grounded_answer(self) -> None:
        evidence = [f"entry {NEAR_MISS_CODES[0]} closed with 3 blockers"]
        verdict = harness.support_gate(f"What happened under {NEAR_MISS_CODES[0]}?",
                                       f"{NEAR_MISS_CODES[0]} closed with 3 blockers.", evidence)
        self.assertEqual(verdict["verdict"], "supported")
        self.assertEqual(verdict["asserted_unsupported"], [])

    def test_substring_matching_would_pass_a_code_that_only_appears_inside_a_longer_one(self) -> None:
        """The company code NC-3617 is a substring of every planted code in the store.

        A gate written as ``code in evidence`` reports support from NC-3617-014. Token
        matching does not. This is the difference between a gate and a rubber stamp.
        """
        evidence = "entry NC-3617-014 closed with 3 blockers"
        self.assertFalse(harness.verbatim_support("NC-3617", evidence, match="token"))
        self.assertTrue(harness.verbatim_support("NC-3617", evidence, match="substring"))

    def test_identifiers_survive_a_cut_even_when_the_chunker_rewraps_the_line(self) -> None:
        """The chunker normalises transcript lines; identifiers still come through whole.

        The corpus version of this test shows the other half: a planted SENTENCE is not
        always byte-identical in the rendered piece, so sentence-level verbatim support
        is not available. Here the point is that a cut cannot orphan a code.
        """
        codes = [f"NC-3617-X{i:02d}" for i in range(1, 11)]
        source = "".join(
            f"{speaker}. Name: it says, and i quote, Entry {code}: the flat-file interface "
            f"set window ran {4 + i} hours {10 + i} minutes with {30 + i} blockers and one "
            f"open follow-up that nobody closed before the review ended\n\n"
            for i, code in enumerate(codes)
            for speaker in ("V. Dubois", "E. Tancredi")
        )
        for window in (7500, 200, 40, 10):
            joined = "\n".join(harness.chunk_texts(source, max_piece_tokens=window,
                                                     min_piece_tokens=0))
            for code in codes:
                self.assertTrue(harness.verbatim_support(code, joined),
                                f"{code} lost at window {window}")

    def test_a_control_character_in_the_question_would_false_refuse(self) -> None:
        """LightRAG deletes C0 controls (a PDF page break leaves \x0c behind) from stored
        text, so the evidence is clean while a question pasted from a PDF may not be. A
        gate that reads the raw question would refuse a perfectly answerable question.

        Scope, honestly: zero-width characters such as U+200B are NOT in the character set
        our imitator removes, so this hazard is about control characters, not about every
        invisible code point.
        """
        poisoned = "NC-3617\x1e-NA-01"
        self.assertNotIn("NC-3617-NA-01", harness.identifiers(poisoned))
        self.assertEqual(
            harness.identifiers(lightrag_text.sanitize_like_lightrag(poisoned)),
            ["NC-3617-NA-01"],
            "sanitising the question first is what makes the rule fair")

    def test_near_miss_grading_counts_the_whole_company_prefix(self) -> None:
        pool = ["NC-3617-014", "NC-3617-022", "NC-3617-NA-01", "CP-2507-001"]
        graded = harness.near_misses("NC-3617-NA-01", pool, company_code="NC-3617")
        self.assertEqual(graded["same_company_code"], ["NC-3617-014", "NC-3617-022"])
        self.assertEqual(graded["same_first_segment"], ["NC-3617-014", "NC-3617-022"])
        self.assertEqual(graded["shares_a_subtoken"], [])


class GeneratedCorpusShape(unittest.TestCase):
    """What the invented probe set really looks like to retrieval and to a gate."""

    @classmethod
    def setUpClass(cls) -> None:
        if not HAS_PPTX:
            raise unittest.SkipTest("python-pptx is not installed; the generator cannot build the deck")
        cls.tmp = Path(tempfile.mkdtemp(prefix="grounding-test-"))
        cls.dir_a, cls.manifest_a = gen.generate_company(COMPANY_A, SEED, cls.tmp)
        cls.dir_b, cls.manifest_b = gen.generate_company(COMPANY_B, SEED, cls.tmp)
        # Analysing a corpus means chunking every document at four budgets, and every
        # probe question against every chunk. Compute each number once for the class.
        cls.pairs = ((cls.dir_a, cls.manifest_a), (cls.dir_b, cls.manifest_b))
        cls.reports = {dir_: harness.analyse_company(dir_) for dir_, _ in cls.pairs}
        cls.separations, cls.with_manifest = {}, {}
        for dir_, manifest in cls.pairs:
            documents = harness.corpus_documents(dir_)
            cls.separations[dir_] = harness.probe_retrieval_separation(manifest, documents)
            raw = (dir_ / "manifest.json").read_text(encoding="utf-8")
            cls.with_manifest[dir_] = harness.probe_retrieval_separation(
                manifest, dict(documents, **{"manifest.json": raw}))
        cls.store = harness.store_report([cls.dir_a, cls.dir_b])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_unanswerable_codes_are_in_no_document_and_in_every_manifest(self) -> None:
        """The probe promise holds for documents and fails for manifest.json.

        manifest.json sat inside the company folder and was ingested as an ordinary
        document until the skip rule landed, so an unanswerable code WAS in the index,
        alongside a sentence about it that reads like a recorded outcome.
        """
        for company_dir, manifest in self.pairs:
            report = self.reports[company_dir]
            codes = [entry["code"] for entry in manifest["unanswerable_facts"]]
            self.assertEqual(report["unanswerable_absent_from_document_chunks"], len(codes),
                             f"{manifest['company']}: an unanswerable code was found in a document")
            self.assertEqual(report["unanswerable_present_in_manifest_chunks"], len(codes))
            self.assertEqual(report["unanswerable_substring_false_support_in_documents"], 0)
            # the answer key is in the same file
            self.assertIn("refusal:", (company_dir / "manifest.json").read_text(encoding="utf-8"))

    def test_lookalike_density_is_two_orders_of_magnitude_per_probe(self) -> None:
        for company_dir, _manifest in self.pairs:
            report = self.reports[company_dir]
            self.assertGreaterEqual(report["unanswerable_near_miss_same_company_code_min"], 100,
                                    "the retrieval pool should be dense with same-prefix codes")

    def test_the_gate_would_fire_on_every_unanswerable_probe_and_on_none_of_the_others(self) -> None:
        """Identifier support separates the two probe shapes under a lexical stand-in."""
        for company_dir, _manifest in self.pairs:
            separation = self.separations[company_dir]
            self.assertEqual(separation["unanswerable"]["probes"], 6)
            self.assertEqual(separation["unanswerable"]["asked_or_expected_code_in_top_k"], 0,
                             "no unanswerable code reached the top of retrieval")
            self.assertGreater(separation["scoped"]["asked_or_expected_code_in_top_k"], 0,
                               "the stand-in must be able to find real codes, or the zero above means nothing")

    def test_no_planted_code_is_ever_split_by_our_chunker(self) -> None:
        """Swept from the shipped window down to a pathological one.

        The instrument is live: the same helper reports a loss when an identifier exists
        only in raw text that the sanitiser removes (see TestIdentifierSupportRule and
        identifier_survival), so these zeros are a measurement, not a tautology.
        """
        for company_dir, _manifest in self.pairs:
            report = self.reports[company_dir]
            self.assertEqual(report["answerable_codes_verbatim_in_home_document"],
                             report["answerable_codes_count"])
            self.assertEqual(report["answerable_codes_lost_to_sanitiser"], 0)
            for budget, lost in report["answerable_codes_lost_to_chunking_by_budget"].items():
                self.assertEqual(lost, 0, f"a code was split at window {budget}")

    def test_sentence_level_support_is_not_available_even_for_plain_text(self) -> None:
        """Some planted sentences are not byte-identical in the rendered piece.

        A grounding rule must key on identifiers (or normalise whitespace), never on
        sentence equality, or it will refuse answers that are properly supported.
        """
        for company_dir, _manifest in self.pairs:
            report = self.reports[company_dir]
            self.assertGreater(report["plaintext_facts"], 0)
            self.assertLess(report["plaintext_facts_sentence_verbatim_in_piece"],
                            report["plaintext_facts"],
                            "expected at least one re-wrapped sentence to miss a verbatim match")

    def test_the_55_probe_run_has_41_distinct_questions(self) -> None:
        """A number the report depends on: probe ROWS versus distinct questions.

        28 scoped + 15 cross-company + 12 unanswerable = 55 rows, but 14 scoped rows per
        company cover only 7 questions, so accuracy is not 55 independent samples.
        """
        store = self.store
        self.assertEqual(store["probe_counts"], {"scoped": 28, "cross_company": 15, "unanswerable": 12})
        self.assertEqual(store["distinct_questions"], {"scoped": 14, "cross_company": 15, "unanswerable": 12})
        self.assertEqual(sum(store["probe_counts"].values()), 55)

    def test_a_manifest_in_the_pool_never_makes_an_unanswerable_probe_answerable_lexically(self) -> None:
        """Measured, and deliberately NOT a claim about LightRAG.

        The manifest chunk holding the asked code ranks 50-70 of ~120 under lexical
        scoring, i.e. outside chunk_top_k. LightRAG retrieves by embedding plus entity
        graph, where that same code would have been extracted as an ENTITY from
        manifest.json; only a run against the live store can settle whether it surfaced.
        """
        for company_dir, _manifest in self.pairs:
            separation = self.with_manifest[company_dir]
            ranks = separation["unanswerable"]["best_code_chunk_rank_min"]
            self.assertIsNotNone(ranks, "the manifest chunk should be scorable at all")
            self.assertGreater(ranks, 16, "not inside chunk_top_k under the lexical stand-in")


class TestHarnessCountersAreLive(unittest.TestCase):
    """Prove the survival counter can report a loss, so its zeros mean something."""

    def test_reports_an_identifier_missing_from_the_source(self) -> None:
        # an invisible character inside the code: the exact token is NOT in the raw text,
        # which is also precisely the question-side false-refusal hazard
        survival = harness.identifier_survival(
            "entry NC\u200b-3617-014 closed", ["NC-3617-014"])
        self.assertEqual(survival["missing_in_raw"], 1)
        self.assertEqual(survival["total"], 1)

    def test_reports_a_clean_document_as_clean(self) -> None:
        survival = harness.identifier_survival(
            "entry NC-3617-014 closed with 3 blockers", ["NC-3617-014"])
        self.assertEqual(survival, {"total": 1, "missing_in_raw": 0,
                                    "lost_to_sanitiser": 0, "lost_to_chunking": 0})

    def test_the_sanitiser_never_touches_the_identifier_alphabet(self) -> None:
        """Why lost_to_sanitiser is structurally zero, not luckily zero."""
        sample = ("Entry NC-3617-T03-01: the window ran 4 hours 55 minutes (see row "
                  "NC-3617-R1101) blockers: 30\x0c\x1e")
        stripped = lightrag_text.stripped_characters(sample)
        self.assertTrue(stripped, "the fixture must actually contain a removed character")
        for character in stripped:
            self.assertNotIn(character, "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-")


if __name__ == "__main__":
    unittest.main()
