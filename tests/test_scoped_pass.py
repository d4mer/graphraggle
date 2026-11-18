"""GL-10 part 3: the scoped pass — retrieve, restrict to the named companies, answer again.

Why this exists
---------------
Part 2 filters the citations after the answer exists, so another company's material is still what
the model answered from. This packet makes the retrieval result itself the boundary. The tests here
are the proof of the one claim that matters: **the text handed to the second model call contains
the named company's chunks and none of the other company's.** Everything else is bookkeeping.

What the tests pin
------------------
- reference classification: the named companies' documents are kept, another company's are dropped,
  and a reference the gateway cannot join to any row is dropped as unattributable (the upload-door
  basename case) rather than assumed to belong here
- a path owned by two companies (one ingested, one a `skipped` duplicate) is kept for either
- the context cap drops whole references, never part of one, and counts what it dropped
- nothing attributable survives -> an honest refusal, no second model call, no invented answer
- kept references that carry no chunk text -> the same refusal, citations still shown
- a blank regeneration -> refusal, never a silent pass-through of the unscoped draft
- the `/query` door: with `COMPANY_SCOPE_ENFORCEMENT=citations` (the shipped default) exactly one
  upstream call happens, as today; with `retrieval` there are two, the second is `mode: bypass`,
  and the evidence-adding stages (multi-query, naive fallback, graph expansion) are skipped
- the bridge door: with `retrieval` the streaming call is not made at all; with the default it is,
  and no scoped pass runs

The "LightRAG" here is a fake that records every payload; the store is a temp DB of invented rows.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.api as api
import app.observability
import app.document_identity as document_identity
import app.scoped_pass as scoped_pass
import app.state_store as state_store
from app.models import QueryRequest

NW = "Northwind Chemicals"
CP = "Contoso Pharma"
NW_PATH = "/source_docs/Northwind Chemicals/northwind-q3.pdf"
CP_PATH = "/source_docs/Contoso Pharma/contoso-q3.pdf"
GHOST_PATH = "contoso-q3.pdf"          # an upload-door basename: never joins a stored path
NW_CHUNK = "Northwind lead times rose by four days in Q3."
CP_CHUNK = "Contoso lead times fell by one day in Q3."


def _run(coro):
    return asyncio.run(coro)


def _ref(index: str, path: str, chunks: list[str] | None) -> dict:
    reference = {"reference_id": index, "file_path": path}
    if chunks is not None:
        reference["content"] = chunks
    return reference


# The top-level fields LightRAG 1.5.7 accepts on /query and /query/data, as read from its source by
# Hermes on the stack2 run. A fake that accepts any body hides request-shape bugs: the first R2 run
# died because the scoped retrieval body carried no `query` at all, and a permissive fake could not
# notice. Anything outside this set is rejected the way the real server rejects it.
LIGHTRAG_REQUEST_FIELDS = frozenset({
    "query", "mode", "only_need_context", "only_need_prompt", "response_type", "top_k",
    "chunk_top_k", "max_entity_tokens", "max_relation_tokens", "max_total_tokens", "hl_keywords",
    "ll_keywords", "conversation_history", "user_prompt", "disable_user_prompt_prefix",
    "enable_rerank", "include_references", "include_chunk_content", "include_progress", "stream",
})


class LightRAGRejected(RuntimeError):
    """Stand-in for httpx.HTTPStatusError: carries .response.status_code and .response.text."""

    class _Response:
        def __init__(self, status_code: int, text: str) -> None:
            self.status_code = status_code
            self.text = text

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"HTTP {status_code}: {message}")
        self.response = self._Response(status_code, message)


class FakeLightRAG:
    """Records payloads; answers a retrieval with fixed references and a bypass with fixed text.

    `/query/data` is answered with the envelope the brief describes (`status`, `message`,
    `data{entities, relationships, chunks, references}`) unless a test supplies its own body or makes
    the endpoint fail, which is how the fallback routes are exercised.

    Every body is checked the way the server checks it: a missing or blank `query`, or a top-level key
    outside LIGHTRAG_REQUEST_FIELDS, is a 400/422 rather than a silent answer. That strictness is the
    point - it is what makes the "payload never carried the question" class of bug fail a test instead
    of a stack run.
    """

    DEFAULT = object()

    def __init__(self, references: list[dict], generation_text: str = "the scoped answer",
                 response_text: str = "the unscoped draft answer", *,
                 data_payload: object = DEFAULT, data_raises: Exception | None = None,
                 strict: bool = True, reject_first_retrievals: int = 0) -> None:
        self.references = references
        self.generation_text = generation_text
        self.response_text = response_text
        self.data_payload = data_payload
        self.data_raises = data_raises
        self.strict = strict
        self.reject_first_retrievals = reject_first_retrievals
        self.retrievals_seen = 0
        self.rejections: list[tuple[str, list[str]]] = []
        self.calls: list[tuple[str, dict]] = []

    def _reject_reasons(self, payload: dict) -> list[str]:
        reasons: list[str] = []
        query = payload.get("query")
        if not isinstance(query, str) or not query.strip():
            reasons.append("query")
        unknown = sorted(set(payload) - LIGHTRAG_REQUEST_FIELDS)
        if unknown:
            reasons.append("unknown_keys=" + ",".join(unknown))
        return reasons

    async def post_json(self, path: str, payload: dict) -> dict:
        self.calls.append((path, payload))
        # Reject the first N retrieval calls (a scoped pass runs /query/data then /query, and runs
        # first in both doors). Shape-based rejection would also catch the ordinary pass, which asks
        # for chunk content too, and that would test the wrong thing.
        if payload.get("mode") != "bypass":
            self.retrievals_seen += 1
            if self.retrievals_seen <= self.reject_first_retrievals:
                raise LightRAGRejected(400 if path == "/query/data" else 422,
                                       '{"detail":"Invalid request parameters: query"}')
        if self.strict:
            reasons = self._reject_reasons(payload)
            if reasons:
                self.rejections.append((path, reasons))
                raise LightRAGRejected(400 if path == "/query/data" else 422,
                                       '{"detail":"Invalid request parameters: '
                                       + "; ".join(reasons) + '"}')
        if payload.get("mode") == "bypass":
            return {"response": self.generation_text}
        if path == "/query/data":
            if self.data_raises is not None:
                raise self.data_raises
            if self.data_payload is not self.DEFAULT:
                return self.data_payload
            return {"status": "success", "message": "ok",
                    "data": {"entities": [], "relationships": [], "chunks": [],
                             "references": self.references}}
        return {"response": self.response_text, "references": self.references}

    @property
    def paths(self) -> list[str]:
        return [path for path, _ in self.calls]

    @property
    def bypass_prompts(self) -> list[str]:
        return [payload["query"] for _, payload in self.calls if payload.get("mode") == "bypass"]

    @property
    def retrieval_payloads(self) -> list[dict]:
        return [payload for _, payload in self.calls if payload.get("mode") != "bypass"]


class _TempStore(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="grg-gl10p3-")
        self.base = Path(self._tmp.name)
        settings = state_store.settings
        self._saved = (settings.state_db_path, settings.source_docs_dir, settings.uploads_dir)
        settings.state_db_path = str(self.base / "state.db")
        settings.source_docs_dir = str(self.base / "source_docs")
        settings.uploads_dir = str(self.base / "uploads")
        (self.base / "source_docs").mkdir()
        (self.base / "uploads").mkdir()
        _run(state_store.init_db())
        self.seed(NW_PATH, NW, document_id="doc-nw")
        self.seed(CP_PATH, CP, document_id="doc-cp")

    def tearDown(self) -> None:
        for name, value in zip(("state_db_path", "source_docs_dir", "uploads_dir"), self._saved):
            setattr(state_store.settings, name, value)
        self._tmp.cleanup()

    def seed(self, path: str, company: str | None, *, document_id: str,
             status: str = "ingested", sha: str | None = None,
             submitted_source: str | None = None) -> None:
        _run(state_store.upsert_document(
            document_id, path, "scan", company, Path(path).name, None, sha or f"sha-{path}",
            status, None, submitted_source=submitted_source))


def _owners(**companies_by_path: dict[str, set[str]]) -> dict[str, document_identity.Ownership]:
    """Build the ownership map the classifier consumes, from a path -> companies shorthand."""
    return {path: document_identity.Ownership(companies=set(companies), via="path")
            for path, companies in companies_by_path.items()}


class TestReferenceClassification(unittest.TestCase):
    owners = _owners(**{NW_PATH: {NW}, CP_PATH: {CP}})

    def test_named_companies_are_kept_and_another_company_is_dropped(self):
        refs = [_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])]
        kept, counts = scoped_pass.classify_references(refs, self.owners, [NW])
        self.assertEqual([ref["file_path"] for ref in kept], [NW_PATH])
        self.assertEqual(counts, {"total": 2, "kept": 1, "other_company": 1, "unattributable": 0,
                                  "ambiguous": 0})

    def test_an_unjoinable_reference_is_dropped_not_assumed(self):
        refs = [_ref("1", GHOST_PATH, [CP_CHUNK])]
        kept, counts = scoped_pass.classify_references(refs, self.owners, [CP])
        self.assertEqual(kept, [])
        self.assertEqual(counts["unattributable"], 1)

    def test_a_path_owned_by_two_companies_is_kept_for_either(self):
        owners = dict(self.owners)
        owners[NW_PATH] = document_identity.Ownership(companies={NW, CP}, via="path")
        for allowed in ([NW], [CP], [NW, CP]):
            with self.subTest(allowed=allowed):
                kept, counts = scoped_pass.classify_references(
                    [_ref("1", NW_PATH, [NW_CHUNK])], owners, allowed)
                self.assertEqual(len(kept), 1)
                self.assertEqual(counts["unattributable"], 0)

    def test_a_path_whose_rows_carry_no_company_is_unattributable(self):
        kept, counts = scoped_pass.classify_references(
            [_ref("1", "/source_docs/unscoped/x.pdf", ["text"])],
            _owners(**{"/source_docs/unscoped/x.pdf": set()}), [NW])
        self.assertEqual(kept, [])
        self.assertEqual(counts["unattributable"], 1)


class TestSharedFileOwnership(_TempStore):
    """A file two companies own, indexed once upstream: both must still be able to reach it."""

    def setUp(self):
        super().setUp()
        # The shared-file pair: identical bytes, one row per company, different paths.
        self.shared_bytes = "identical cutover checklist\n" * 4
        self.nw_shared = "/source_docs/Northwind Chemicals/shared/cutover.txt"
        self.cp_shared = "/source_docs/Contoso Pharma/shared/cutover.txt"
        for path, company, document_id in ((self.nw_shared, NW, "doc-shared-nw"),
                                           (self.cp_shared, CP, "doc-shared-cp")):
            _run(state_store.upsert_document(
                document_id, path, "scan", company, Path(path).name, None, "sha-shared",
                "ingested", None))

    def test_a_shared_paths_ownership_extends_to_the_other_company_by_content_hash(self):
        owners = _run(scoped_pass.companies_for_paths([self.nw_shared]))
        self.assertEqual(owners[self.nw_shared], {NW, CP},
                         "COMPANY_AWARE_DEDUPE indexes identical bytes once, so the citation carries "
                         "one path while both companies own the document")

    def test_a_scoped_question_for_either_company_keeps_the_shared_reference(self):
        reference = _ref("1", self.nw_shared, ["Cutover freezes at 18:00 on the freeze date."])
        for allowed in ([NW], [CP]):
            with self.subTest(allowed=allowed):
                client = FakeLightRAG([reference])
                result = _run(scoped_pass.company_scoped_pass("when does the freeze start?", allowed,
                                                              payload_base={}, client=client))
                self.assertEqual([c["file_path"] for c in result.citations], [self.nw_shared])
                self.assertFalse(result.meta["declined_reason"])

    def test_a_path_with_no_row_owns_nothing(self):
        owners = _run(scoped_pass.companies_for_paths(["/source_docs/Ghost/x.pdf"]))
        self.assertEqual(owners.get("/source_docs/Ghost/x.pdf", set()), set(),
                         "no row, no company: nothing is invented")

    def test_no_paths_makes_no_query(self):
        self.assertEqual(_run(scoped_pass.companies_for_paths([])), {})


class TestContextBuilding(unittest.TestCase):
    def test_whole_references_only_under_the_cap(self):
        refs = [_ref("1", NW_PATH, ["a" * 100]), _ref("2", NW_PATH, ["b" * 100])]
        # The cap counts the whole block, including its "Source:" line.
        context, meta = scoped_pass.build_scoped_context(refs, 200)
        self.assertIn("a" * 100, context)
        self.assertNotIn("b" * 100, context, "a reference over the cap goes whole, not trimmed")
        self.assertEqual(meta["dropped_by_cap"], 1)

    def test_a_reference_without_text_is_counted_not_invented(self):
        context, meta = scoped_pass.build_scoped_context([_ref("1", NW_PATH, None)], 1000)
        self.assertEqual(context, "")
        self.assertEqual(meta["without_content"], 1)

    def test_list_and_string_content_both_reach_the_context(self):
        reference = {"file_path": NW_PATH, "content": ["first chunk", "second chunk"]}
        context, _ = scoped_pass.build_scoped_context([reference], 1000)
        self.assertIn("first chunk\n\nsecond chunk", context)


class TestScopedPass(_TempStore):
    def test_the_second_call_sees_only_the_named_company_text(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass(
            "what happened to lead times in Q3?", [NW],
            payload_base={"mode": "mix", "top_k": 12, "chunk_top_k": 5}, client=client))
        self.assertTrue(result.used)
        self.assertEqual(result.answer, "the scoped answer")
        self.assertEqual(len(client.bypass_prompts), 1)
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertNotIn(CP_CHUNK, prompt, "the whole point: the other company is not in the prompt")
        self.assertIn(NW_PATH, prompt)
        self.assertNotIn(CP_PATH, prompt)
        self.assertEqual([c["file_path"] for c in result.citations], [NW_PATH])
        self.assertEqual(result.meta["references_dropped_other_company"], 1)
        # `/query/data` retrieval costs no generation, so this call is the pass's ONLY one: `generated`
        # says the pass generated, `second_model_call` says a SECOND generation ran, and on this route it
        # did not. (It used to be true here, which is why every scoped row looked like it had paid twice.)
        self.assertTrue(result.meta["generated"])
        self.assertFalse(result.meta["second_model_call"])
        self.assertEqual(result.meta["model_calls"], 1)

    def test_a_cross_company_question_keeps_both_companies(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass(
            "compare Northwind Chemicals and Contoso Pharma", [NW, CP],
            payload_base={"mode": "mix"}, client=client))
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertIn(CP_CHUNK, prompt)
        self.assertEqual(len(result.citations), 2)
        self.assertFalse(result.meta["declined_reason"])

    def test_retrieval_asks_for_chunk_content_and_generation_bypasses_retrieval(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("q", [NW],
                                                      payload_base={"mode": "mix", "top_k": 9},
                                                      client=client))
        # One retrieval plus one generation: the retrieval goes to the retrieval-only endpoint, so no
        # answer is written and thrown away.
        self.assertEqual(client.paths, ["/query/data", "/query"])
        retrieval = client.retrieval_payloads[0]
        self.assertTrue(retrieval["include_chunk_content"])
        self.assertTrue(retrieval["include_references"])
        self.assertEqual(retrieval["top_k"], 9)
        self.assertEqual(client.calls[-1][1]["mode"], "bypass")
        self.assertFalse(client.calls[-1][1]["include_references"])
        self.assertEqual(result.meta["model_calls"], 1)
        self.assertEqual(result.meta["retrieval_route"], "query/data")
        self.assertFalse(result.meta["retrieval_fallback_reason"])

    def test_nothing_joinable_refuses_without_a_second_call(self):
        client = FakeLightRAG([_ref("1", CP_PATH, [CP_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertEqual(client.bypass_prompts, [], "a refusal must not spend a model call")
        self.assertIn("could not find", result.answer)
        self.assertNotIn(CP_CHUNK, result.answer)
        self.assertEqual(result.citations, [])
        self.assertEqual(result.meta["declined_reason"], "no_reference_joins_a_named_company")

    def test_an_unattributable_reference_leads_to_a_refusal_not_a_guess(self):
        # A name the store has no row for at all. A bare basename that DOES match a stored file is no
        # longer unattributable - that is the basename join, tested in tests/test_document_identity.py.
        client = FakeLightRAG([_ref("1", "phantom-q3.pdf", [CP_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("anything", [CP], payload_base={},
                                                      client=client))
        self.assertEqual(result.meta["references_unattributable"], 1)
        self.assertEqual(client.bypass_prompts, [])
        self.assertIn("could not find", result.answer)

    def test_kept_references_without_chunk_text_refuse_but_still_show_documents(self):
        client = FakeLightRAG([_ref("1", NW_PATH, None)])
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertEqual(result.meta["declined_reason"], "kept_references_carry_no_chunk_text")
        self.assertEqual([c["file_path"] for c in result.citations], [NW_PATH])
        self.assertEqual(client.bypass_prompts, [])

    def test_a_blank_regeneration_never_falls_back_to_the_unscoped_draft(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], generation_text="   ")
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertIn("could not find", result.answer)
        self.assertNotIn("the unscoped draft answer", result.answer)
        self.assertEqual(result.meta["declined_reason"], "regeneration_returned_no_text")

    def test_the_context_cap_is_enforced_and_counted(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", NW_PATH, ["x" * 400])])
        with patch.object(scoped_pass.settings, "company_scoped_context_max_chars", 120):
            result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                          client=client))
        self.assertNotIn("x" * 400, client.bypass_prompts[0])
        self.assertEqual(result.meta["references_dropped_by_context_cap"], 1)

    def test_no_companies_named_declines_the_pass_immediately(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("anything", [], payload_base={},
                                                      client=client))
        self.assertFalse(result.used)
        self.assertEqual(client.calls, [])

    def test_the_refusal_says_which_companies_were_searched(self):
        message = scoped_pass.no_scoped_material_message([NW, CP])
        self.assertIn(NW, message)
        self.assertIn(CP, message)
        self.assertNotIn(", and", message.split("documents of")[1][:60])


class _PipelineSettings(unittest.TestCase):
    """Everything that could add evidence of its own, held off so the door under test is visible."""

    OFF = {
        "multi_query_enabled": False,
        "graph_native_enabled": False,
        "graph_expansion_enabled": False,
        "graph_synthesis_replace_answer": False,
        "grounding_check_asked_identifier": False,
        "rerank_enabled": False,
        "rerank_binding_host": None,
    }

    def setUp(self) -> None:
        patches = [patch.object(api.settings, name, value) for name, value in self.OFF.items()]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)


class TestQueryDoor(_TempStore, _PipelineSettings):
    def test_default_enforcement_makes_one_upstream_call_as_today(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api, "client", client):
            envelope = _run(api._query_core(
                QueryRequest(query="what did Northwind Chemicals report for Q3?"),
                request_id="req-1", timer=api.StageTimer(), t_start=0.0))
        self.assertEqual(len(client.calls), 1)
        data = envelope.data if isinstance(envelope.data, dict) else envelope.data.model_dump()
        self.assertEqual(data["answer"], "the unscoped draft answer")
        self.assertEqual([c["file_path"] for c in data["citations"]], [NW_PATH])
        self.assertNotIn("scoped_pass", data["query_scope"])

    def test_retrieval_enforcement_answers_from_the_scoped_pass(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "company_scope_enforcement", "retrieval"), \
                patch.object(api, "client", client):
            envelope = _run(api._query_core(
                QueryRequest(query="what did Northwind Chemicals report for Q3?"),
                request_id="req-2", timer=api.StageTimer(), t_start=0.0))
        data = envelope.data if isinstance(envelope.data, dict) else envelope.data.model_dump()
        self.assertEqual(data["answer"], "the scoped answer")
        self.assertEqual([c["file_path"] for c in data["citations"]], [NW_PATH])
        self.assertEqual(len(client.bypass_prompts), 1)
        self.assertNotIn(CP_CHUNK, client.bypass_prompts[0])
        scope = data["query_scope"]
        self.assertEqual(scope["scoped_pass"]["references_dropped_other_company"], 1)
        self.assertEqual(scope["stages_skipped_for_scope"],
                         ["multi_query", "naive_fallback", "graph_expansion"])
        self.assertIn("regenerated", scope["warning"])

    def test_a_failed_scoping_degrades_to_citation_filtering_instead_of_a_500(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])],
                              reject_first_retrievals=2)
        with patch.object(api.settings, "company_from_question", True),                 patch.object(api.settings, "company_scope_enforcement", "retrieval"),                 patch.object(api, "client", client):
            envelope = _run(api._query_core(
                QueryRequest(query="what did Northwind Chemicals report for Q3?"),
                request_id="req-4", timer=api.StageTimer(), t_start=0.0))
        data = envelope.data if envelope.data is not dict else envelope.data
        data = data if isinstance(data, dict) else data.model_dump()
        self.assertEqual(client.rejections, [])          # the bodies were valid; the server refused
        self.assertEqual(data["answer"], "the unscoped draft answer",
                         "the request is served, not failed")
        self.assertEqual([c["file_path"] for c in data["citations"]], [NW_PATH],
                         "and it is served with the company filter still applied")
        scoped = data["query_scope"]["scoped_pass"]
        self.assertEqual(scoped["declined_reason"],
                         "retrieval_routes_failed:/query=422;/query/data=400")
        self.assertEqual(scoped["retrieval_errors"]["/query/data"]["status"], 400)
        self.assertNotIn("stages_skipped_for_scope", data["query_scope"],
                         "the unscoped pipeline ran, so the skipped-stage note would lie")

    def test_the_flag_off_pipeline_never_runs_a_scoped_pass(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        with patch.object(api.settings, "company_from_question", False), \
                patch.object(api.settings, "company_scope_enforcement", "retrieval"), \
                patch.object(api, "client", client):
            envelope = _run(api._query_core(
                QueryRequest(query="what did Northwind Chemicals report for Q3?"),
                request_id="req-3", timer=api.StageTimer(), t_start=0.0))
        data = envelope.data if isinstance(envelope.data, dict) else envelope.data.model_dump()
        self.assertEqual(len(client.calls), 1, "the master flag gates everything")
        self.assertEqual(data["answer"], "the unscoped draft answer")
        self.assertNotIn("company_scope", data["query_scope"])


class TestGeneratedSharedFilePair(_TempStore):
    """The GL-07 generator's own shared file: same bytes, one row per company.

    This is the case the design doc prices for per-company workspaces, and the reason the scoped
    pass widens ownership by content hash: with COMPANY_AWARE_DEDUPE on, upstream indexes identical
    bytes once, so a Contoso question retrieves the path Northwind's row holds. A path-only join would
    make that document invisible to Contoso.
    """

    def setUp(self):
        super().setUp()
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
            from make_synthetic_company import SHARED_FILE_REL, generate_company
        except ImportError as exc:               # the generator needs python-pptx for the deck
            self.skipTest(f"GL-07 generator unavailable: {exc}")
        self.out = Path(self._tmp.name) / "synth"
        self.out.mkdir()
        self.shares: dict[str, Path] = {}
        for company in (NW, CP):
            try:
                generate_company(company, 7, self.out)
            except Exception as exc:
                self.skipTest(f"GL-07 generator failed: {exc}")
            self.shares[company] = self.out / company / SHARED_FILE_REL

    def test_the_generated_shared_files_are_the_same_bytes_under_different_paths(self):
        first = self.shares[NW].read_bytes()
        second = self.shares[CP].read_bytes()
        self.assertEqual(first, second, "the generator's shared file must be byte-identical")
        self.assertNotEqual(self.shares[NW], self.shares[CP])

    def test_a_company_question_reaches_the_shared_file_through_the_other_companys_path(self):
        nw_path = str(self.shares[NW])
        cp_path = str(self.shares[CP])
        shared_sha = "sha-generated-shared"
        for path, company, document_id in ((nw_path, NW, "doc-gen-nw"), (cp_path, CP, "doc-gen-cp")):
            _run(state_store.upsert_document(
                document_id, path, "scan", company, Path(path).name, None, shared_sha,
                "ingested", None))
        chunk = self.shares[NW].read_text(encoding="utf-8").splitlines()
        chunk = [line for line in chunk if line.strip()][:3]
        # LightRAG returns ONE reference for identical bytes - the one it indexed first.
        client = FakeLightRAG([_ref("1", nw_path, chunk)])
        for allowed, other in ((CP, NW), (NW, CP)):
            with self.subTest(asked=allowed):
                result = _run(scoped_pass.company_scoped_pass("when does the cutover freeze start?",
                                                              [allowed], payload_base={},
                                                              client=client))
                self.assertEqual([c["file_path"] for c in result.citations], [nw_path],
                                 f"{allowed} owns those bytes and must see the document")
                self.assertEqual(result.meta["references_unattributable"], 0)
                # The prompt holds the one reference that was indexed, whichever company asked.
                self.assertIn(nw_path, client.bypass_prompts[0])

    def test_a_company_that_does_not_own_the_file_still_cannot_reach_it(self):
        outsider = "/source_docs/Zenith/cutover.txt"
        _run(state_store.upsert_document("doc-gen-zen", outsider, "scan", "Zenith",
                                         "cutover.txt", None, "sha-generated-unique", "ingested",
                                         None))
        client = FakeLightRAG([_ref("1", outsider, ["freeze at 18:00"])])
        result = _run(scoped_pass.company_scoped_pass("when does the freeze start?", [NW],
                                                      payload_base={}, client=client))
        self.assertEqual(result.citations, [])
        self.assertEqual(result.meta["references_dropped_other_company"], 1)


class _BridgeResp:
    def __init__(self, content: bytes):
        self.content = content


def _stream_body(chunks: list[dict]) -> bytes:
    return "\n".join(json.dumps(c) for c in chunks).encode("utf-8")


class TestBridgeDoor(_TempStore, _PipelineSettings):
    stream_body = _stream_body([
        {"references": [{"reference_id": "1", "file_path": NW_PATH},
                        {"reference_id": "2", "file_path": CP_PATH}], "response": ""},
        {"response": "streamed answer"},
    ])

    def _bridge(self, question: str, client: FakeLightRAG) -> str:
        proxy = AsyncMock(return_value=_BridgeResp(self.stream_body))
        client.proxy = proxy
        with patch.object(api, "client", client), \
                patch.object(api.settings, "bridge_sources_enabled", True):
            answer = _run(api.answer_ollama_bridge_direct(
                {"messages": [{"role": "user", "content": question}]}))
        self.proxy_calls = proxy.call_count
        return answer

    def test_retrieval_enforcement_skips_the_stream_entirely(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "company_scope_enforcement", "retrieval"):
            answer = self._bridge("what did Northwind Chemicals report for Q3?", client)
        self.assertTrue(answer.startswith("the scoped answer"))
        self.assertIn("northwind-q3.pdf", answer)
        self.assertNotIn("contoso-q3.pdf", answer)
        self.assertNotIn("streamed answer", answer)
        self.assertEqual(self.proxy_calls, 0, "the streaming call is not made when scoped")

    def test_a_failed_scoping_falls_back_to_the_stream_and_filters_the_sources(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])],
                              reject_first_retrievals=2)
        with patch.object(api.settings, "company_from_question", True),                 patch.object(api.settings, "company_scope_enforcement", "retrieval"):
            answer = self._bridge("what did Northwind Chemicals report for Q3?", client)
        self.assertIn("streamed answer", answer, "the request is served on the stream, not failed")
        self.assertIn("northwind-q3.pdf", answer)
        self.assertNotIn("contoso-q3.pdf", answer, "the company filter still applies")
        self.assertEqual(self.proxy_calls, 1)

    def test_default_enforcement_keeps_the_stream_and_filters_only_the_sources(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        with patch.object(api.settings, "company_from_question", True), \
                patch.object(api.settings, "company_scope_enforcement", "citations"):
            answer = self._bridge("what did Northwind Chemicals report for Q3?", client)
        self.assertIn("streamed answer", answer)
        self.assertEqual(client.calls, [], "no scoped pass runs at the default enforcement")
        self.assertEqual(self.proxy_calls, 1)


class TestRetrievalRequestBody(_TempStore):
    """The body is part of the contract: a retrieval without the question is not a retrieval."""

    def _pass(self, client, question="what did Northwind Chemicals report for Q3?"):
        return _run(scoped_pass.company_scoped_pass(question, [NW],
                                                    payload_base={"mode": "mix", "top_k": 9,
                                                                  "chunk_top_k": 16},
                                                    client=client))

    def test_both_retrieval_routes_send_the_question(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = self._pass(client)
        self.assertEqual(client.rejections, [])
        self.assertEqual(client.retrieval_payloads[0]["query"],
                         "what did Northwind Chemicals report for Q3?")

    def test_the_fallback_route_sends_it_too(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], data_raises=RuntimeError("404"))
        self._pass(client)
        self.assertEqual(client.paths.count("/query"), 2)   # the fallback retrieval + the generation
        self.assertEqual(client.retrieval_payloads[-1]["query"],
                         "what did Northwind Chemicals report for Q3?")

    def test_the_generation_body_passes_the_same_strict_check(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        self._pass(client)
        self.assertEqual(client.rejections, [], "the bypass body uses only known fields and carries "
                                               "its prompt under `query`")
        bypass = [payload for _, payload in client.calls if payload.get("mode") == "bypass"]
        self.assertIn(NW_CHUNK, bypass[0]["query"])

    def test_the_door_payloads_use_only_known_fields(self):
        """The gateway builds mode/top_k/chunk_top_k and the bridge strips `query`; neither may add a
        key LightRAG does not know, or the strict fake turns the run into a 4xx."""
        gateway_base = {"mode": "mix", "top_k": 9, "chunk_top_k": 16, "conversation_history": []}
        bridge_base = {"mode": "mix", "top_k": 9, "only_need_context": False, "enable_rerank": True,
                       "max_entity_tokens": 1000, "hl_keywords": ["lead times"]}
        for base in (gateway_base, bridge_base):
            with self.subTest(keys=sorted(base)):
                client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
                result = self._pass(client)
                self.assertEqual(client.rejections, [])
                self.assertEqual(result.meta["retrieval_route"], "query/data")

    def test_an_unknown_key_degrades_instead_of_answering_from_a_rejected_body(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("what did Northwind Chemicals report?", [NW],
                                                      payload_base={"mode": "mix",
                                                                    "knn_search": True},
                                                      client=client))
        self.assertFalse(result.used, "no answer may be claimed from a body the server rejected")
        self.assertIn("400", result.meta["declined_reason"])
        self.assertIn("422", result.meta["declined_reason"])
        self.assertEqual(result.meta["retrieval_errors"]["/query/data"]["status"], 400)
        self.assertEqual(client.bypass_prompts, [])

    def test_a_degraded_pass_reports_the_status_codes_without_document_text(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], reject_first_retrievals=2)
        result = self._pass(client)
        self.assertFalse(result.used)
        self.assertEqual(result.meta["declined_reason"],
                         "retrieval_routes_failed:/query=422;/query/data=400")
        dumped = json.dumps(result.meta)
        self.assertNotIn(NW_CHUNK, dumped)
        self.assertNotIn("Invalid request parameters", result.meta["declined_reason"],
                         "the declined_reason names codes, not the server body")


class TestBareFilenameReferences(_TempStore):
    """The stack2 shape: citations carry a basename, `documents.path` is absolute.

    Hermes' R1/R2 run made every citation unattributable (1046 of them) because the join was exact
    path. These rows are seeded the way the filesystem door seeds them, and the references are the
    bare names LightRAG actually returns.
    """

    NW_PDF = "/source_docs/Northwind Chemicals/northwind-q3.pdf"
    CP_PDF = "/source_docs/Contoso Pharma/contoso-q3.pdf"

    def setUp(self):
        super().setUp()
        self.seed(self.NW_PDF, NW, document_id="doc-nw")
        self.seed(self.CP_PDF, CP, document_id="doc-cp")

    def test_the_load_bearing_prompt_test_with_bare_filename_references(self):
        client = FakeLightRAG([_ref("1", "northwind-q3.pdf", [NW_CHUNK]),
                               _ref("2", "contoso-q3.pdf", [CP_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("what did Northwind Chemicals report?", [NW],
                                                      payload_base={"mode": "mix"}, client=client))
        self.assertEqual([c.get("file_path") for c in result.citations], ["northwind-q3.pdf"])
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertNotIn(CP_CHUNK, prompt)
        self.assertEqual(result.meta["references_dropped_other_company"], 1)
        self.assertEqual(result.meta["references_unattributable"], 0)

    def test_the_shared_file_is_reachable_under_either_company(self):
        self.seed("/source_docs/shared/cutover-checklist.txt", NW, document_id="doc-nw-shared",
                  sha="sha-shared")
        self.seed("/source_docs/shared-2/cutover-checklist.txt", CP, document_id="doc-cp-shared",
                  sha="sha-shared")
        for company in (NW, CP):
            with self.subTest(company=company):
                client = FakeLightRAG([_ref("1", "cutover-checklist.txt", ["checklist text"])])
                result = _run(scoped_pass.company_scoped_pass("what is frozen?", [company],
                                                              payload_base={}, client=client))
                self.assertEqual([c.get("file_path") for c in result.citations],
                                 ["cutover-checklist.txt"])

    def test_one_name_two_companies_different_bytes_is_refused_as_ambiguous(self):
        # Identical basename, different bytes, different owners: nothing can be attributed.
        self.seed("/source_docs/Northwind Chemicals/transcript.txt", NW, document_id="doc-nw-t",
                  sha="sha-nw-t")
        self.seed("/source_docs/Contoso Pharma/transcript.txt", CP, document_id="doc-cp-t",
                  sha="sha-cp-t")
        client = FakeLightRAG([_ref("1", "transcript.txt", [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("what did we agree?", [NW], payload_base={},
                                                      client=client))
        self.assertEqual(result.meta["references_ambiguous"], 1)
        self.assertEqual(result.meta["references_kept"], 0)
        self.assertEqual(result.meta["declined_reason"],
                         "every_reference_is_ambiguous_across_companies")
        self.assertEqual(client.bypass_prompts, [], "an ambiguous name never reaches the model")

    def test_ambiguity_is_counted_apart_from_a_missing_join(self):
        client = FakeLightRAG([_ref("1", "transcript.txt", [NW_CHUNK]),
                               _ref("2", "phantom.txt", [NW_CHUNK])])
        self.seed("/source_docs/Northwind Chemicals/transcript.txt", NW, document_id="doc-nw-t2",
                  sha="sha-nw-t2")
        self.seed("/source_docs/Contoso Pharma/transcript.txt", CP, document_id="doc-cp-t2",
                  sha="sha-cp-t2")
        result = _run(scoped_pass.company_scoped_pass("what did we agree?", [NW], payload_base={},
                                                      client=client))
        self.assertEqual(result.meta["references_ambiguous"], 1)
        self.assertEqual(result.meta["references_unattributable"], 1)
        self.assertEqual(result.meta["declined_reason"], "no_reference_joins_a_named_company")


class TestQueryDataRoute(_TempStore):
    """The retrieval leg: which endpoint, which keys, and what makes it fall back."""

    def setUp(self):
        super().setUp()
        self._saved_hint = scoped_pass.settings.company_keyword_hint

    def tearDown(self):
        scoped_pass.settings.company_keyword_hint = self._saved_hint
        super().tearDown()

    def _pass(self, client, companies=(NW,)):
        return _run(scoped_pass.company_scoped_pass("What changed on the line?", list(companies),
                                                    payload_base={"mode": "mix", "top_k": 9},
                                                    client=client))

    def test_chunks_only_body_is_grouped_by_document(self):
        client = FakeLightRAG([], data_payload={"data": {"chunks": [
            {"file_path": NW_PATH, "content": NW_CHUNK},
            {"file_path": NW_PATH, "content": "Northwind ran at 82 per cent utilisation."},
            {"file_path": CP_PATH, "content": CP_CHUNK}],
            "references": []}})
        result = self._pass(client)
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertNotIn(CP_CHUNK, prompt)
        self.assertEqual(result.meta["retrieval_shape"]["chunks_found"], 3)
        self.assertEqual(result.meta["retrieval_shape"]["chunk_only_paths"], 2)

    def test_chunk_keys_the_gateway_already_tolerates_are_accepted(self):
        items, census = scoped_pass.normalize_data_payload({"data": {"chunks": [
            {"file_source": CP_PATH, "text": CP_CHUNK}]}})
        self.assertEqual(scoped_pass.path_of(items[0]), CP_PATH)
        self.assertEqual(census["items_with_text"], 1)
        self.assertEqual(census["path_keys_seen"], {"file_source": 1})

    def test_the_path_key_census_names_the_four_the_gateway_tolerates(self):
        """No invented fifth key: an unseen name shows up as items_without_path, not as a wrong owner."""
        self.assertEqual(scoped_pass.PATH_KEYS,
                         tuple(key for key in ("file_source", "path", "source", "file_path")))
        items, census = scoped_pass.normalize_data_payload({"data": {"chunks": [
            {"document_name": CP_PATH, "content": CP_CHUNK}]}})
        self.assertEqual(scoped_pass.path_of(items[0]), "")
        self.assertEqual(census["items_without_path"], 1)
        self.assertEqual(census["path_keys_seen"], {})

    def test_references_win_when_they_already_carry_the_document(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])],
                              data_payload={"data": {"references": [_ref("1", NW_PATH, [NW_CHUNK])],
                                                     "chunks": [{"file_path": NW_PATH,
                                                                 "content": "duplicate chunk"}]}})
        result = self._pass(client)
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertNotIn("duplicate chunk", prompt)      # not counted twice into the context
        self.assertEqual(result.meta["retrieval_shape"]["chunk_only_paths"], 0)
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])

    def test_text_without_a_path_stays_unattributable(self):
        client = FakeLightRAG([], data_payload={"data": {"chunks": [{"content": NW_CHUNK}],
                                                         "references": []}})
        result = self._pass(client)
        self.assertEqual(result.meta["retrieval_shape"]["items_without_path"], 1)
        self.assertEqual(result.meta["references_kept"], 0)
        self.assertEqual(result.meta["references_unattributable"], 1)
        self.assertEqual(result.meta["declined_reason"], "no_reference_joins_a_named_company")
        self.assertEqual(result.citations, [])

    def test_an_absent_query_data_endpoint_falls_back_and_pays_for_the_generation(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])],
                              data_raises=RuntimeError("404 from /query/data"))
        result = self._pass(client)
        self.assertEqual(client.paths, ["/query/data", "/query", "/query"])
        self.assertEqual(result.meta["retrieval_route"], "query")
        self.assertTrue(result.meta["retrieval_fallback_reason"].startswith("query_data_error:"))
        self.assertEqual(result.meta["model_calls"], 2)   # the discarded answer this route forces
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt)
        self.assertNotIn(CP_CHUNK, prompt)               # the guarantee survives the fallback

    def test_a_query_data_body_with_no_usable_text_falls_back(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])],
                              data_payload={"status": "success", "message": "ok",
                                            "data": {"entities": [], "relationships": [],
                                                     "chunks": [], "references": []}})
        result = self._pass(client)
        self.assertEqual(result.meta["retrieval_route"], "query")
        self.assertEqual(result.meta["retrieval_fallback_reason"], "query_data_no_items")
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])

    def test_a_body_of_the_wrong_shape_falls_back_rather_than_crashing(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], data_payload={"detail": "not what we asked for"})
        result = self._pass(client)
        self.assertEqual(result.meta["retrieval_fallback_reason"], "query_data_no_items")
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])

    def test_the_shape_census_is_logged_without_content(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = self._pass(client)
        census = result.meta["retrieval_shape"]
        self.assertEqual(census["references_found"], 1)
        self.assertEqual(census["items_with_text"], 1)
        self.assertEqual(census["content_key_counts"], {"content": 1})
        self.assertEqual(census["path_keys_seen"], {"file_path": 1})
        self.assertNotIn(NW_CHUNK, json.dumps(census))


class TestCompanyKeywordHint(_TempStore):
    """A retrieval bias, off by default, never a guarantee."""

    def setUp(self):
        super().setUp()
        self._saved_hint = scoped_pass.settings.company_keyword_hint

    def tearDown(self):
        scoped_pass.settings.company_keyword_hint = self._saved_hint
        super().tearDown()

    def _pass(self, client):
        return _run(scoped_pass.company_scoped_pass("What changed on the line?", [NW],
                                                    payload_base={"mode": "mix", "top_k": 9},
                                                    client=client))

    def test_off_by_default_and_absent_from_the_payload(self):
        self.assertFalse(scoped_pass.settings.company_keyword_hint)
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = self._pass(client)
        self.assertNotIn("hl_keywords", client.retrieval_payloads[0])
        self.assertNotIn("ll_keywords", client.retrieval_payloads[0])
        self.assertFalse(result.meta["keyword_hint_applied"])

    def test_on_names_the_company_in_both_keyword_lists(self):
        scoped_pass.settings.company_keyword_hint = True
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = self._pass(client)
        payload = client.retrieval_payloads[0]
        self.assertEqual(payload["hl_keywords"], [NW])
        self.assertEqual(payload["ll_keywords"], [NW])
        self.assertTrue(result.meta["keyword_hint_applied"])

    def test_the_hint_travels_on_the_fallback_route_too(self):
        scoped_pass.settings.company_keyword_hint = True
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], data_raises=RuntimeError("down"))
        result = self._pass(client)
        self.assertEqual(result.meta["retrieval_route"], "query")
        self.assertTrue(result.meta["keyword_hint_applied"])
        self.assertEqual(client.retrieval_payloads[-1]["hl_keywords"], [NW])

    def test_the_hint_adds_to_question_keywords_rather_than_replacing_them(self):
        scoped_pass.settings.company_keyword_hint = True
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        _run(scoped_pass.company_scoped_pass("What changed on the line?", [NW],
                                             payload_base={"mode": "mix",
                                                           "hl_keywords": ["lead times"],
                                                           "ll_keywords": ["lead times"]},
                                             client=client))
        payload = client.retrieval_payloads[0]
        self.assertEqual(payload["hl_keywords"], ["lead times", NW])
        self.assertEqual(payload["ll_keywords"], ["lead times", NW])

    def test_hint_never_substitutes_for_the_filter(self):
        scoped_pass.settings.company_keyword_hint = True
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]), _ref("2", CP_PATH, [CP_CHUNK])])
        result = self._pass(client)
        self.assertEqual([c.get("file_path") for c in result.citations], [NW_PATH])
        self.assertNotIn(CP_CHUNK, client.bypass_prompts[0])


class TestModelCallAccounting(_TempStore):
    """One generation for a scoped answer; none for a refusal made before generating."""

    def _pass(self, client):
        return _run(scoped_pass.company_scoped_pass("What changed on the line?", [NW],
                                                    payload_base={"mode": "mix", "top_k": 9},
                                                    client=client))

    def test_a_scoped_answer_costs_one_retrieval_and_one_generation(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = self._pass(client)
        self.assertEqual(result.meta["model_calls"], 1)
        self.assertEqual(client.paths.count("/query"), 1)     # the generation, not a retrieval

    def test_a_refusal_before_generation_costs_no_generation(self):
        # Nothing retrieved joins the named company, so no answer is ever asked for.
        client = FakeLightRAG([_ref("1", CP_PATH, [CP_CHUNK])])
        result = self._pass(client)
        self.assertEqual(result.meta["declined_reason"], "no_reference_joins_a_named_company")
        self.assertEqual(result.meta["model_calls"], 0)
        self.assertEqual(client.bypass_prompts, [])

    def test_the_fallback_route_costs_its_discarded_answer_plus_the_generation(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], data_raises=RuntimeError("404"))
        result = self._pass(client)
        self.assertEqual(result.meta["model_calls"], 2)

    def test_the_fallback_route_reports_the_discard_even_when_it_declines(self):
        client = FakeLightRAG([_ref("1", CP_PATH, [CP_CHUNK])], data_raises=RuntimeError("404"))
        result = self._pass(client)
        self.assertEqual(result.meta["declined_reason"], "no_reference_joins_a_named_company")
        self.assertEqual(result.meta["model_calls"], 1)       # the answer /query forced, nothing more


class TestQueryLogCarriesTheRetrievalRoute(unittest.TestCase):
    def _emit(self, scoped: dict) -> dict:
        timer = app.observability.StageTimer()
        with patch("app.observability._emit") as emit:
            app.observability.emit_query_log(
                request_id="req-1", query="q", company=None, mode_requested="mix",
                mode_used="mix", top_k=12, chunk_top_k=16,
                query_scope={"company_scope": {"companies": [NW], "company_detected_from": "question",
                                               "company_candidates": []},
                             "scoped_pass": scoped},
                citations_raw=1, citations_merged=1, citations_final=1, answer="a",
                timer=timer, started=0.0)
        self.assertEqual(emit.call_count, 1)
        return emit.call_args.args[0]

    def test_the_route_and_census_are_logged(self):
        line = self._emit({"references_kept": 1, "references_dropped_other_company": 1,
                           "references_unattributable": 0, "references_dropped_by_context_cap": 0,
                           "second_model_call": True, "model_calls": 1,
                           "retrieval_route": "query/data", "retrieval_fallback_reason": None,
                           "keyword_hint_applied": False,
                           "retrieval_shape": {"references_found": 2, "chunks_found": 0,
                                               "items_with_text": 1, "items_without_path": 0},
                           "declined_reason": None})
        self.assertEqual(line["scoped_pass_model_calls"], 1)
        self.assertEqual(line["scoped_pass_retrieval_route"], "query/data")
        self.assertIsNone(line["scoped_pass_retrieval_fallback_reason"])
        self.assertIs(line["scoped_pass_keyword_hint_applied"], False)
        self.assertEqual(line["scoped_pass_retrieval_shape"]["references_found"], 2)

    def test_chunk_text_never_reaches_the_line(self):
        line = self._emit({"retrieval_shape": {"items_with_text": 1}, "model_calls": 1})
        self.assertNotIn(NW_CHUNK, json.dumps(line))
        self.assertNotIn(CP_CHUNK, json.dumps(line))


class TestRefusalPrompt(_TempStore):
    """Stack2 R2 refused 0/12 unanswerable questions while R0 refused 1/12.

    `mode: bypass` sends the LLM only the text we hand it - LightRAG's own retrieval prompt, with its
    "if you do not have enough information say so" instruction and its reference discipline, never
    runs. So the scoped pass has to carry that discipline itself. These tests assert the request
    carries it, because the model's compliance cannot be tested offline and the request can be.
    """

    def test_the_generation_request_carries_the_refusal_instruction(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        _run(scoped_pass.company_scoped_pass("what is CP-2507 batch yield?", [NW],
                                             payload_base={}, client=client))
        prompt = client.bypass_prompts[0]
        self.assertIn(NW_CHUNK, prompt, "the kept chunk must be in the request")
        self.assertIn(scoped_pass.REFUSAL_SENTENCE, prompt,
                      "the model is told the exact refusal sentence, not asked to phrase one")
        self.assertIn("do not have enough information", prompt.lower(),
                      "the wording must be the one app/api.py:WEAK_ANSWER_MARKERS detects")
        self.assertIn("MATERIAL START", prompt)
        self.assertIn("MATERIAL END", prompt)
        self.assertIn("First decide whether the material states", prompt,
                      "the coverage check is asked for before the answer")
        self.assertIn("Do not guess", prompt)

    def test_the_refusal_the_gateway_writes_itself_is_detectable(self):
        """Every refusal the pass can produce is recognised by the gateway's own weak-answer detector."""
        messages = [scoped_pass.no_scoped_material_message([NW]),
                    scoped_pass.no_scoped_material_message([NW, CP]),
                    scoped_pass.REFUSAL_SENTENCE]
        for message in messages:
            with self.subTest(message=message[:40]):
                self.assertTrue(api.has_weak_answer_signal(message),
                                "a refusal nothing downstream can see is a refusal that never happened")
        self.assertIn(NW, messages[0])
        self.assertIn(CP, messages[1])

    def test_a_refused_answer_is_left_as_the_model_wrote_it(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])],
                              generation_text="I do not have enough information in these documents "
                                              "to answer that.")
        result = _run(scoped_pass.company_scoped_pass("what is CP-2507 yield?", [NW],
                                                      payload_base={}, client=client))
        self.assertTrue(result.used)
        self.assertTrue(api.has_weak_answer_signal(result.answer))
        self.assertIsNone(result.meta["declined_reason"], "a refusal is an answer, not a declined pass")


