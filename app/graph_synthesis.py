from __future__ import annotations

from typing import Any


WEAK_GRAPH_SYNTHESIS_MARKERS = (
    "no relevant context found",
    "not enough information",
    "do not have enough information",
)

MAX_SYNTHESIS_EVIDENCE_ITEMS = 3
MAX_VECTOR_CONTENT_CHARS = 140
MAX_GRAPH_LABELS = 4


def build_graph_aware_prompt(query: str, combined_evidence: list[dict[str, Any]]) -> str:
    lines = [
        "Answer the user query using only the evidence below.",
        "Use graph evidence for relationships and vector evidence for direct support.",
        "If the evidence is insufficient, say so clearly.",
        "Keep the answer under 60 words and focus only on the key relationship facts.",
        "",
        f"USER QUERY:\n{query}",
        "",
        "EVIDENCE:",
    ]
    for item in combined_evidence[:MAX_SYNTHESIS_EVIDENCE_ITEMS]:
        kind = item.get("kind", "unknown")
        evidence = item.get("evidence", {}) or {}
        if kind == "graph":
            labels = evidence.get("labels") if isinstance(evidence.get("labels"), list) else []
            lines.append(
                f"[GRAPH] seed={evidence.get('seed_label')} nodes={evidence.get('node_count')} edges={evidence.get('edge_count')} labels={labels[:MAX_GRAPH_LABELS]}"
            )
        else:
            lines.append(
                f"[VECTOR] path={evidence.get('path') or evidence.get('file_path')} content={str(evidence.get('content', ''))[:MAX_VECTOR_CONTENT_CHARS]}"
            )
    lines.append("")
    lines.append("Return a concise grounded answer.")
    return "\n".join(lines)


def is_graph_synthesis_answer_usable(answer: str | None) -> bool:
    if not isinstance(answer, str):
        return False
    normalized = answer.strip()
    if not normalized:
        return False
    lowered = normalized.lower()
    return not any(marker in lowered for marker in WEAK_GRAPH_SYNTHESIS_MARKERS)


async def synthesize_graph_aware_answer(client: Any, query: str, combined_evidence: list[dict[str, Any]]) -> tuple[str | None, str | None]:
    prompt = build_graph_aware_prompt(query, combined_evidence)
    try:
        result = await client.post_json(
            "/query",
            {
                "query": prompt,
                "mode": "bypass",
                "top_k": 1,
                "include_references": False,
                "include_chunk_content": False,
            },
        )
    except Exception as exc:
        return None, str(exc)

    answer = result.get("response")
    if isinstance(answer, str) and answer.strip():
        return answer.strip(), None
    return None, "empty_graph_synthesis_response"
