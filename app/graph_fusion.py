from __future__ import annotations

from typing import Any


def _citation_key(citation: dict[str, Any]) -> str:
    for key in ("path", "file_path", "file_source", "source"):
        value = citation.get(key)
        if isinstance(value, str) and value.strip():
            return f"path:{value.strip().lower()}"
    content = citation.get("content")
    if isinstance(content, str) and content.strip():
        return f"content:{content.strip()[:200].lower()}"
    if isinstance(content, list):
        joined = " ".join(item.strip() for item in content if isinstance(item, str) and item.strip())
        if joined:
            return f"content:{joined[:200].lower()}"
    return f"id:{id(citation)}"


def fuse_graph_and_vector_evidence(
    citations: list[dict[str, Any]],
    graph_evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    fused: list[dict[str, Any]] = []
    seen: set[str] = set()

    for citation in citations:
        key = _citation_key(citation)
        if key in seen:
            continue
        seen.add(key)
        fused.append({"kind": "vector", "evidence": citation, "provenance": [citation.get("path") or citation.get("file_path")]})

    for item in graph_evidence:
        provenance = item.get("provenance") or []
        prov_keys = {f"path:{p.strip().lower()}" for p in provenance if isinstance(p, str) and p.strip()}
        if prov_keys and prov_keys.issubset(seen):
            continue
        fused.append({"kind": "graph", "evidence": item, "provenance": provenance})
        seen.update(prov_keys)

    return fused
