"""One-command online probe for the M1 evidence question. Run it ON THE STACK; it is not runnable
from a dev box (no stack, and it refuses without --gateway-url).

Why this exists: the 2026-11-14 run's log said `grounding_references_without_content` = 10-11 on
every row, so the gate's evidence argument was empty online. Whether LightRAG 1.5.7 never returns
chunk text in `references`, or returns it under a key/shape we do not read, decides which fix is
correct — and the repo holds no recorded raw upstream response (every `references` fixture in
tests/ is one we invented). One response settles it.

What it prints is a SHAPE, not content: key paths, value types, lengths, short hashes, and
membership booleans. It never prints chunk text. The only text it sends upstream is the probe's own
question (invented corpus).

Two things it answers in one run:

  Q1  Do `references` entries carry a content-bearing field at all, and under what key/shape?
      The gateway reads `content` as a plain string (app/grounding.py:88-93) while
      app/generation.py:29 reads `content` as a LIST of strings and app/rerank.py:33-41 accepts
      content/text/chunk_text/body as string or list. If the upstream shape is the list form, the
      gate is blind because of our reader, not because LightRAG withheld anything.

  Q2  Did the asked identifier reach the gateway in the first place? `asked_code_token_hits` counts
      the strings in the whole response in which the code appears as a standalone token (the same
      token rule app/grounding.verbatim_support uses). Zero hits with a non-empty context block
      would mean retrieval genuinely missed the piece; zero references-with-content plus zero hits
      means the text never left LightRAG.

Examples (stack2):

  python scripts/m1_reference_shape_probe.py \
    --gateway-url http://localhost:8000 --api-key-env GATEWAY_API_KEY \
    --company "Northwind Chemicals" --probe-id idn-nc3617-s001

  # also ask LightRAG directly with the gateway's own retrieval flags:
  python scripts/m1_reference_shape_probe.py \
    --gateway-url http://localhost:8000 --api-key-env GATEWAY_API_KEY \
    --raw-lightrag-url http://localhost:9621 --api-key-env LIGHTRAG_API_KEY \
    --company "Northwind Chemicals" --probe-id idn-nc3617-s001 --mode mix
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app import grounding  # noqa: E402  (imports without the app settings; see tests/conftest.py)

DEFAULT_PROBE_ROOT = Path("/home/maverick/work/synthetic-co-probes")

# Keys whose value is the request echoed back. Kept short on purpose: an echo key reported as a
# "hit" would be the question naming its own code, which proves nothing.
ECHO_KEYS = frozenset({"query", "question", "content_type", "model", "mode", "response_type"})


def walk_strings(payload: Any, *, key_path: str = "", skip: frozenset[str] = ECHO_KEYS) -> list[tuple[str, str]]:
    """(key_path, string) for every string in the payload, request-echo keys excluded."""
    found: list[tuple[str, str]] = []
    if isinstance(payload, str):
        if key_path.split(".")[-1] not in skip:
            found.append((key_path, payload))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            child = f"{key_path}.{key}" if key_path else str(key)
            if str(key) in skip and isinstance(value, str):
                continue
            found.extend(walk_strings(value, key_path=child, skip=skip))
    elif isinstance(payload, list):
        for index, value in enumerate(payload):
            found.extend(walk_strings(value, key_path=f"{key_path}[{index}]", skip=skip))
    return found


def shape_of(payload: Any, *, key_path: str = "", cells: dict[str, set[str]] | None = None,
             max_depth: int = 6) -> dict[str, set[str]]:
    """Key paths -> observed value descriptions (type, length, short hash). No content."""
    cells = cells if cells is not None else defaultdict(set)
    if max_depth <= 0:
        cells[key_path or "<root>"].add("max_depth")
        return cells
    if isinstance(payload, str):
        cells[key_path or "<root>"].add(
            f"str(len={len(payload)} sha256={hashlib.sha256(payload.encode('utf-8', 'ignore')).hexdigest()[:12]})")
    elif isinstance(payload, bool):
        cells[key_path or "<root>"].add("bool")
    elif isinstance(payload, (int, float)):
        cells[key_path or "<root>"].add(type(payload).__name__)
    elif payload is None:
        cells[key_path or "<root>"].add("null")
    elif isinstance(payload, list):
        cells[key_path or "<root>"].add(f"list(n={len(payload)})")
        for value in payload:
            shape_of(value, key_path=f"{key_path}[]", cells=cells, max_depth=max_depth - 1)
    elif isinstance(payload, dict):
        cells[key_path or "<root>"].add(f"dict(keys={len(payload)})")
        for key, value in payload.items():
            shape_of(value, key_path=f"{key_path}.{key}" if key_path else str(key),
                     cells=cells, max_depth=max_depth - 1)
    return cells


def describe(payload: Any) -> str:
    cells = shape_of(payload)
    return "\n".join(f"    {path}: {', '.join(sorted(shapes))}"
                     for path, shapes in sorted(cells.items()))


def post(url: str, payload: dict[str, Any], api_key: str | None, timeout: int) -> tuple[int, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/query",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {api_key}"} if api_key else {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as exc:            # a 404 from /query/data is an answer, not a crash
        status, raw = exc.code, exc.read()
    except urllib.error.URLError as exc:
        return -1, {"error": str(exc)}
    try:
        return status, json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError:
        return status, {"non_json": f"len={len(raw)}"}


def reference_report(response: Any) -> dict[str, Any]:
    """Shape + membership facts about `references`, without pasting reference text."""
    data = response.get("data") if isinstance(response, dict) and "data" in response else response
    references = data.get("references") if isinstance(data, dict) else None
    out: dict[str, Any] = {"references_present": isinstance(references, list)}
    if not isinstance(references, list):
        out["top_level_keys"] = sorted(response.keys()) if isinstance(response, dict) else []
        return out
    content_keys = ("content", "text", "chunk_text", "body")
    counts = {key: 0 for key in content_keys}
    list_valued = {key: 0 for key in content_keys}
    non_empty = 0
    for reference in references:
        if not isinstance(reference, dict):
            continue
        for key in content_keys:
            value = reference.get(key)
            if isinstance(value, str):
                counts[key] += 1
                if value.strip():
                    non_empty += 1
            elif isinstance(value, list):
                counts[key] += 1
                list_valued[key] += 1
                if any(isinstance(item, str) and item.strip() for item in value):
                    non_empty += 1
    out.update({
        "reference_count": len(references),
        "keys_seen": sorted({key for reference in references if isinstance(reference, dict)
                             for key in reference}),
        "content_key_counts": counts,
        "content_key_list_valued": list_valued,
        "references_with_nonempty_content": non_empty,
        # The reader app/grounding.py actually uses.
        "readable_by_the_shipped_gate": sum(
            1 for reference in references if isinstance(reference, dict)
            and isinstance(reference.get("content"), str) and reference["content"].strip()),
    })
    return out


def bucket_of(key_path: str) -> str:
    """Which part of the response a string came from: evidence, answer, or somewhere else.

    The split matters. A code found only in the answer string is the model repeating the question
    back; a code found in a reference string is evidence that the chunk text reached the gateway.
    """
    segments = [part.split("[")[0] for part in key_path.lstrip(".").split(".")]
    if any(segment in ("references", "citations") for segment in segments[:3]):
        return "evidence"
    if any(segment in ("response", "answer") for segment in segments[:3]):
        return "answer"
    return "other"


def membership_report(response: Any, asked_codes: list[str]) -> dict[str, Any]:
    """Does each asked code appear as a standalone token, in evidence strings vs answer strings?

    Uses the same token rule as app/grounding.verbatim_support, so a hit is exactly what the M1 gate
    would have counted as support if the text had been where the gate reads it.
    """
    strings = walk_strings(response)
    by_bucket: dict[str, list[str]] = defaultdict(list)
    for key_path, text in strings:
        by_bucket[bucket_of(key_path)].append(text)
    hits = {code: {bucket: sum(1 for text in texts if grounding.verbatim_support(code, text))
                   for bucket, texts in by_bucket.items()}
            for code in asked_codes}
    return {"asked_codes": asked_codes,
            "strings_scanned": len(strings),
            "strings_by_bucket": {bucket: len(texts) for bucket, texts in by_bucket.items()},
            "asked_code_token_hits_by_bucket": hits,
            "evidence_reached_the_gateway": any(facts.get("evidence", 0) > 0 for facts in hits.values())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 reference-shape probe (run on the stack)")
    parser.add_argument("--gateway-url", required=True, help="e.g. http://localhost:8000")
    parser.add_argument("--api-key-env", default="GATEWAY_API_KEY")
    parser.add_argument("--raw-lightrag-url", default=None,
                        help="also POST /query straight to LightRAG with the gateway's flags")
    parser.add_argument("--lightrag-api-key-env", default="LIGHTRAG_API_KEY")
    parser.add_argument("--probe-root", type=Path, default=DEFAULT_PROBE_ROOT)
    parser.add_argument("--company", required=True)
    parser.add_argument("--probe-id", required=True, help="row id from the generated probe manifest")
    parser.add_argument("--mode", default=None, help="retrieval mode for the raw LightRAG call")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--chunk-top-k", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    manifest_path = args.probe_root / args.company / "manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: {manifest_path} not found", file=sys.stderr)
        return 2
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next((probe for bucket in document["probes"].values()
                for probe in bucket if probe["id"] == args.probe_id), None)
    if row is None:
        print(f"ERROR: probe id {args.probe_id!r} not in {manifest_path}", file=sys.stderr)
        return 2
    question = row["question"]
    asked = row.get("expected_gate_asked") or grounding.identifiers(question)

    print(f"probe {args.probe_id}  company={args.company}  asked={asked}  "
          f"expected_gate={row.get('expected_gate')}  expected_document={row.get('expected_document')}")

    gateway_key = os.environ.get(args.api_key_env)
    status, response = post(args.gateway_url,
                            {"query": question, "company": args.company},
                            gateway_key, args.timeout)
    print(f"\n== gateway POST {args.gateway_url}/query == HTTP {status}")
    print("  response shape:")
    print(describe(response))
    print("  references:")
    print(json.dumps(reference_report(response), indent=2, sort_keys=True))
    print("  membership:")
    print(json.dumps(membership_report(response, asked), indent=2, sort_keys=True))

    if args.raw_lightrag_url:
        payload = {"query": question, "top_k": args.top_k, "chunk_top_k": args.chunk_top_k,
                   "include_references": True, "include_chunk_content": True}
        if args.mode:
            payload["mode"] = args.mode
        status, raw = post(args.raw_lightrag_url, payload,
                           os.environ.get(args.lightrag_api_key_env), args.timeout)
        print(f"\n== LightRAG POST {args.raw_lightrag_url}/query == HTTP {status}")
        print("  payload sent (keys only): " + ", ".join(sorted(payload)))
        print("  response shape:")
        print(describe(raw))
        print("  references:")
        print(json.dumps(reference_report(raw), indent=2, sort_keys=True))
        print("  membership:")
        print(json.dumps(membership_report(raw, asked), indent=2, sort_keys=True))
    else:
        print("\n(no --raw-lightrag-url: the upstream shape question is still open)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
