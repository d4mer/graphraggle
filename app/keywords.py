"""Gateway-side keyword extraction for the Ollama bridge (issue-08 keyword fix).

LightRAG v1.5.7 extracts query keywords itself with a bare
``response_format={"type": "json_object"}`` call (operate.py ~5048). The local
oMLX server accepts that field but does not enforce it, and about 15% of draws
return an array wrapping the object, a tool-call-shaped array, or a degenerate
payload. LightRAG then logs "Keyword extraction payload is not a JSON object:
list", gets empty keyword lists, and answers with the canned no-context text
(queries >= 50 chars) or silently degrades (shorter queries).

This module lets the bridge produce and validate the keywords itself and pass
them to /query/stream as hl_keywords / ll_keywords, which makes LightRAG skip
its own extraction (operate.py ~4865-4866). Parsing is pure and I/O-free; the
async client function never raises: the caller always gets a KeywordResult.

The prompt template below is copied verbatim from public LightRAG v1.5.7
source, lightrag/prompt.py lines 484-516 (template) and 517-521 (examples).
It contains only the query, fixed examples and a language token - no corpus
text. The keyword call goes only to the local oMLX server.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import httpx

from . import config as _config


def _settings():
    # Resolve at call time: test files reload app.config, which replaces the
    # module-level settings object; a bind-at-import reference goes stale.
    return _config.settings

# ── Prompt (verbatim from LightRAG v1.5.7 lightrag/prompt.py:484) ─────────

KEYWORDS_EXTRACTION_TEMPLATE = """---Role---
You are an expert keyword extractor, specializing in analyzing user queries for a Retrieval-Augmented Generation (RAG) system. Your purpose is to identify both high-level and low-level keywords in the user's query that will be used for effective document retrieval.

---Goal---
Given a user query, your task is to extract two distinct types of keywords:
1. **high_level_keywords**: for overarching concepts or themes, capturing user's core intent, the subject area, or the type of question being asked.
2. **low_level_keywords**: for specific entities or details, identifying the specific entities, proper nouns, technical jargon, product names, or concrete items.

