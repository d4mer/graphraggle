"""M1: the asked-identifier grounding check in the gateway query path — FLAG ONLY.

What this file holds to account
-------------------------------
1. **Off is the shipped state and it is byte-identical.** With
   ``GROUNDING_CHECK_ASKED_IDENTIFIER`` off, the upstream payload and the response envelope
   are exactly what they were before the check existed, and the word "grounding" appears
   nowhere in the response.
2. **On changes only what is reported.** The answer, the citation list and the ok flag are
   identical to the flag-off run; the only difference is ``query_scope["grounding"]``.
3. **The rule and its traps**, each pinned where it can actually fire: token match rather
   than substring (a company code is a substring of every planted code in the store), the
   question sanitised the way LightRAG sanitises stored text, evidence taken from the
   primary pass's references rather than the caller's scoped-and-truncated citations, and
   references that carry no content counted rather than silently treated as unsupported.
4. **One implementation.** The investigation harness and the gateway call the same
   functions, so the harness cannot certify a rule the gateway no longer runs.
5. **The log line carries the discrimination counts** (asked / unsupported / which pass the
   evidence came from) without any reference text.

Nothing here reads corpus, LightRAG or probe data. Everything upstream is stubbed.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))

from app import api, grounding, observability, state_store          # noqa: E402
from app.config import Settings                                     # noqa: E402
from app.models import QueryRequest                                 # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "unanswerable_grounding_for_gate", ROOT_DIR / "scripts" / "unanswerable_grounding.py")
harness = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
sys.modules[_SPEC.name] = harness
_SPEC.loader.exec_module(harness)

ABSENT_CODE = "NC-3617-NA-01"
REAL_CODE = "NC-3617-014"


def _run(coro):
    return asyncio.run(coro)


class _QueryPathProbe(unittest.TestCase):
    """One stubbed /query pass, with the flag under the caller's control."""

    def setUp(self) -> None:
        # The query path reads the state store (company scoping, query log); conftest gives
        # the path but no schema, so create a throwaway one.
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-ground-")
        self._saved_db = state_store.settings.state_db_path
        state_store.settings.state_db_path = str(Path(self._tmp.name) / "state.db")
        _run(state_store.init_db())
        self.addCleanup(self._restore)

    def _restore(self) -> None:
        state_store.settings.state_db_path = self._saved_db
        self._tmp.cleanup()

    def call(self, answer: str, citations: list[dict], *, flag: bool,
             question: str | None = None) -> tuple[object, list[dict]]:
        payloads: list[dict] = []

        async def post_json(path, payload):
            payloads.append(payload)
            return {"response": answer, "references": citations}

        client = AsyncMock()
        client.post_json = AsyncMock(side_effect=post_json)
        with patch.object(api, "client", client), patch.object(
                api.settings, "grounding_check_asked_identifier", flag):
            envelope = _run(api.query(QueryRequest(
                query=question or f"What outcome was recorded under {ABSENT_CODE}?",
                mode="mix")))
        return envelope, payloads


CITED_NEAR_MISS = [
    {"reference_id": "1", "file_path": "Northwind Chemicals/transcripts/a.txt",
     "content": f"Reviewer: entry {REAL_CODE} closed with 3 blockers outstanding."},
    {"reference_id": "2", "file_path": "Northwind Chemicals/transcripts/b.txt",
     "content": "Reviewer: entry NC-3617-022 was reopened after 12 hours."},
]


class TestFlagOffIsByteIdentical(_QueryPathProbe):
    ANSWER = f"{ABSENT_CODE} closed at 4 hours 10 minutes, in line with {REAL_CODE}."

    def test_the_shipped_default_is_off(self) -> None:
        self.assertIs(Settings().grounding_check_asked_identifier, False,
                      "M1 ships as a measurement, not as a behaviour change")

    def test_the_upstream_payload_does_not_change(self) -> None:
        _off_envelope, off_payloads = self.call(self.ANSWER, CITED_NEAR_MISS, flag=False)
        _on_envelope, on_payloads = self.call(self.ANSWER, CITED_NEAR_MISS, flag=True)
        self.assertEqual(len(off_payloads), 1)
        self.assertEqual(off_payloads, on_payloads,
                         "the check must not add, drop or reorder an upstream field")

    def test_the_response_is_identical_apart_from_the_reported_field(self) -> None:
        off, _payloads = self.call(self.ANSWER, CITED_NEAR_MISS, flag=False)
        on, _payloads2 = self.call(self.ANSWER, CITED_NEAR_MISS, flag=True)
        self.assertEqual(off.data["answer"], on.data["answer"])
        self.assertEqual(off.data["citations"], on.data["citations"])
        self.assertEqual(off.ok, on.ok)
        self.assertNotIn("grounding", off.data["query_scope"])
        self.assertIn("grounding", on.data["query_scope"])
        off_scope = dict(off.data["query_scope"])
        on_scope = dict(on.data["query_scope"])
        on_scope.pop("grounding")
        self.assertEqual(off_scope, on_scope,
                         "flag on must add exactly one key to query_scope and change nothing else")

    def test_the_word_grounding_is_absent_when_the_flag_is_off(self) -> None:
        envelope, _payloads = self.call(self.ANSWER, CITED_NEAR_MISS, flag=False)
        blob = json.dumps({"data": envelope.data, "meta": envelope.meta}, default=str).lower()
        self.assertNotIn("grounding", blob)


