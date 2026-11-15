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
      Three readers disagreed about this until 51e3c39: app/generation.py:29 read `content` as a LIST
      of strings and app/rerank.py:33-41 accepted content/text/chunk_text/body as string or list,
      while the M1 gate looked only at a string-valued `content`. If the upstream shape is the list
      form, the gate was blind because of our reader, not because LightRAG withheld anything.
      `readable_by_the_shipped_gate` deliberately still measures the OLD string-only reader, so this
      run is comparable with the 2026-11-15 one that reported n=0; read it next to
      `content_key_counts` / `content_key_list_valued` to see what the widened reader gains.

  Q2  Did the asked identifier reach the gateway in the first place? `asked_code_token_hits` counts
      the strings in the whole response in which the code appears as a standalone token (the same
      token rule app/grounding.verbatim_support uses). Zero hits with a non-empty context block
      would mean retrieval genuinely missed the piece; zero references-with-content plus zero hits
      means the text never left LightRAG.

Examples (stack2):

  # UNSCOPED (the default, and what the probe runner does): the probe id picks the manifest.
  python scripts/m1_reference_shape_probe.py \
    --gateway-url http://localhost:8000 --api-key-env GATEWAY_API_KEY \
    --probe-id idn-nc3617-s001

  # also ask LightRAG directly with the gateway's own retrieval flags. Stock lightrag v1.5.7 wants
  # `X-API-Key`, not `Authorization: Bearer`; --company stays available for a deliberate scoped run:
  python scripts/m1_reference_shape_probe.py \
    --gateway-url http://localhost:8000 --api-key-env GATEWAY_API_KEY \
    --raw-lightrag-url http://localhost:9621 --api-key-env LIGHTRAG_API_KEY \
    --lightrag-auth-header x-api-key \
    --company "Northwind Chemicals" --probe-id idn-nc3617-s001 --mode mix

Why --company is optional
-------------------------
The first stack run sent ``{"query": ..., "company": ...}`` to the gateway, and company scoping
returned ``data.citations n=0``: the probe asked a scoped question, got no references back, and so
learned nothing about the reference shape. The probe runner asks unscoped, so the default is now
unscoped too, and a scoped run is an explicit choice rather than an accident.

Why the auth header is selectable
---------------------------------
The raw LightRAG leg authenticates with ``Authorization: Bearer`` and stock lightrag v1.5.7 answered
HTTP 401; ``X-API-Key`` is the header that image accepts. The default stays ``bearer`` so the first
run's command keeps working verbatim; ``--lightrag-auth-header x-api-key`` is the one-flag fix.
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


def auth_headers(api_key: str | None, style: str = "bearer") -> dict[str, str]:
    """The credential header, in the style the receiving service expects.

    The gateway takes ``Authorization: Bearer``. Stock lightrag v1.5.7 answered 401 to that and
    accepts ``X-API-Key``. No default changes: ``bearer`` stays the default everywhere.
    """
    if not api_key:
        return {}
    if style == "x-api-key":
        return {"X-API-Key": api_key}
    return {"Authorization": f"Bearer {api_key}"}


def post(url: str, payload: dict[str, Any], api_key: str | None, timeout: int,
         auth_style: str = "bearer") -> tuple[int, Any]:
    request = urllib.request.Request(
        url.rstrip("/") + "/query",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **auth_headers(api_key, auth_style)},
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
        # The reader the gate used before 51e3c39, kept as-is so the second stack run is comparable
        # with the first. The widened reader is app/citation_text.citation_content; its gain on this
        # response is visible as (content_key_counts - readable_by_the_shipped_gate).
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


class ProbeLookupError(Exception):
    """Manifest lookup failure. Messages carry paths and ids only, never probe text."""


