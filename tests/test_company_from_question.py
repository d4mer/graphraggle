"""GL-10 part 2: the company named in the question scopes the query.

Why this exists
---------------
The operator's rule is "just the mention of the company should be enough". Before this packet a
query was scoped only when a caller sent the ``company`` field, and the caller that matters most -
Open WebUI through the bridge - never sends one. These tests pin the reading of a company out of
the question text, the multi-company case, and what happens when the question names something the
store has no rows for.

What the tests pin
------------------
- one company named -> that company; several named -> all of them; none named -> unscoped
- matching is case- and punctuation-insensitive ("NORTHWIND  CHEMICALS'" still scopes)
- whole-phrase matching: "Acmeist" never scopes to "Acme"; the longest known name wins, and a
  shorter name cannot match inside the longer one's span
- a single-word company whose name is an ordinary English word needs a capital in the question,
  so "what did the target say" does not scope to a company called Target
- aliases bridge what a person types to the directory name; an alias naming a company with no
  rows is ignored rather than trusted
- a question that names an unknown organisation stays unscoped and records it as a candidate
- the known-company list comes from the store's own rows, `skipped` duplicates included
- the citation filter accepts a company SET, and still excludes a citation whose document row is
  missing (an unattributable citation cannot be claimed as the asked company's)
- on the bridge path (the one the operator uses) the flag-on run filters the references to the
  named companies, and the flag-off run does not touch them

Every upstream call is mocked; the documents are invented in a temp store.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.api as api
import app.observability
import app.state_store as state_store
from app.company_scope import (
    detect_companies,
    match_key,
    parse_alias_map,
    validate_aliases,
)

KNOWN = ["Northwind Chemicals", "Contoso Pharma", "Acme Chemicals", "Acme", "Target"]


def _run(coro):
    return asyncio.run(coro)


def _stream_body(chunks: list[dict]) -> bytes:
    return "\n".join(json.dumps(c) for c in chunks).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes):
        self.content = content


class TestDetectionRules(unittest.TestCase):
    def test_one_company_named_scopes_to_that_company(self):
        d = detect_companies("what did Northwind Chemicals report for Q3", KNOWN)
        self.assertEqual(d.companies, ["Northwind Chemicals"])
        self.assertEqual(d.mode, "single")

    def test_several_companies_named_scope_to_all_of_them(self):
        d = detect_companies("compare Northwind Chemicals and Contoso Pharma on lead times", KNOWN)
        self.assertEqual(sorted(d.companies), ["Contoso Pharma", "Northwind Chemicals"])
        self.assertEqual(d.mode, "multi")

    def test_no_company_named_leaves_the_query_unscoped(self):
        d = detect_companies("summarise the quarterly comparison", KNOWN)
        self.assertEqual(d.companies, [])
        self.assertEqual(d.mode, "unscoped")
        self.assertEqual(d.candidates, [])

    def test_case_and_punctuation_do_not_unscope_a_question(self):
        for question in ("NORTHWIND CHEMICALS Q3", "northwind  chemicals' Q3", "Northwind, Chemicals?"):
            with self.subTest(question=question):
                self.assertEqual(detect_companies(question, KNOWN).companies,
                                 ["Northwind Chemicals"])

    def test_matching_is_whole_phrase_not_substring(self):
        self.assertEqual(detect_companies("compare it with Acmeist's policy", KNOWN).companies, [])
        self.assertEqual(detect_companies("the acmeist manifesto", KNOWN).companies, [])

    def test_longest_name_wins_and_a_shorter_name_cannot_match_inside_it(self):
        d = detect_companies("does Acme Chemicals differ from Acme?", KNOWN)
        self.assertEqual(sorted(d.companies), ["Acme", "Acme Chemicals"])
        # One span "Acme Chemicals" plus one separate "Acme": two companies, not three mentions of
        # the short name.
        self.assertEqual(len(d.companies), 2)

    def test_ordinary_word_company_needs_a_capital_in_the_question(self):
        self.assertEqual(detect_companies("what did the target say about safety?", KNOWN).companies,
                         [], "a lowercase ordinary noun must not scope the query")
        self.assertEqual(detect_companies("what did Target say about safety?", KNOWN).companies,
                         ["Target"])

    def test_multi_word_names_match_case_insensitively(self):
        self.assertEqual(detect_companies("notes from the contoso pharma call", KNOWN).companies,
                         ["Contoso Pharma"])

    def test_aliases_bridge_what_a_person_types(self):
        aliases, rejected = validate_aliases(
            parse_alias_map("Northwind Chemicals=nwind,north wind;Ghost Corp=phantom"), KNOWN)
        self.assertEqual(rejected, ["Ghost Corp"], "an alias to a company with no rows is ignored")
        d = detect_companies("how does nwind handle customs?", KNOWN, aliases=aliases)
        self.assertEqual(d.companies, ["Northwind Chemicals"])
        self.assertEqual(d.aliased_by, {"Northwind Chemicals": "nwind"})

    def test_unknown_organisation_stays_unscoped_and_is_recorded_as_a_candidate(self):
        d = detect_companies("what did Zenith Works decide about freight", KNOWN)
        self.assertEqual(d.companies, [])
        self.assertEqual(d.mode, "unscoped")
        self.assertIn("Zenith Works", d.candidates)

    def test_names_with_regex_metachars_are_matched_literally(self):
        known = ["ITM Group (Pty) Ltd", "A.B. & Co"]
        self.assertEqual(detect_companies("ask ITM Group (Pty) Ltd about capacity", known).companies,
                         ["ITM Group (Pty) Ltd"])
        self.assertEqual(detect_companies("the A.B. & Co invoice", known).companies, ["A.B. & Co"])

    def test_empty_inputs_never_raise(self):
        for question, known in (("", KNOWN), (None, KNOWN), ("anything", []), (None, None)):
            with self.subTest(question=question, known=known):
                d = detect_companies(question, known)
                self.assertEqual(d.companies, [])

    def test_a_long_question_does_not_blow_up_or_match_twice(self):
        question = ("Northwind Chemicals " * 40) + "logistics"
        d = detect_companies(question, KNOWN)
        self.assertEqual(d.companies, ["Northwind Chemicals"])

    def test_match_key_normalises_spacing_and_punctuation(self):
        self.assertEqual(match_key("Northwind  Chemicals'"), match_key("northwind chemicals"))


class _TempStore(unittest.TestCase):
    """A real state DB in a temp directory: the company list must come from real rows."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-gl10-")
        self.base = Path(self._tmp.name)
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.base / "source_docs")
        settings.uploads_dir = str(self.base / "uploads")
        (self.base / "source_docs").mkdir()
        (self.base / "uploads").mkdir()
        _run(state_store.init_db())

    def tearDown(self) -> None:
        for name, value in zip(("state_db_path", "source_docs_dir", "uploads_dir"), self._saved):
            setattr(state_store.settings, name, value)
        self._tmp.cleanup()

    def seed(self, path: str, company: str | None, *, status: str = "ingested",
             document_id: str | None = None) -> None:
        _run(state_store.upsert_document(
            document_id or f"doc-{abs(hash((path, company))) % 10_000}",
            path, "scan", company, Path(path).name, None, f"sha-{path}", status, None))