class TestFlagOnReportsWithoutChanging(_QueryPathProbe):
    ANSWER = f"{ABSENT_CODE} closed at 4 hours 10 minutes, in line with {REAL_CODE}."

    def test_an_unsupported_answer_is_reported_and_otherwise_untouched(self) -> None:
        off, _p = self.call(self.ANSWER, CITED_NEAR_MISS, flag=False)
        on, _p2 = self.call(self.ANSWER, CITED_NEAR_MISS, flag=True)
        report = on.data["query_scope"]["grounding"]
        self.assertEqual(report["verdict"], "no_support_for_asked_identifier")
        self.assertEqual(report["asked"], [ABSENT_CODE])
        self.assertEqual(report["unsupported"], [ABSENT_CODE])
        self.assertIs(report["supported"], False)
        self.assertEqual(report["evidence_pass"], "primary")
        self.assertTrue(report["check_enabled"])
        # ...and the caller still gets the same answer and the same citations.
        self.assertEqual(on.data["answer"], off.data["answer"])
        self.assertEqual(on.data["citations"], off.data["citations"])
        self.assertTrue(on.ok)

    def test_a_grounded_answer_reports_supported(self) -> None:
        answer = f"{REAL_CODE} closed with 3 blockers outstanding."
        envelope, _payloads = self.call(answer, CITED_NEAR_MISS, flag=True,
                                        question=f"What happened under {REAL_CODE}?")
        report = envelope.data["query_scope"]["grounding"]
        self.assertEqual(report["verdict"], "supported")
        self.assertEqual(report["unsupported"], [])
        self.assertIs(report["supported"], True)

    def test_a_question_naming_no_identifier_reports_an_empty_asked_list(self) -> None:
        """28 scoped + 15 cross-company probes in the GL-08 run are shaped like this.

        The rule cannot cost them recall because it has nothing to key on - which is also
        why the verdict is not a pass.
        """
        envelope, _payloads = self.call(
            f"In Northwind Chemicals, the review closed with 3 blockers.",
            CITED_NEAR_MISS, flag=True,
            question="In Northwind Chemicals, what is recorded about order-to-cash handling?")
        report = envelope.data["query_scope"]["grounding"]
        self.assertEqual(report["asked"], [])
        self.assertEqual(report["verdict"], "no_identifier_in_question")
        self.assertIsNone(report["supported"], "no identifiers is not the same as supported")