---Instructions & Constraints---
1. **Output Format**: Your output MUST be a valid JSON object and nothing else. Do not include any explanatory text, markdown code fences (like ```json), comments, or any other text before or after the JSON.
2. **Exact JSON Shape**: The JSON object must contain exactly these two keys:
   - `"high_level_keywords"`: an array of strings
   - `"low_level_keywords"`: an array of strings
3. **JSON Boundary**: The first character of your response must be `{{` and the last character must be `}}`.
4. **Source of Truth**: All keywords must be explicitly derived only from the `User Query` in the `---Real Data---` section. Do not infer unsupported facts. Do not invent entities, products, organizations, dates, or technical terms that are not grounded in the query.
5. **Concise & Meaningful**: Keywords should be concise words or meaningful phrases. Prioritize multi-word phrases when they represent a single concept instead of splitting meaningful phrases into isolated words.
6. **Handle Edge Cases**: For queries that are too simple, vague, or nonsensical (e.g., "hello", "ok", "asdfghjkl"), return:
   `{{"high_level_keywords": [], "low_level_keywords": []}}`
7. **No Duplicates**: Do not repeat the same keyword within a list. Keep the lists short and high-signal.
8. **Language**: All extracted keywords MUST be in {language}. Proper nouns (e.g., personal names, place names, organization names) should be kept in their original language.
9. **Output Format Template Safety**: The `---Output Format Template---` section contains an output JSON template only. It is never source text. Do not extract, infer, or copy keywords from the template. Angle-bracket tokens such as `<high_level_keyword>` are placeholders; replace them only with keywords derived from the current `User Query` and never output the placeholders literally.

---Output Format Template---
The following content is an output JSON format template only. It is not source text and must never be used as keyword extraction content.

{examples}

---Real Data---
User Query: {query}

---Output---
Output:"""

# Verbatim from LightRAG v1.5.7 lightrag/prompt.py:517.
KEYWORDS_EXTRACTION_EXAMPLES = [
    '{\n  "high_level_keywords": ["<high_level_keyword>"],\n  "low_level_keywords": ["<low_level_keyword>"]\n}\n'
]

# LightRAG constants.DEFAULT_SUMMARY_LANGUAGE
DEFAULT_SUMMARY_LANGUAGE = "English"

KEYWORD_KEYS = ("high_level_keywords", "low_level_keywords")

# Rejection reasons are enum strings only - never payload or query text.
REASON_EMPTY_TEXT = "empty_text"
REASON_NO_JSON = "no_json_found"
REASON_TOOL_CALL = "tool_call_shape"
REASON_ARRAY_MULTI = "array_multi_element"
REASON_ARRAY_NON_DICT = "array_non_dict_element"
REASON_MISSING_KEYS = "missing_keys"
REASON_WRONG_TYPES = "wrong_types"
REASON_THOUGHT_DUMP = "thought_dump"
REASON_BOTH_EMPTY = "both_lists_empty"
REASON_NOT_OBJECT = "not_object_or_array"
REASON_TIMEOUT = "timeout"
REASON_HTTP_ERROR = "http_error"
REASON_UNEXPECTED = "unexpected_error"

# thought-dump guard (issue-08 defaults)
MAX_KEYWORD_CHARS = 80
MAX_KEYWORD_WORDS = 8

_TOOL_CALL_KEYS = {"name", "arguments", "function"}


@dataclass
class KeywordResult:
    hl: list
    ll: list
    source: str  # llm | llm_unwrapped | fallback | rejected | error
    attempts: int = 0
    failure_reasons: list = field(default_factory=list)


def build_keyword_prompt(query: str, language: Optional[str] = None) -> str:
    """Same prompt LightRAG v1.5.7 would build for this query."""
    examples = "\n".join(KEYWORDS_EXTRACTION_EXAMPLES)
    return KEYWORDS_EXTRACTION_TEMPLATE.format(
        query=query,
        examples=examples,
        language=language or DEFAULT_SUMMARY_LANGUAGE,
    )


def _strip_reasoning(text: str) -> str:
    """Reuse the bridge's complete-pair reasoning strip (api.py).

    Deferred import avoids an import cycle (api imports this module).
    """
    try:
        from .api import strip_reasoning_regions
        return strip_reasoning_regions(text)
    except Exception:
        return text


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _extract_json_candidate(text: str):
    """Return the parsed JSON value, or a sentinel string on failure."""
    # whole text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    # fenced block
    for match in _FENCE_RE.finditer(text):
        try:
            return json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
    # first balanced {...} or [...] (string-aware depth counting)
    start = -1
    opener = closer = ""
    for i, ch in enumerate(text):
        if ch in "{[":
            start, opener, closer = i, ch, "}" if ch == "{" else "]"
            break
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except (json.JSONDecodeError, ValueError):
                    return None
    return None


def _normalize_list(items, max_items: int):
    """Strip, drop empty/non-string, de-dup case-insensitively, cap.

    Returns (list, reject_reason_or_None). The thought-dump guard rejects the
    whole payload when any item is a long prose string.
    """
    out = []
    seen = set()
    for item in items:
        if not isinstance(item, str):
            continue
        item = item.strip()
        if not item:
            continue
        if len(item) > MAX_KEYWORD_CHARS or len(item.split()) > MAX_KEYWORD_WORDS:
            return [], REASON_THOUGHT_DUMP
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_items:
            break
    return out, None


def parse_keyword_payload(text, max_items: Optional[int] = None) -> KeywordResult:
    """Parse and validate one keyword response. Never raises.

    Accepts: a dict with the two expected keys; a single-element list wrapping
    such a dict (source ``llm_unwrapped``). Rejects everything else with typed
    enum reasons (no payload text).
    """
    if max_items is None:
        max_items = _settings().bridge_keyword_max_items
    if not isinstance(text, str) or not text.strip():
        return KeywordResult([], [], "rejected", 0, [REASON_EMPTY_TEXT])

    cleaned = _strip_reasoning(text)
    payload = _extract_json_candidate(cleaned)
    if payload is None:
        return KeywordResult([], [], "rejected", 0, [REASON_NO_JSON])

    source = "llm"
    if isinstance(payload, list):
        if not payload:
            return KeywordResult([], [], "rejected", 0, [REASON_ARRAY_NON_DICT])
        for element in payload:
            if isinstance(element, dict) and _TOOL_CALL_KEYS & set(element.keys()):
                return KeywordResult([], [], "rejected", 0, [REASON_TOOL_CALL])
        if not all(isinstance(element, dict) for element in payload):
            # [1], [1.0, 2.0, 3.0], ["x"], ... - degenerate numeric/string wrap
            return KeywordResult([], [], "rejected", 0, [REASON_ARRAY_NON_DICT])
        if len(payload) > 1:
            return KeywordResult([], [], "rejected", 0, [REASON_ARRAY_MULTI])
        payload = payload[0]
        source = "llm_unwrapped"
    if not isinstance(payload, dict):
        return KeywordResult([], [], "rejected", 0, [REASON_NOT_OBJECT])

    missing = [k for k in KEYWORD_KEYS if k not in payload]
    if missing:
        return KeywordResult([], [], "rejected", 0, [REASON_MISSING_KEYS])
    if not isinstance(payload["high_level_keywords"], list) or not isinstance(
        payload["low_level_keywords"], list
    ):
        return KeywordResult([], [], "rejected", 0, [REASON_WRONG_TYPES])

    hl, reject = _normalize_list(payload["high_level_keywords"], max_items)
    if reject:
        return KeywordResult([], [], "rejected", 0, [reject])
    ll, reject = _normalize_list(payload["low_level_keywords"], max_items)
    if reject:
        return KeywordResult([], [], "rejected", 0, [reject])
    if not hl and not ll:
        return KeywordResult([], [], "rejected", 0, [REASON_BOTH_EMPTY])
    return KeywordResult(hl, ll, source, 1, [])


# ── Fallback: derive terms from the query itself ───────────────────────────

_STOPWORDS = {
    "a", "about", "an", "and", "are", "as", "at", "be", "been", "but", "by",
    "can", "could", "did", "do", "does", "for", "from", "had", "has", "have",
    "how", "i", "if", "in", "into", "is", "it", "its", "me", "my", "no", "not",
    "of", "on", "or", "our", "please", "say", "said", "she", "should", "so",
    "summarize", "tell", "than", "that", "the", "their", "them", "then",
    "there", "these", "they", "this", "those", "to", "too", "up", "us", "was",
    "we", "were", "what", "when", "where", "which", "who", "why", "will",
    "with", "would", "you", "your",
}

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'\-]*")
_QUOTED_RE = re.compile(r"[\"']([^\"']{2,})[\"']")


def fallback_keywords(query: str, max_items: Optional[int] = None):
    """Deterministic query-term fallback. Marked source='fallback' by caller.

    Keeps original-case proper nouns, numbers, hyphenated tokens and quoted
    phrases intact; lower-cased content words become high-level terms; the
    whole query joins the low-level list when it is short (<= 8 words).
    """
    if max_items is None:
        max_items = _settings().bridge_keyword_max_items
    query = (query or "").strip()
    hl: list = []
    ll: list = []

    for phrase in _QUOTED_RE.findall(query):
        ll.append(phrase.strip())

    words = query.split()
    if 0 < len(words) <= 8:
        ll.append(query)

    tokens = _TOKEN_RE.findall(query)
    # proper-noun runs: capitalised tokens (not sentence-initial) grouped
    run: list = []
    for idx, tok in enumerate(tokens):
        is_number = bool(re.match(r"^\d", tok))
        is_proper = (
            not is_number
            and re.match(r"^[A-Z][A-Za-z0-9'\-]+$", tok)
            and tok.lower() not in _STOPWORDS
            and idx > 0
        )
        if is_proper:
            run.append(tok)
            continue
        if run:
            ll.append(" ".join(run))
            run = []
        if is_number:
            ll.append(tok)
        elif tok.lower() not in _STOPWORDS and len(tok) > 1:
            hl.append(tok.lower())
    if run:
        ll.append(" ".join(run))

    def dedupe(items):
        out, seen = [], set()
        for item in items:
            key = item.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item)
        return out[:max_items]

    return dedupe(hl), dedupe(ll)


# ── Async client (never raises) ────────────────────────────────────────────

def _resolve_endpoint():
    base = (_settings().bridge_keyword_llm_base_url or _settings().llm_binding_host or "").rstrip("/")
    model = _settings().bridge_keyword_llm_model or _settings().llm_model or ""
    key = _settings().bridge_keyword_llm_api_key or _settings().llm_binding_api_key or ""
    return base, model, key


async def _http_post_chat(body: dict, timeout: int) -> dict:
    base, _model, key = _resolve_endpoint()
    headers = {"Content-Type": "application/json"}
    if key:
        # oMLX accepts x-api-key universally; send Bearer too for parity.
        headers["x-api-key"] = key
        headers["Authorization"] = "Bearer " + key
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base}/chat/completions", headers=headers, json=body)
        resp.raise_for_status()
        return resp.json()


async def extract_keywords(
    query: str,
    *,
    post: Optional[Callable[[dict, int], Awaitable[dict]]] = None,
) -> KeywordResult:
    """Produce validated keywords for ``query``. Sequential retries; never raises.

    ``post`` is injectable for tests; it receives the request body and the
    timeout and returns the parsed chat-completion JSON.
    """
    poster = post or _http_post_chat
    _base, model, _key = _resolve_endpoint()
    max_attempts = 1 + max(0, _settings().bridge_keyword_retries)
    reasons: list = []
    attempts = 0
    for _ in range(max_attempts):
        attempts += 1
        body: dict = {
            "model": model,
            "messages": [{"role": "user", "content": build_keyword_prompt(query)}],
            # harmless parity with LightRAG's call; oMLX accepts but does not
            # enforce it, which is why validation and retry live here.
            "response_format": {"type": "json_object"},
            # modest cap that still leaves room for any reasoning the model
            # emits before the JSON
            "max_tokens": 2000,
        }
        if _settings().bridge_keyword_no_think:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        try:
            data = await poster(body, _settings().bridge_keyword_timeout)
            text = data["choices"][0]["message"]["content"]
        except Exception as exc:  # noqa: BLE001 - never raise by design
            name = type(exc).__name__
            if "Timeout" in name:
                reasons.append(REASON_TIMEOUT)
            else:
                reasons.append(REASON_HTTP_ERROR)
            continue
        result = parse_keyword_payload(text)
        if result.source in ("llm", "llm_unwrapped"):
            result.attempts = attempts
            result.failure_reasons = reasons + result.failure_reasons
            return result
        reasons.extend(result.failure_reasons)

    hl, ll = fallback_keywords(query)
    return KeywordResult(hl, ll, "fallback", attempts, reasons)
