from __future__ import annotations

from typing import Any
from urllib.parse import quote
import re


def tokenize_graph_query(query: str) -> list[str]:
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", query.lower()) if len(t) > 3]
    seen: set[str] = set()
    result: list[str] = []
    for term in terms:
        if term not in seen:
            seen.add(term)
            result.append(term)
    return result[:6]


def select_graph_seed_labels(query: str, label_candidates: list[str], max_seeds: int = 3) -> list[str]:
    query_terms = tokenize_graph_query(query)

    def rank(label: str) -> tuple[int, int, str]:
        lowered = label.lower()
        overlap = sum(1 for term in query_terms if term in lowered)
        return (-overlap, len(lowered), lowered)

    ordered = sorted({label.strip() for label in label_candidates if isinstance(label, str) and label.strip()}, key=rank)
    return ordered[:max_seeds]


def _extract_provenance(values: Any) -> list[str]:
    found: list[str] = []

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in {"source_id", "file_path", "source", "path"} and isinstance(value, str) and value.strip():
                    found.append(value.strip())
                else:
                    walk(value)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(values)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in found:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def normalize_graph_result(seed_label: str, payload: dict[str, Any]) -> dict[str, Any]:
    nodes = payload.get("nodes") if isinstance(payload.get("nodes"), list) else []
    edges = payload.get("edges") if isinstance(payload.get("edges"), list) else []
    labels = []
    for node in nodes[:10]:
        if isinstance(node, dict):
            label = node.get("label") or node.get("id") or node.get("name")
            if isinstance(label, str) and label.strip():
                labels.append(label.strip())
    return {
        "seed_label": seed_label,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "labels": labels[:10],
        "provenance": _extract_provenance(payload),
        "raw": payload,
    }


async def fetch_graph_native_evidence(
    client: Any,
    query: str,
    *,
    max_seeds: int = 3,
    max_depth: int = 2,
    max_nodes: int = 50,
) -> tuple[list[dict[str, Any]], str | None, list[str]]:
    try:
        terms = tokenize_graph_query(query)
        if not terms:
            return [], None, []

        label_candidates: list[str] = []
        for term in terms:
            data = await client.get(f"/graph/label/search?q={quote(term)}&limit=3")
            if isinstance(data, list):
                label_candidates.extend([item for item in data if isinstance(item, str)])

        seed_labels = select_graph_seed_labels(query, label_candidates, max_seeds=max_seeds)
        if not seed_labels:
            return [], None, []

        evidence: list[dict[str, Any]] = []
        for label in seed_labels:
            payload = await client.get(
                f"/graphs?label={quote(label)}&max_depth={max_depth}&max_nodes={max_nodes}"
            )
            if isinstance(payload, dict):
                evidence.append(normalize_graph_result(label, payload))

        return evidence, None, seed_labels
    except Exception as exc:
        return [], str(exc), []


def build_graph_native_metadata(
    *,
    enabled: bool,
    applied: bool,
    error: str | None,
    seed_labels: list[str],
    result_count: int,
) -> dict[str, Any]:
    return {
        "graph_native_enabled": enabled,
        "graph_native_applied": applied,
        "graph_native_error": error,
        "graph_native_seed_labels": seed_labels,
        "graph_native_result_count": result_count,
    }