class TestTheRuleAndItsTraps(unittest.TestCase):
    def test_substring_matching_would_pass_a_prefix_of_a_longer_code(self) -> None:
        evidence = f"entry {REAL_CODE} closed with 3 blockers"
        self.assertFalse(grounding.verbatim_support("NC-3617", evidence),
                         "token match: the company code is not in that evidence")
        self.assertTrue(grounding.verbatim_support("NC-3617", evidence, match="substring"),
                         "this is the bug the rule must not have")

    def test_the_question_is_sanitised_the_way_stored_text_is(self) -> None:
        """A C0 control inside the code would otherwise make an answerable question look
        unanswerable: LightRAG deletes those characters from stored text."""
        poisoned = f"What happened under NC-3617\x1e-014?"
        gate = grounding.support_gate(poisoned, f"{REAL_CODE} closed.", f"entry {REAL_CODE}")
        self.assertEqual(gate["asked"], [REAL_CODE])
        self.assertEqual(gate["verdict"], "supported")
        raw = grounding.support_gate(poisoned, f"{REAL_CODE} closed.", f"entry {REAL_CODE}",
                                     sanitise_question=False)
        self.assertNotEqual(raw["asked"], [REAL_CODE],
                            "un-sanitised, the same question would be refused")

    def test_references_without_content_are_counted_not_guessed(self) -> None:
        """The payload asks for chunk content (include_chunk_content: True). If a real
        response ever omits it, the check must say so rather than call everything
        unsupported."""
        citations = [{"reference_id": "1", "file_path": "a.txt"},
                     {"reference_id": "2", "file_path": "b.txt"}]
        report = grounding.asked_identifier_grounding(
            f"What happened under {ABSENT_CODE}?", f"{ABSENT_CODE} closed.", citations)
        self.assertEqual(report["verdict"], "no_evidence")
        self.assertEqual(report["evidence_reference_count"], 2)
        self.assertEqual(report["evidence_references_without_content"], 2)

    def test_evidence_is_the_reference_list_not_the_truncated_citation_view(self) -> None:
        """Same triple, different evidence base: the caller's citation list is a view."""
        cited = [{"reference_id": "1", "content": f"entry {ABSENT_CODE} was never measured"}]
        report = grounding.asked_identifier_grounding(
            f"What outcome was recorded under {ABSENT_CODE}?", f"{ABSENT_CODE} was never measured",
            cited)
        self.assertEqual(report["verdict"], "supported")
        self.assertEqual(report["evidence_reference_count"], 1)

    def test_an_answer_may_not_assert_a_code_nothing_supported(self) -> None:
        gate = grounding.support_gate("Was the freeze window reduced?",
                                      f"Yes; see {ABSENT_CODE}.",
                                      "the freeze window was reduced to 48 hours")
        self.assertEqual(gate["verdict"], "unsupported_identifiers_in_answer")
        self.assertEqual(gate["unsupported"], [ABSENT_CODE])

    def test_identifiers_are_token_shaped_and_not_all_caps_prose(self) -> None:
        self.assertEqual(grounding.identifiers("PDF and WARNING but NC-3617-014 and CP-2-1"),
                         ["NC-3617-014", "CP-2-1"])

    def test_the_harness_and_the_gateway_run_the_same_code(self) -> None:
        """One implementation. A second copy in scripts/ would let the harness certify a
        rule the gateway no longer executes."""
        self.assertIs(harness.identifiers, grounding.identifiers)
        self.assertIs(harness.verbatim_support, grounding.verbatim_support)
        self.assertIs(harness.grounding, grounding)
        triples = (f"What happened under {ABSENT_CODE}?", f"{ABSENT_CODE} closed.",
                   [f"entry {REAL_CODE}"])
        harness_report = harness.support_gate(*triples)
        app_report = grounding.support_gate(triples[0], triples[1], "\n".join(triples[2]))
        for key in ("verdict", "asked", "unsupported", "supported"):
            self.assertEqual(harness_report[key], app_report[key])


class TestQueryLogCarriesTheCounts(unittest.TestCase):
    def _emit(self, query_scope: dict) -> dict:
        timer = observability.StageTimer()
        with patch("app.observability._emit") as emit:
            observability.emit_query_log(
                request_id="req-1", query="q", company=None, mode_requested="mix",
                mode_used="mix", top_k=12, chunk_top_k=16, query_scope=query_scope,
                citations_raw=2, citations_merged=2, citations_final=2, answer="a",
                timer=timer, started=0.0)
        self.assertEqual(emit.call_count, 1)
        return emit.call_args.args[0]

    def test_the_fields_are_present_when_the_check_ran(self) -> None:
        line = self._emit({"grounding": {
            "verdict": "no_support_for_asked_identifier", "asked": [ABSENT_CODE],
            "unsupported": [ABSENT_CODE], "evidence_pass": "primary",
            "evidence_references_without_content": 0}})
        self.assertTrue(line["grounding_checked"])
        self.assertEqual(line["grounding_verdict"], "no_support_for_asked_identifier")
        self.assertEqual(line["grounding_asked"], [ABSENT_CODE])
        self.assertEqual(line["grounding_unsupported"], [ABSENT_CODE])
        self.assertEqual(line["grounding_evidence_pass"], "primary")

    def test_the_fields_are_quiet_when_it_did_not(self) -> None:
        line = self._emit({})
        self.assertFalse(line["grounding_checked"])
        self.assertIsNone(line["grounding_verdict"])

    def test_no_reference_text_ever_reaches_the_line(self) -> None:
        """The log projects named fields, so evidence text cannot leak by accident."""
        line = self._emit({"grounding": {
            "verdict": "supported", "asked": [REAL_CODE], "unsupported": [],
            "evidence_pass": "primary", "evidence_references_without_content": 0,
            # A hypothetical future field. If emit_query_log ever dumps the whole dict,
            # this line starts carrying corpus text and the test fails.
            "evidence": "Reviewer: entry NC-3617-014 closed with 3 blockers outstanding."}})
        blob = json.dumps(line)
        self.assertNotIn("Reviewer", blob)
        self.assertNotIn("blockers", blob)


if __name__ == "__main__":
    unittest.main()