def find_probe(probe_root: Path, probe_id: str,
               company: str | None = None) -> tuple[Path, dict[str, Any]]:
    """The probe row and the manifest it came from.

    With ``company`` this is the single company manifest, as before. Without it - the default now -
    every ``<probe-root>/*/manifest.json`` is searched, because the probe id is what identifies the
    row and the query itself stays unscoped. An id present under two companies is an error rather
    than a coin flip: which question gets asked changes what the run measures.
    """
    candidates = ([probe_root / company / "manifest.json"] if company
                  else sorted(probe_root.glob("*/manifest.json")))
    if not candidates:
        raise ProbeLookupError(f"no manifests under {probe_root} (check --probe-root / --company)")
    matches: list[tuple[Path, dict[str, Any]]] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProbeLookupError(f"{path}: unreadable manifest ({type(exc).__name__})")
        for bucket in (document.get("probes") or {}).values():
            for probe in bucket:
                if isinstance(probe, dict) and probe.get("id") == probe_id:
                    matches.append((path, probe))
    if not matches:
        raise ProbeLookupError(
            f"probe id {probe_id!r} not found in {len(candidates)} manifest(s) under {probe_root}")
    if len(matches) > 1:
        owners = sorted({path.parent.name for path, _ in matches})
        if len(owners) > 1:
            raise ProbeLookupError(
                f"probe id {probe_id!r} appears in {len(matches)} company manifests "
                f"({', '.join(owners)}); pass --company to pick one")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="M1 reference-shape probe (run on the stack)")
    parser.add_argument("--gateway-url", required=True, help="e.g. http://localhost:8000")
    parser.add_argument("--api-key-env", default="GATEWAY_API_KEY")
    parser.add_argument("--raw-lightrag-url", default=None,
                        help="also POST /query straight to LightRAG with the gateway's flags")
    parser.add_argument("--lightrag-api-key-env", default="LIGHTRAG_API_KEY")
    parser.add_argument("--probe-root", type=Path, default=DEFAULT_PROBE_ROOT)
    parser.add_argument("--company", default=None,
                        help="scope the gateway query to one company and read that company's "
                             "manifest. Omit it to ask unscoped, the way the probe runner does - "
                             "scoping is what returned data.citations n=0 on the first stack run")
    parser.add_argument("--probe-id", required=True, help="row id from the generated probe manifest")
    parser.add_argument("--lightrag-auth-header", choices=("bearer", "x-api-key"), default="bearer",
                        help="credential header for the raw LightRAG leg. Stock lightrag v1.5.7 "
                             "answers 401 to 'Authorization: Bearer' and accepts X-API-Key; the "
                             "default stays bearer so earlier commands still run")
    parser.add_argument("--mode", default=None, help="retrieval mode for the raw LightRAG call")
    parser.add_argument("--top-k", type=int, default=12)
    parser.add_argument("--chunk-top-k", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)

    try:
        manifest_path, row = find_probe(args.probe_root, args.probe_id, args.company)
    except ProbeLookupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    question = row["question"]
    asked = row.get("expected_gate_asked") or grounding.identifiers(question)

    print(f"probe {args.probe_id}  company={args.company or 'UNSCOPED'}  asked={asked}  "
          f"expected_gate={row.get('expected_gate')}  expected_document={row.get('expected_document')}")
    print(f"manifest: {manifest_path}")

    gateway_key = os.environ.get(args.api_key_env)
    gateway_body: dict[str, Any] = {"query": question}
    if args.company:
        gateway_body["company"] = args.company
    status, response = post(args.gateway_url, gateway_body,
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
                           os.environ.get(args.lightrag_api_key_env), args.timeout,
                           auth_style=args.lightrag_auth_header)
        print(f"\n== LightRAG POST {args.raw_lightrag_url}/query == HTTP {status} "
              f"(auth header: {args.lightrag_auth_header})")
        if status == 401:
            other = "x-api-key" if args.lightrag_auth_header == "bearer" else "bearer"
            print(f"  HTTP 401: stock lightrag v1.5.7 takes X-API-Key, not Bearer. "
                  f"Re-run with --lightrag-auth-header {other}.")
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