class TestKnownCompanyList(_TempStore):
    def test_known_companies_come_from_the_store_and_include_skipped_rows(self):
        self.seed("/source_docs/Northwind Chemicals/a.pdf", "Northwind Chemicals")
        self.seed("/source_docs/Contoso Pharma/b.pdf", "Contoso Pharma", status="skipped")
        self.seed("/source_docs/unscoped/c.pdf", None)
        self.assertEqual(_run(state_store.list_distinct_companies()),
                         ["Contoso Pharma", "Northwind Chemicals"],
                         "distinct, sorted, no NULLs; a skipped duplicate still proves the company")


class TestScopeResolution(_TempStore):
    def test_detection_fills_a_query_that_sends_no_company_field(self):
        self.seed("/source_docs/Northwind Chemicals/a.pdf", "Northwind Chemicals")
        meta = _run(api.resolve_company_scope("what did Northwind Chemicals report", None))
        self.assertEqual(meta["companies"], ["Northwind Chemicals"])
        self.assertEqual(meta["company_detected_from"], "question")
        self.assertEqual(meta["company_mode"], "single")

    def test_an_explicit_company_field_beats_detection(self):
        self.seed("/source_docs/Northwind Chemicals/a.pdf", "Northwind Chemicals")
        self.seed("/source_docs/Contoso Pharma/b.pdf", "Contoso Pharma")
        meta = _run(api.resolve_company_scope("what did Northwind Chemicals report", "Contoso Pharma"))
        self.assertEqual(meta["companies"], ["Contoso Pharma"])
        self.assertEqual(meta["company_detected_from"], "request")

    def test_a_question_naming_nothing_leaves_the_mode_unscoped(self):
        self.seed("/source_docs/Northwind Chemicals/a.pdf", "Northwind Chemicals")
        meta = _run(api.resolve_company_scope("summarise the freight notes", None))
        self.assertEqual(meta["company_mode"], "unscoped")
        self.assertEqual(meta["companies"], [])

    def test_a_misconfigured_alias_is_reported_not_applied(self):
        self.seed("/source_docs/Northwind Chemicals/a.pdf", "Northwind Chemicals")
        with patch.object(api.settings, "company_aliases", "Ghost Corp=phantom"):
            meta = _run(api.resolve_company_scope("ask phantom about capacity", None))
        self.assertEqual(meta["companies"], [])
        self.assertEqual(meta["company_aliases_rejected"], ["Ghost Corp"])


