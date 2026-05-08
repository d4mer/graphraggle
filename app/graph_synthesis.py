from __future__ import annotations

from typing import Any


def build_graph_aware_prompt(query: str, combined_evidence: list[dict[str, Any]]) -> str:
    lines = [
        "Answer the user query using the evidence below.",
        "Use graph evidence for relationships and vector evidence for direct support.",
        "If the evidence is insufficient, say so clearly.",
        "",
        f"USER QUERY:\n{query}",
        "",
        "EVIDENCE:",
    ]
    for item in combined_evidence[:12]:
        kind = item.get("kind", "unknown")
        evidence = item.get("evidence", {}) or {}
        if kind == "graph":
            lines.append(
                f"[GRAPH] seed={evidence.get('seed_label')} nodes={evidence.get('node_count')} edges={evidence.get('edge_count')} labels={evidence.get('labels')} provenance={evidence.get('provenance')}"
            )
        else:
            lines.append(
                f"[VECTOR] path={evidence.get('path') or evidence.get('file_path')} content={str(evidence.get('content', ''))[:600]}"
            )
    lines.append("")
    lines.append("Return a concise grounded answer.")
    return "\n".join(lines)


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