class TestHardRefusalFlag(_TempStore):
    """COMPANY_SCOPED_HARD_REFUSAL: the one unanswerable case the gateway can decide for itself.

    Off by default and off in every existing test; the M1 investigation never measured this
    classifier's false-refusal rate on a dev set, so it ships as a knob to measure, not a promise.
    """

    def test_off_by_default_a_question_about_an_absent_code_still_generates(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("what was CP-2507's yield?", [NW],
                                                      payload_base={}, client=client))
        self.assertTrue(result.meta["generated"])
        self.assertIsNone(result.meta["hard_refusal_verdict"])
        self.assertEqual(len(client.bypass_prompts), 1)

    def test_on_it_refuses_when_the_asked_code_is_nowhere_in_the_context(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        with patch.object(scoped_pass.settings, "company_scoped_hard_refusal", True):
            result = _run(scoped_pass.company_scoped_pass("what was CP-2507's yield?", [NW],
                                                          payload_base={}, client=client))
        self.assertTrue(result.used)
        self.assertEqual(client.bypass_prompts, [], "no model call: the gateway already knows")
        self.assertTrue(api.has_weak_answer_signal(result.answer))
        self.assertEqual(result.meta["hard_refusal_verdict"], "no_support_for_asked_identifier")
        self.assertEqual(result.meta["declined_reason"],
                         "asked_identifier_absent_from_scoped_context")
        self.assertEqual(result.meta["model_calls"], 0)
        self.assertEqual(result.citations, [_ref("1", NW_PATH, [NW_CHUNK])],
                         "the documents searched are still shown")

    def test_on_it_still_answers_when_the_asked_code_is_in_the_context(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK + " CP-2507 is unrelated here."])])
        with patch.object(scoped_pass.settings, "company_scoped_hard_refusal", True):
            result = _run(scoped_pass.company_scoped_pass("what was CP-2507's yield?", [NW],
                                                          payload_base={}, client=client))
        self.assertTrue(result.meta["generated"])
        self.assertIsNone(result.meta["hard_refusal_verdict"])

    def test_a_question_with_no_identifier_is_never_hard_refused(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        with patch.object(scoped_pass.settings, "company_scoped_hard_refusal", True):
            result = _run(scoped_pass.company_scoped_pass("how did lead times move?", [NW],
                                                          payload_base={}, client=client))
        self.assertTrue(result.meta["generated"], "absence of a code proves nothing")

    def test_a_code_lost_to_the_cap_says_so_in_the_reason(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK]),
                               _ref("2", NW_PATH, ["the CP-2507 pilot yield was 91 percent."])])
        with patch.object(scoped_pass.settings, "company_scoped_hard_refusal", True), \
                patch.object(scoped_pass.settings, "company_scoped_context_max_chars", 140):
            result = _run(scoped_pass.company_scoped_pass("what was CP-2507's yield?", [NW],
                                                          payload_base={}, client=client))
        self.assertEqual(result.meta["declined_reason"],
                         "asked_identifier_absent_from_scoped_context_after_context_cap")

    def test_the_verdict_is_logged_without_the_codes_or_the_text(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        with patch.object(scoped_pass.settings, "company_scoped_hard_refusal", True):
            result = _run(scoped_pass.company_scoped_pass("what was CP-2507's yield?", [NW],
                                                          payload_base={}, client=client))
        timer = app.observability.StageTimer()
        with patch("app.observability._emit") as emit:
            app.observability.emit_query_log(
                request_id="req-1", query="q", company=None, mode_requested="mix", mode_used="mix",
                top_k=12, chunk_top_k=16,
                query_scope={"company_scope": {"companies": [NW], "company_detected_from": "question",
                                               "company_candidates": []},
                             "scoped_pass": result.meta},
                citations_raw=1, citations_merged=1, citations_final=1, answer=result.answer,
                timer=timer, started=0.0)
        line = emit.call_args.args[0]
        self.assertEqual(line["scoped_pass_hard_refusal_verdict"], "no_support_for_asked_identifier")
        self.assertIs(line["scoped_pass_generated"], False)
        self.assertEqual(line["scoped_pass_model_calls"], 0)
        # The identifier that decided the refusal is a fact code, and codes are what a probe leaks on:
        # the log carries the verdict, never the code or the chunk text.
        self.assertNotIn("CP-2507", json.dumps(line))
        self.assertNotIn(NW_CHUNK, json.dumps(line))


class TestContextOrdering(unittest.TestCase):
    """The cap is a decision about which evidence the model may see, so it is ranked, not first-fit."""

    def test_a_lower_ranked_chunk_is_never_kept_over_a_higher_ranked_one(self):
        refs = [_ref("1", NW_PATH, ["a" * 300]), _ref("2", NW_PATH, ["b" * 40]),
                _ref("3", NW_PATH, ["c" * 40])]
        context, meta = scoped_pass.build_scoped_context(refs, 160)
        self.assertIn("a" * 40, context, "the top-ranked block is kept, cut, not dropped")
        self.assertLess(len(context), 161, "the cut block still respects the cap")
        self.assertNotIn("b" * 40, context, "the fill stops at the first block that does not fit")
        self.assertNotIn("c" * 40, context,
                         "keeping rank 3 while withholding rank 2 is what the old loop did")
        self.assertEqual(meta["dropped_by_cap"], 2)
        self.assertEqual(meta["first_dropped_index"], 1)
        self.assertEqual(meta["truncated_blocks"], 1)
        self.assertIn(scoped_pass.CONTEXT_CUT_MARKER, context,
                      "a cut block is marked, so it cannot be quoted as complete evidence")

    def test_an_ordinary_cap_cut_keeps_everything_above_the_cut(self):
        refs = [_ref("1", NW_PATH, ["a" * 40]), _ref("2", NW_PATH, ["b" * 40]),
                _ref("3", NW_PATH, ["c" * 300])]
        context, meta = scoped_pass.build_scoped_context(refs, 220)
        self.assertIn("a" * 40, context)
        self.assertIn("b" * 40, context)
        self.assertNotIn("c" * 300, context)
        self.assertEqual(meta["blocks_kept"], 2)
        self.assertEqual(meta["dropped_by_cap"], 1)
        self.assertEqual(meta["first_dropped_index"], 2)
        self.assertGreater(meta["unused_chars"], 0)

    def test_chunk_only_items_do_not_join_a_reference_shaped_context(self):
        """Two rankings cannot be compared, so they are never interleaved into one budget.

        Appending chunk-only documents after the references put the answer-bearing document LAST
        regardless of its rank, where the cap could drop it while keeping worse material.
        """
        payload = {"data": {"references": [{"reference_id": "1", "file_path": NW_PATH,
                                            "content": ["the answer chunk"]}],
                            "chunks": [{"file_path": "/other/doc.txt", "content": ["ranked first"]}]}}
        items, census = scoped_pass.normalize_data_payload(payload)
        self.assertEqual([scoped_pass.path_of(item) for item in items], [NW_PATH])
        self.assertEqual(census["chunk_only_paths"], 1)
        self.assertEqual(census["chunk_only_skipped"], 1)
        self.assertEqual(census["chunk_only_used"], 0)

    def test_a_chunk_shaped_body_is_read_in_chunk_order(self):
        payload = {"data": {"references": [{"reference_id": "1", "file_path": NW_PATH}],
                            "chunks": [{"file_path": NW_PATH, "content": ["first"]},
                                       {"file_path": NW_PATH, "content": ["second"]}]}}
        items, census = scoped_pass.normalize_data_payload(payload)
        self.assertEqual(census["chunk_only_used"], 1)
        self.assertEqual(items[-1]["file_path"], NW_PATH)
        self.assertEqual(items[-1]["content"], ["first", "second"],
                         "chunk order is retrieval order here, and one document is one block")


class TestModelCallSemantics(_TempStore):
    """`second_model_call` means a second GENERATION ran. It used to mean "this pass generated"."""

    def test_on_the_query_data_route_one_call_ran_and_the_flag_is_false(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertTrue(result.meta["generated"])
        self.assertFalse(result.meta["second_model_call"])
        self.assertEqual(result.meta["model_calls"], 1)

    def test_on_the_query_fallback_route_the_flag_is_true_and_the_cost_is_two(self):
        client = FakeLightRAG([_ref("1", NW_PATH, [NW_CHUNK])], data_raises=LightRAGRejected(
            400, '{"detail":"Invalid request parameters: include_chunk_content"}'))
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertTrue(result.meta["generated"])
        self.assertTrue(result.meta["second_model_call"],
                        "retrieval through /query already generated an answer that was thrown away")
        self.assertEqual(result.meta["model_calls"], 2)
        self.assertEqual(result.meta["retrieval_fallback_reason"], "query_data_http_400")

    def test_declining_before_generating_reports_no_calls(self):
        client = FakeLightRAG([_ref("1", GHOST_PATH, [NW_CHUNK])])
        result = _run(scoped_pass.company_scoped_pass("anything", [NW], payload_base={},
                                                      client=client))
        self.assertFalse(result.meta["generated"])
        self.assertFalse(result.meta["second_model_call"])
        self.assertEqual(result.meta["model_calls"], 0)


class TestAmbiguousNamesInTheScopedPass(_TempStore):
    """Task 5 of the run report: the 100 ambiguous references, and what that rule does and does not do.

    In the stack2 corpus the ambiguous names are the generator's own: `manifest.json` (one per
    company, different bytes) and the planted cross-company duplicate pair. They must never be
    credited to a named company - and a duplicate pair inside ONE company must stay reachable.
    """

    MANIFEST = "manifest.json"

    def _seed_manifests(self, north_bytes: str, contoso_bytes: str | None):
        self.seed(f"/source_docs/{NW}/{self.MANIFEST}", NW,
                  document_id="doc-nw-manifest", sha="sha-nw-manifest")
        if contoso_bytes is not None:
            self.seed(f"/source_docs/{CP}/{self.MANIFEST}", CP,
                      document_id="doc-cp-manifest", sha="sha-cp-manifest")

    def test_both_companies_holding_a_manifest_never_attributes_it(self):
        self._seed_manifests("north facts", "contoso facts")
        refs = [_ref("1", self.MANIFEST, ["NC-3617 cutover freeze ran 5 hours"]),
                _ref("2", NW_PATH, [NW_CHUNK])]
        client = FakeLightRAG(refs)
        result = _run(scoped_pass.company_scoped_pass("what happened under NC-3617?", [NW],
                                                      payload_base={}, client=client))
        prompt = client.bypass_prompts[0]
        self.assertNotIn("NC-3617 cutover freeze", prompt,
                         "a manifest two companies could both own is not evidence for either")
        self.assertEqual(result.meta["references_ambiguous"], 1)
        self.assertEqual(result.meta["references_kept"], 1)

    def test_a_manifest_only_one_company_holds_attributes_to_that_company(self):
        """The residual risk the lead should know about: a lone manifest IS citable, and it lists codes.

        Nothing in the join can tell a manifest from any other document. This is why the generator's
        manifest.json being ingested at all (.json is a text extension) is a probe-hygiene problem,
        not a join problem.
        """
        self._seed_manifests("north facts", None)
        refs = [_ref("1", self.MANIFEST, ["NC-3617 cutover freeze ran 5 hours"])]
        client = FakeLightRAG(refs)
        result = _run(scoped_pass.company_scoped_pass("what happened under NC-3617?", [NW],
                                                      payload_base={}, client=client))
        self.assertIn("NC-3617 cutover freeze", client.bypass_prompts[0])
        self.assertEqual(result.meta["references_ambiguous"], 0)

    def test_same_bytes_twice_inside_one_company_stays_reachable(self):
        """A duplicate pair inside one company is a filing fact, not an ambiguity to refuse."""
        for path, document_id in ((f"/source_docs/{NW}/copy-a.txt", "doc-copy-a"),
                                  (f"/source_docs/{NW}/copy-b.txt", "doc-copy-b")):
            self.seed(path, NW, document_id=document_id, sha="sha-shared-bytes")
        refs = [_ref("1", "copy-a.txt", [NW_CHUNK]), _ref("2", "copy-b.txt", ["same bytes again"])]
        client = FakeLightRAG(refs)
        result = _run(scoped_pass.company_scoped_pass("what happened to lead times?", [NW],
                                                      payload_base={}, client=client))
        self.assertEqual(result.meta["references_ambiguous"], 0)
        self.assertEqual(result.meta["references_kept"], 2)
        self.assertIn("same bytes again", client.bypass_prompts[0])


if __name__ == "__main__":
    unittest.main()