class TestCitationFilterWithACompanySet(_TempStore):
    def setUp(self):
        super().setUp()
        self.nw = "/source_docs/Northwind Chemicals/q3.pdf"
        self.cp = "/source_docs/Contoso Pharma/q3.pdf"
        self.zen = "/source_docs/Zenith/q3.pdf"
        self.seed(self.nw, "Northwind Chemicals", document_id="doc-nw")
        self.seed(self.cp, "Contoso Pharma", document_id="doc-cp")
        self.seed(self.zen, "Zenith", document_id="doc-zen")

    def test_two_named_companies_keep_both_and_drop_the_third(self):
        citations = [{"file_path": p} for p in (self.nw, self.cp, self.zen)]
        included, scope = _run(api.scope_query_citations(
            citations, requested_company=None,
            allowed_companies=["Northwind Chemicals", "Contoso Pharma"]))
        self.assertEqual([c["file_path"] for c in included], [self.nw, self.cp])
        self.assertEqual(scope["mode"], "company_set")
        self.assertEqual(sorted(scope["companies"]), ["Contoso Pharma", "Northwind Chemicals"])
        self.assertEqual(included[0]["company"], "Northwind Chemicals")

    def test_a_citation_with_no_document_row_is_still_excluded(self):
        included, _ = _run(api.scope_query_citations(
            [{"file_path": self.nw}, {"file_path": "/source_docs/Ghost/x.pdf"}],
            requested_company=None, allowed_companies=["Northwind Chemicals"]))
        self.assertEqual([c["file_path"] for c in included], [self.nw])

    def test_no_scope_any_way_keeps_everything_as_before(self):
        citations = [{"file_path": p} for p in (self.nw, self.cp, self.zen, "/unmatched/x.pdf")]
        included, scope = _run(api.scope_query_citations(
            citations, requested_company=None, allowed_companies=None))
        self.assertEqual(len(included), 4, "an unscoped query is byte-identical to today's")
        self.assertEqual(scope["mode"], "unscoped")
        self.assertEqual(scope["company"], None)

    def test_a_single_requested_company_still_matches_exactly_as_before(self):
        citations = [{"file_path": p} for p in (self.nw, self.cp)]
        included, scope = _run(api.scope_query_citations(
            citations, requested_company="Contoso Pharma"))
        self.assertEqual([c["file_path"] for c in included], [self.cp])
        self.assertEqual(scope["mode"], "company")


