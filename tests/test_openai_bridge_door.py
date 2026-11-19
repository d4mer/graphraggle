"""The gateway's OpenAI-compatible door: the only Open WebUI path that can carry citations.

Two layers of proof, matching how the rest of the bridge is tested:

* pure helpers in app/openai_bridge.py (annotation shape, the skip reasons, the
  chunk shape Open WebUI 0.12.0 reads), and
* the door itself through the real ASGI app with the strict upstream fake, so
  stream/non-stream, 0 references, many references, the history strip and the
  emitted log lines are all observed as the client sees them.

Every answer here is produced by the existing bridge; the tests assert the door
re-packages it without touching it.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import AsyncMock, patch

import httpx

import app.api as api
from app.openai_bridge import (
    BASE_URL_UNSET,
    CITATIONS_OFF,
    NO_REFERENCES,
    OPENAI_BRIDGE_MODEL,
    chat_completion_chunk,
    citation_annotations,
    openai_models_payload,
    openai_request_invalid_reason,
    sse_event,
)


def _run_async(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _stream_body(chunks: list[dict]) -> bytes:
    return "\n".join(json.dumps(c) for c in chunks).encode("utf-8")


class _Resp:
    def __init__(self, content: bytes, status_code: int = 200):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": "application/json"}


def _upstream(answer: str, references: list[dict]) -> _Resp:
    """A /query/stream body in the shape LightRAG actually sends: references on
    the first line, answer text in the following ones."""
    first = {"references": references, "response": ""}
    return _Resp(_stream_body([first, {"response": answer}]))


def _refs(names: list[str]) -> list[dict]:
    return [{"reference_id": str(i + 1), "file_path": f"docs/{n}"} for i, n in enumerate(names)]


def _door_json(lines: str) -> list[dict]:
    """Parse an SSE body into the events Open WebUI would parse (one per chunk)."""
    events = []
    for line in lines.split("\n\n"):
        line = line.strip()
        if not line:
            continue
        assert line.startswith("data:"), f"chunk is not an SSE event: {line!r}"
        payload = line[len("data:"):].strip()
        events.append(payload if payload == "[DONE]" else json.loads(payload))
    return events


def _log_lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


class DoorSettingsMixin(unittest.TestCase):
    DOOR_SETTINGS = (
        "bridge_backend",
        "bridge_sources_enabled",
        "bridge_history_turns",
        "bridge_task_shortcircuit",
        "bridge_keyword_supply",
        "bridge_rewrite_enabled",
        "bridge_empty_retries",
        "bridge_citations_enabled",
        "bridge_citation_base_url",
        "bridge_citation_limit",
        "rag_api_key",
    )

    def setUp(self):
        self.settings = api.settings
        self._saved = {name: getattr(self.settings, name) for name in self.DOOR_SETTINGS}
        # Prod values for the bridge itself, so the door is tested on the path
        # production runs (backend lightrag_direct, sources on, history 3).
        # The keyword and rewrite steps are pinned off for determinism - the
        # socket guard keeps the suite offline. They are ON in prod and run
        # inside the same bridge answer call either way, so the door cannot
        # diverge from the Ollama door on them; the byte-identity test below
        # compares the two doors under identical settings.
        self.settings.bridge_backend = "lightrag_direct"
        self.settings.bridge_sources_enabled = True
        self.settings.bridge_history_turns = 3
        self.settings.bridge_task_shortcircuit = True
        self.settings.bridge_keyword_supply = False
        self.settings.bridge_rewrite_enabled = False
        self.settings.bridge_empty_retries = 0
        self.settings.rag_api_key = "test-door-key"

    def tearDown(self):
        for name, value in self._saved.items():
            setattr(self.settings, name, value)

    # ── helpers ─────────────────────────────────────────────────────────
    def _post_door(self, body: dict, *, auth: bool = True) -> httpx.Response:
        headers = {"Authorization": "Bearer test-door-key"} if auth else {}

        async def call():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                return await client.post("/v1/chat/completions", json=body, headers=headers)

        return _run_async(call())

    def _get_models(self, *, auth: bool = True) -> httpx.Response:
        headers = {"Authorization": "Bearer test-door-key"} if auth else {}

        async def call():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                return await client.get("/v1/models", headers=headers)

        return _run_async(call())

    def _messages(self, *turns: tuple[str, str]) -> list[dict]:
        return [{"role": role, "content": content} for role, content in turns]

    def _request(self, *turns: tuple[str, str], stream: bool = True) -> dict:
        return {"model": OPENAI_BRIDGE_MODEL, "messages": self._messages(*turns), "stream": stream}


class TestPureHelpers(unittest.TestCase):
    def test_models_advertises_one_model_that_is_not_the_ollama_id(self):
        payload = openai_models_payload()
        ids = [entry["id"] for entry in payload["data"]]
        self.assertEqual(ids, [OPENAI_BRIDGE_MODEL])
        self.assertNotEqual(OPENAI_BRIDGE_MODEL, api.OLLAMA_BRIDGE_MODEL)

    def test_request_validation_ignores_unknown_parameters(self):
        body = {
            "model": OPENAI_BRIDGE_MODEL,
            "messages": [{"role": "user", "content": "What is eCommit?"}],
            "stream": True,
            "temperature": 0.7,
            "max_tokens": 900,
            "stream_options": {"include_usage": True},
            "user": "operator",
        }
        self.assertIsNone(openai_request_invalid_reason(body))

    def test_request_validation_rejects_unusable_bodies(self):
        self.assertEqual(openai_request_invalid_reason(None), "body must be a JSON object")
        self.assertEqual(openai_request_invalid_reason({"messages": []}),
                         "messages must be a non-empty list")
        self.assertEqual(openai_request_invalid_reason({"messages": ["hi"]}),
                         "each message must be an object")
        self.assertEqual(openai_request_invalid_reason({"messages": [{"content": "hi"}]}),
                         "each message needs a string role")
        self.assertEqual(openai_request_invalid_reason(
            {"messages": [{"role": "assistant", "content": "hi"}]}),
            "at least one non-empty user message is required")
        self.assertEqual(openai_request_invalid_reason(
            {"messages": [{"role": "user", "content": "   "}]}),
            "at least one non-empty user message is required")

    def test_annotations_disabled_carries_the_reason(self):
        annotations, reason = citation_annotations(
            [{"label": "a.pdf", "company": None}], base_url="raggle-doc://", limit=12, enabled=False)
        self.assertEqual((annotations, reason), ([], CITATIONS_OFF))

    def test_annotations_need_a_base_url(self):
        for base in ("", "   "):
            annotations, reason = citation_annotations(
                [{"label": "a.pdf", "company": None}], base_url=base, limit=12, enabled=True)
            self.assertEqual((annotations, reason), ([], BASE_URL_UNSET))

    def test_annotations_without_usable_references(self):
        for entries in ([], [{"label": "", "company": None}], [{"label": "  ", "company": "X"}]):
            annotations, reason = citation_annotations(
                entries, base_url="raggle-doc://", limit=12, enabled=True)
            self.assertEqual((annotations, reason), ([], NO_REFERENCES))

    def test_annotation_shape_is_the_one_openwebui_reads(self):
        # middleware.py at v0.12.0 requires type == "url_citation", a nested
        # url_citation object, and a non-empty url, or it drops the annotation.
        annotations, reason = citation_annotations(
            [{"label": "q3 report.pdf", "company": "Acme"}],
            base_url="raggle-doc://raggle/", limit=12, enabled=True)
        self.assertIsNone(reason)
        self.assertEqual(len(annotations), 1)
        annotation = annotations[0]
        self.assertEqual(annotation["type"], "url_citation")
        self.assertEqual(annotation["url_citation"]["title"], "q3 report.pdf — Acme")
        self.assertTrue(annotation["url_citation"]["url"].startswith("raggle-doc://raggle/"))
        self.assertIn("q3%20report.pdf", annotation["url_citation"]["url"])

    def test_annotation_urls_are_distinct_per_document(self):
        # Citations.svelte keys each chip on metadata.source (our url); equal urls
        # collapse into one chip, so two documents must never share one.
        annotations, _ = citation_annotations(
            [{"label": "a.pdf", "company": None}, {"label": "b.pdf", "company": "Acme"},
             {"label": "a.pdf", "company": "Other"}],
            base_url="raggle-doc://d", limit=0, enabled=True)
        urls = [a["url_citation"]["url"] for a in annotations]
        self.assertEqual(len(urls), len(set(urls)))
        self.assertEqual([a["url_citation"]["title"] for a in annotations], ["a.pdf", "b.pdf — Acme"])

    def test_annotation_limit_and_dedupe(self):
        entries = [{"label": f"doc{i}.pdf", "company": None} for i in range(30)]
        capped, _ = citation_annotations(entries, base_url="raggle-doc://d", limit=12, enabled=True)
        self.assertEqual(len(capped), 12)
        uncapped, _ = citation_annotations(entries, base_url="raggle-doc://d", limit=0, enabled=True)
        self.assertEqual(len(uncapped), 30)

    def test_sse_event_is_one_parsable_chunk(self):
        event = sse_event(chat_completion_chunk("m", completion_id="c", created=1, content="x"))
        self.assertTrue(event.startswith("data: "))
        self.assertTrue(event.endswith("\n\n"))
        self.assertEqual(event.count("data:"), 1)

    def test_chunk_shape_first_and_last(self):
        first = chat_completion_chunk("m", completion_id="c", created=1, role="assistant", content="x")
        last = chat_completion_chunk("m", completion_id="c", created=1,
                                     annotations=[{"type": "url_citation"}], finish_reason="stop")
        self.assertEqual(first["choices"][0]["delta"], {"role": "assistant", "content": "x"})
        self.assertIsNone(first["choices"][0]["finish_reason"])
        self.assertEqual(last["choices"][0]["delta"], {"annotations": [{"type": "url_citation"}]})
        self.assertEqual(last["choices"][0]["finish_reason"], "stop")
        for chunk in (first, last):
            self.assertEqual(chunk["object"], "chat.completion.chunk")
            self.assertEqual(chunk["id"], "c")
            self.assertEqual(chunk["model"], "m")


class TestCitationEntries(unittest.TestCase):
    """The door reuses the Sources block's own path and company rules."""

    def test_entries_follow_the_block_and_dedupe_by_label(self):
        citations = [
            {"file_source": "docs/ecommit.pdf"},
            {"file_path": "docs/ecommit.pdf"},                       # duplicate label
            {"source": "reports/Q3 2024.pdf", "company": "Acme"},
            {"path": "x/budget.pdf", "company": "   "},              # blank company is not a company
            {"reference_id": "9"},                                   # no path at all
            "not-a-dict",
        ]
        self.assertEqual(api.citation_entries(citations), [
            {"label": "ecommit.pdf", "company": None},
            {"label": "Q3 2024.pdf", "company": "Acme"},
            {"label": "budget.pdf", "company": None},
        ])

    def test_entries_empty_means_no_annotations_reason(self):
        _, reason = api.build_door_citations([])
        self.assertEqual(reason, CITATIONS_OFF)


