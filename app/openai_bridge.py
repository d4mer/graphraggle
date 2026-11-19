"""OpenAI-compatible door on the gateway, so Open WebUI can show real citations.

Why a second door exists (verified in the Open WebUI source at tag v0.12.0,
not inferred): every Ollama-native response is rebuilt by
``convert_response_ollama_to_openai`` / ``convert_streaming_response_ollama_to_openai``
in ``backend/open_webui/utils/response.py``, which read only ``model``,
``message.content`` / ``message.thinking`` / ``message.tool_calls``, ``done``,
``done_reason`` and the usage fields into a freshly built OpenAI template. Any
extra top-level key on an Ollama response (``sources``, ``citations``,
``metadata``) is dropped, so the Ollama door structurally cannot carry
citations. On the OpenAI-compatible chat-completions path the same version
reads ``choices[0].delta.annotations[].url_citation``
(``backend/open_webui/utils/middleware.py`` ~5302) and turns each one into a
``type: source`` event; ``src/lib/components/chat/Chat.svelte`` ~1626 appends it
to ``message.sources`` and ``Messages/Citations.svelte`` renders the "N Sources"
chip with one row per document.

Two facts about that renderer decide the payload shape here, both read from the
same tag:

* ``Citations.svelte`` keys each chip on ``metadata.source`` (the url we send)
  and, when that id starts with ``http``, it OVERWRITES the displayed name with
  the id itself. So each document needs a DISTINCT url - equal urls collapse into
  one chip, and an empty url collapses every document into a single chip whose id
  is "" - and an http base makes the chip read as a URL rather than as the
  document name.
* The chat-completions delta branch does NOT guard on a non-empty url (the
  Responses-API branch at ~5182 does). So an empty url is not dropped; it just
  produces the collapsed single chip above. That is why the door needs a base
  url before it can emit anything useful, and falls back to the text block.
* The chip rows are buttons that open a modal, not links; the modal renders a
  link only when ``isValidHttpUrl(source.url)``. A non-http url therefore loses
  nothing except the link.

Annotations only appear on a STREAMING completion (the only other annotation
handler in that tag is the Responses-API ``output_item.done`` branch), which is
why the door streams even though the answer arrives as one chunk.

Nothing in this module retrieves or generates: the door reuses the existing
bridge answer byte for byte and only re-packages it.
"""

from __future__ import annotations

import json
import time
import uuid
from urllib.parse import quote
from typing import Any, Iterable, Optional, Sequence

# Model id advertised on this door. Deliberately NOT the Ollama door's
# ``lightrag:latest``: the operator picks one door or the other in the model
# picker, and two connections exposing the same id is how a silently-changed
# path gets missed.
OPENAI_BRIDGE_MODEL = "raggle:latest"

# Why no annotations were emitted, logged as an enum string only.
CITATIONS_OFF = "citations_off"
BASE_URL_UNSET = "base_url_unset"
NO_REFERENCES = "no_references"


def openai_models_payload() -> dict[str, Any]:
    """GET /v1/models body: one model, this door's. No corpus content."""
    return {
        "object": "list",
        "data": [
            {
                "id": OPENAI_BRIDGE_MODEL,
                "object": "model",
                "created": 0,
                "owned_by": "raggle-gateway",
            }
        ],
    }


def openai_request_invalid_reason(payload: Any) -> Optional[str]:
    """None when the body is usable as a chat-completions request, else a reason.

    Kept narrow on purpose: unknown parameters (temperature, max_tokens,
    stream_options, user, ...) are ignored rather than rejected, because the
    answer is produced by the existing bridge, which never sees them. Rejecting
    them would turn an Open WebUI upgrade into an outage.
    """
    if not isinstance(payload, dict):
        return "body must be a JSON object"
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        return "messages must be a non-empty list"
    for message in messages:
        if not isinstance(message, dict):
            return "each message must be an object"
        if not isinstance(message.get("role"), str):
            return "each message needs a string role"
    if not any(
        isinstance(m.get("role"), str) and m["role"] == "user" and isinstance(m.get("content"), str)
        and m["content"].strip()
        for m in messages
    ):
        return "at least one non-empty user message is required"
    return None


def citation_annotations(
    entries: Iterable[dict[str, Any]],
    *,
    base_url: str,
    limit: int,
    enabled: bool,
) -> tuple[list[dict[str, Any]], Optional[str]]:
    """Build ``url_citation`` annotations from prepared (label, company) entries.

    ``entries`` are already extracted by the caller, which owns the path/company
    rules (``extract_citation_path``, ``build_sources_block``); this module does
    not re-derive them and so cannot drift from the Sources block.

    Returns ``(annotations, skipped_reason)``. Titles are
    ``"<basename> — <company>"`` where a company is known, matching what the
    text block shows, because the title is the thing the operator reads.
    """
    if not enabled:
        return [], CITATIONS_OFF
    if not (isinstance(base_url, str) and base_url.strip()):
        return [], BASE_URL_UNSET
    prepared: Sequence[dict[str, Any]] = [e for e in entries if isinstance(e, dict)]
    if not [e for e in prepared if str(e.get("label") or "").strip()]:
        return [], NO_REFERENCES

    base = base_url.strip().rstrip("/")
    annotations: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        title = str(entry.get("label") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        company = entry.get("company")
        if isinstance(company, str) and company.strip():
            title = f"{title} — {company.strip()}"
        annotations.append({
            "type": "url_citation",
            "url_citation": {"url": f"{base}/{quote(title, safe='')}", "title": title},
        })
        if limit > 0 and len(annotations) >= limit:
            break
    if not annotations:
        return [], NO_REFERENCES
    return annotations, None


def chat_completion_chunk(
    model: str,
    *,
    completion_id: str,
    created: int,
    content: str | None = None,
    annotations: list[dict[str, Any]] | None = None,
    finish_reason: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    """One chat-completion chunk.

    The first chunk carries ``delta.role``; the last carries ``finish_reason``
    and, when present, the annotations inside the delta - the exact place
    Open WebUI 0.12.0 reads them from.
    """
    delta: dict[str, Any] = {}
    if role is not None:
        delta["role"] = role
    if content is not None:
        delta["content"] = content
    if annotations:
        delta["annotations"] = annotations
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "logprobs": None, "finish_reason": finish_reason, "delta": delta}
        ],
    }


def chat_completion_body(
    model: str,
    *,
    completion_id: str,
    created: int,
    content: str,
    annotations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Non-streaming chat-completion body.

    A non-streaming response carries no citations in Open WebUI 0.12.0 (no
    annotation handler on that path), so the caller keeps the text Sources
    block here rather than swallowing it.
    """
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if annotations:
        message["annotations"] = annotations
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {"index": 0, "logprobs": None, "finish_reason": "stop", "message": message}
        ],
    }


def sse_event(payload: dict[str, Any]) -> str:
    """One SSE event.

    One event per yielded chunk, not several in one string: Open WebUI's
    streaming loop iterates the response body iterator and JSON-parses each
    item after stripping the ``data:`` prefix, so two events in one chunk fail
    to parse.
    """
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def new_completion_id() -> str:
    return "chatcmpl-" + str(uuid.uuid4())