class TestBridgePathScoping(_TempStore):
    """The path the operator actually uses: Open WebUI -> bridge -> LightRAG /query/stream."""

    def setUp(self):
        super().setUp()
        # Distinct basenames on purpose: build_sources_block dedupes by basename, so a shared
        # name would hide whether the filter ran at all.
        self.nw = "/source_docs/Northwind Chemicals/northwind-q3.pdf"
        self.cp = "/source_docs/Contoso Pharma/contoso-q3.pdf"
        self.seed(self.nw, "Northwind Chemicals", document_id="doc-nw")
        self.seed(self.cp, "Contoso Pharma", document_id="doc-cp")
        self.body = _stream_body([
            {"references": [
                {"reference_id": "1", "file_path": self.nw},
                {"reference_id": "2", "file_path": self.cp},
            ], "response": ""},
            {"response": "Q3 lead times rose by four days."},
        ])

    def _bridge(self, question: str) -> str:
        proxy = AsyncMock(return_value=_Resp(self.body))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            return _run(api.answer_ollama_bridge_direct(
                {"messages": [{"role": "user", "content": question}]}))

    def test_flag_on_names_one_company_and_only_that_company_is_cited(self):
        captured: list[dict] = []

        def capture(line):
            captured.append(line)

        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "bridge_sources_enabled", True), \
                patch.object(app.observability, "_emit", capture):
            answer = self._bridge("what did Northwind Chemicals report for Q3?")
        self.assertIn("Q3 lead times rose by four days.", answer)
        self.assertIn("northwind-q3.pdf — Northwind Chemicals", answer)
        self.assertNotIn("contoso-q3.pdf", answer, "only the named company's source is listed")
        scope = captured[-1].get("company_scope") or {}
        self.assertEqual(scope.get("companies"), ["Northwind Chemicals"],
                         "the log line says what the question named")

    def test_flag_on_names_two_companies_and_both_are_cited(self):
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "bridge_sources_enabled", True):
            answer = self._bridge("compare Northwind Chemicals and Contoso Pharma for Q3")
        self.assertIn("northwind-q3.pdf", answer)
        self.assertIn("contoso-q3.pdf", answer)

    def test_flag_off_leaves_every_reference_and_logs_no_company_scope(self):
        captured: list[dict] = []

        def capture(line):
            captured.append(line)

        with patch.object(api.settings, "company_from_question", False), \
                patch.object(api.settings, "bridge_sources_enabled", True), \
                patch.object(app.observability, "_emit", capture):
            answer = self._bridge("what did Northwind Chemicals report for Q3?")
        self.assertIn("northwind-q3.pdf", answer)
        self.assertIn("contoso-q3.pdf", answer, "off means untouched: no filter, no company label")
        self.assertNotIn("— Northwind Chemicals", answer)
        self.assertNotIn("company_scope", captured[-1])

    def test_a_question_naming_nothing_known_leaves_all_references(self):
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "bridge_sources_enabled", True):
            answer = self._bridge("what changed in Q3?")
        self.assertIn("northwind-q3.pdf", answer)
        self.assertIn("contoso-q3.pdf", answer)


class TestDefaults(unittest.TestCase):
    def test_the_packet_is_off_by_default(self):
        from app.config import Settings
        defaults = Settings()
        self.assertFalse(defaults.company_from_question)
        self.assertEqual(defaults.company_aliases, "")


if __name__ == "__main__":
    unittest.main()