class TestDoorStreaming(DoorSettingsMixin):
    def test_stream_with_citations_carries_annotations_and_drops_the_block(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        proxy = AsyncMock(return_value=_upstream(
            "eCommit is a manual trigger.", _refs(["ecommit.pdf", "handbook.pdf"])))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?"), stream=True))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = _door_json(response.text)
        self.assertEqual(events[-1], "[DONE]")
        content_chunk, final_chunk = events[0], events[1]
        self.assertEqual(content_chunk["choices"][0]["delta"]["content"],
                         "eCommit is a manual trigger.")
        self.assertNotIn("Sources", response.text)
        annotations = final_chunk["choices"][0]["delta"]["annotations"]
        self.assertEqual([a["url_citation"]["title"] for a in annotations],
                         ["ecommit.pdf", "handbook.pdf"])
        self.assertEqual(final_chunk["choices"][0]["finish_reason"], "stop")
        self.assertEqual(content_chunk["id"], final_chunk["id"])

        lines = _log_lines(out.getvalue())
        bridge = [line for line in lines if line["endpoint"] == "bridge"]
        door = [line for line in lines if line["endpoint"] == "openai_bridge"]
        self.assertEqual(len(bridge), 1)
        self.assertEqual(len(door), 1)
        self.assertEqual(bridge[0]["request_id"], door[0]["request_id"])
        self.assertEqual(door[0]["citations_emitted"], 2)
        self.assertIsNone(door[0]["citations_skipped_reason"])
        self.assertFalse(door[0]["sources_block_included"])
        self.assertTrue(door[0]["stream"])
        self.assertEqual(door[0]["reference_count"], 2)

    def test_stream_without_citations_keeps_the_text_block(self):
        self.settings.bridge_citations_enabled = False
        proxy = AsyncMock(return_value=_upstream(
            "eCommit is a manual trigger.", _refs(["ecommit.pdf"])))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?")))
        events = _door_json(response.text)
        content = events[0]["choices"][0]["delta"]["content"]
        self.assertIn("**Sources**", content)
        self.assertIn("1. ecommit.pdf", content)
        self.assertNotIn("annotations", events[1]["choices"][0]["delta"])
        door = [line for line in _log_lines(out.getvalue()) if line["endpoint"] == "openai_bridge"][0]
        self.assertEqual(door["citations_skipped_reason"], CITATIONS_OFF)
        self.assertEqual(door["citations_emitted"], 0)
        self.assertTrue(door["sources_block_included"])

    def test_stream_with_citations_on_but_no_base_url_falls_back_to_the_block(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = ""
        proxy = AsyncMock(return_value=_upstream("answer text", _refs(["ecommit.pdf"])))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?")))
        events = _door_json(response.text)
        self.assertIn("**Sources**", events[0]["choices"][0]["delta"]["content"])
        door = [line for line in _log_lines(out.getvalue()) if line["endpoint"] == "openai_bridge"][0]
        self.assertEqual(door["citations_skipped_reason"], BASE_URL_UNSET)
        self.assertTrue(door["sources_block_included"])

    def test_stream_with_no_references_has_no_annotations_and_no_block(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        proxy = AsyncMock(return_value=_upstream("Nothing found.", []))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = proxy
            response = self._post_door(self._request(("user", "Unasked thing?")))
        events = _door_json(response.text)
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "Nothing found.")
        self.assertNotIn("annotations", events[1]["choices"][0]["delta"])
        self.assertNotIn("Sources", response.text)
        door = [line for line in _log_lines(out.getvalue()) if line["endpoint"] == "openai_bridge"][0]
        self.assertEqual(door["citations_skipped_reason"], NO_REFERENCES)
        self.assertEqual(door["reference_count"], 0)

    def test_many_references_are_capped_at_the_limit(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        self.settings.bridge_citation_limit = 12
        names = [f"doc{i}.pdf" for i in range(30)]
        proxy = AsyncMock(return_value=_upstream("answer", _refs(names)))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            response = self._post_door(self._request(("user", "Wide question?")))
        annotations = _door_json(response.text)[1]["choices"][0]["delta"]["annotations"]
        self.assertEqual(len(annotations), 12)
        self.assertEqual(len({a["url_citation"]["url"] for a in annotations}), 12)

    def test_upstream_exception_still_returns_a_valid_completion(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        proxy = AsyncMock(side_effect=RuntimeError("upstream down"))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?")))
        self.assertEqual(response.status_code, 200)
        events = _door_json(response.text)
        self.assertEqual(events[0]["choices"][0]["delta"]["content"], "")
        self.assertEqual(events[-1], "[DONE]")

    def test_answer_is_byte_identical_to_the_ollama_door(self):
        """The door re-packages; it does not re-answer."""
        self.settings.bridge_citations_enabled = False
        messages = self._messages(
            ("user", "What is eCommit?"),
            ("assistant", "A trigger.\n\n---\n\n**Sources**\n\n1. ecommit.pdf"),
            ("user", "and who owns that step?"),
        )
        upstream = lambda: _upstream("Renée owns it.", _refs(["ecommit.pdf", "org chart.pdf"]))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = AsyncMock(return_value=upstream())
            ollama_answer = _run_async(api.answer_ollama_bridge_prompt(
                {"model": api.OLLAMA_BRIDGE_MODEL, "messages": messages, "stream": True}))
        with patch.object(api, "client") as client:
            client.proxy = AsyncMock(return_value=upstream())
            response = self._post_door({"model": OPENAI_BRIDGE_MODEL, "messages": messages,
                                        "stream": True})
        content = _door_json(response.text)[0]["choices"][0]["delta"]["content"]
        self.assertEqual(content, ollama_answer)

    def test_sources_blocks_are_stripped_from_carried_history(self):
        """Today's history strip survives the new door, including when the prior
        turn came from the citations door and has no block."""
        self.settings.bridge_history_turns = 3
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        proxy = AsyncMock(return_value=_upstream("answer", _refs(["ecommit.pdf"])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            self._post_door({"model": OPENAI_BRIDGE_MODEL, "stream": True, "messages": [
                {"role": "user", "content": "What is eCommit?"},
                {"role": "assistant",
                 "content": "A trigger.\n\n---\n\n**Sources**\n\n1. ecommit.pdf"},
                {"role": "user", "content": "and who owns that step?"},
            ]})
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(body["conversation_history"], [
            {"role": "user", "content": "What is eCommit?"},
            {"role": "assistant", "content": "A trigger."},
        ])
        self.assertEqual(body["query"], "and who owns that step?")

    def test_task_prompt_short_circuits_through_the_door(self):
        proxy = AsyncMock(return_value=_upstream("Chat title", []))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            response = self._post_door(self._request(
                ("user", "### Task:\nSummarise the chat in five words.")))
        body = json.loads(proxy.await_args.kwargs["body"])
        self.assertEqual(body["mode"], "bypass")
        self.assertEqual(proxy.await_count, 1)
        self.assertEqual(_door_json(response.text)[0]["choices"][0]["delta"]["content"], "Chat title")


class TestDoorNonStreaming(DoorSettingsMixin):
    def test_non_stream_keeps_the_block_so_sources_are_never_lost(self):
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = "raggle-doc://raggle"
        proxy = AsyncMock(return_value=_upstream("answer body", _refs(["ecommit.pdf"])))
        out = StringIO()
        with patch.object(api, "client") as client, redirect_stdout(out):
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?"), stream=False))
        self.assertEqual(response.headers["content-type"], "application/json")
        body = response.json()
        self.assertEqual(body["object"], "chat.completion")
        self.assertIn("**Sources**", body["choices"][0]["message"]["content"])
        # Attached because that is where the OpenAI schema puts them; Open WebUI
        # 0.12.0 ignores them on this path, which is why the block stays.
        self.assertEqual(len(body["choices"][0]["message"]["annotations"]), 1)
        door = [line for line in _log_lines(out.getvalue()) if line["endpoint"] == "openai_bridge"][0]
        self.assertFalse(door["stream"])
        self.assertTrue(door["sources_block_included"])
        self.assertEqual(door["citations_emitted"], 1)

    def test_non_stream_citations_off_is_plain_answer_text(self):
        self.settings.bridge_citations_enabled = False
        proxy = AsyncMock(return_value=_upstream("answer body", _refs(["ecommit.pdf"])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            response = self._post_door(self._request(("user", "What is eCommit?"), stream=False))
        body = response.json()
        self.assertIn("1. ecommit.pdf", body["choices"][0]["message"]["content"])
        self.assertNotIn("annotations", body["choices"][0]["message"])


def _owui_stream_events(body: str) -> tuple[str, list[dict]]:
    """Transcription of Open WebUI 0.12.0's reading of a chat-completions stream.

    backend/open_webui/utils/middleware.py at tag v0.12.0:
      ~5092 async for line in response.body_iterator   (each chunk parsed whole)
      ~5119 if not data.startswith('data:'): continue
      ~5126 data = data[5:].strip(); json.loads(...)
      ~5301 annotations = choices[0]['delta'].get('annotations'); each entry must
            be type url_citation with a nested url_citation object, and each one
            becomes a type "source" event built exactly as below.
    Only the pieces this door's response touches are reproduced; error handling,
    filters and the Responses-API branch are left out.
    """
    content = ""
    sources: list[dict] = []
    for line in body.split("\n\n"):
        if not line or line.isspace():
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:"):].strip()
        if payload == "[DONE]":
            continue
        data = json.loads(payload)
        choices = data.get("choices") or [{}]
        delta = choices[0].get("delta", {})
        if isinstance(delta.get("content"), str):
            content += delta["content"]
        for annotation in delta.get("annotations") or []:
            if annotation.get("type") == "url_citation" and "url_citation" in annotation:
                url_citation = annotation["url_citation"]
                url = url_citation.get("url", "")
                title = url_citation.get("title", url)
                sources.append({
                    "source": {"name": title, "url": url},
                    "document": [title],
                    "metadata": [{"source": url, "name": title}],
                })
    return content, sources


def _owui_chip_labels(sources: list[dict]) -> list[str]:
    """Transcription of Messages/Citations.svelte at the same tag (~108-147).

    Chips are keyed on metadata.source; when that id starts with http the
    displayed name is replaced by the id itself. This is what the operator
    actually reads under "N Sources".
    """
    chips: list[dict] = []
    for source in sources:
        for index, _document in enumerate(source.get("document") or []):
            metadata = (source.get("metadata") or [None] * 99)[index]
            cid = metadata["source"] if metadata and metadata.get("source") is not None \
                else source.get("source", {}).get("id", "N/A")
            name = source.get("source", {}).get("name")
            if metadata and metadata.get("name"):
                name = metadata["name"]
            if str(cid).startswith("http://") or str(cid).startswith("https://"):
                name = cid
            existing = next((item for item in chips if item["id"] == cid), None)
            if existing:
                existing["documents"] += 1
            else:
                chips.append({"id": cid, "name": name, "documents": 1})
    return [chip["name"] for chip in chips]


class TestOpenWebUi012Replay(DoorSettingsMixin):
    """What the operator sees, checked offline against the 0.12.0 reading rules.

    These tests do not run Open WebUI; they replay the two code paths verified
    from the tag's source. Hermes still owes the real-question check on stack2.
    """

    def _door_body(self, base_url: str) -> str:
        self.settings.bridge_citations_enabled = True
        self.settings.bridge_citation_base_url = base_url
        proxy = AsyncMock(return_value=_upstream(
            "eCommit is a manual trigger.", _refs(["ecommit.pdf", "Q3 report.pdf"])))
        with patch.object(api, "client") as client:
            client.proxy = proxy
            return self._post_door(self._request(("user", "What is eCommit?"))).text

    def test_one_chip_per_document_with_a_non_http_base(self):
        body = self._door_body("raggle-doc://raggle")
        content, sources = _owui_stream_events(body)
        self.assertEqual(content, "eCommit is a manual trigger.")
        self.assertEqual(len(sources), 2)
        self.assertEqual(_owui_chip_labels(sources), ["ecommit.pdf", "Q3 report.pdf"])

    def test_an_http_base_makes_the_chip_label_the_url(self):
        """Pinned so the setting note can say so instead of discovering it later."""
        body = self._door_body("http://192.0.2.10:9621/webui/")
        _content, sources = _owui_stream_events(body)
        labels = _owui_chip_labels(sources)
        # The chip reads as the whole URL, with the document name only at the end
        # of the path - which is what the setting note has to say a base of this
        # shape costs the operator.
        self.assertEqual(labels, [
            "http://192.0.2.10:9621/webui/ecommit.pdf",
            "http://192.0.2.10:9621/webui/Q3%20report.pdf",
        ])


class TestDoorAuth(unittest.TestCase):
    def test_both_v1_routes_require_the_gateway_key(self):
        async def call(path: str, headers: dict) -> int:
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                response = await client.request("GET" if path == "/v1/models" else "POST",
                                                path, headers=headers,
                                                json={"model": "m", "messages": [
                                                    {"role": "user", "content": "x"}]})
                return response.status_code

        for path in ("/v1/models", "/v1/chat/completions"):
            for headers in ({}, {"Authorization": "Bearer wrong"}):
                self.assertEqual(_run_async(call(path, headers)), 401, f"{path} {headers}")

    def test_the_ollama_door_is_unchanged_by_the_new_one(self):
        """A model the bridge does not own must still be proxied upstream."""
        async def call() -> int:
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway") as client:
                response = await client.post("/api/chat", json={
                    "model": "some-other-model", "messages": [{"role": "user", "content": "x"}]})
                return response.status_code

        resp = _Resp(json.dumps({"message": {"content": "hi"}, "done": True}).encode())
        with patch.object(api, "client") as client:
            client.proxy = AsyncMock(return_value=resp)
            self.assertEqual(_run_async(call()), 200)
