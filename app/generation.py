from .lightrag_client import client


PROMPTS = {
    "summary": "Create a concise grounded summary from the retrieved context.",
    "memo": "Draft a professional memo grounded only in the retrieved context.",
    "report": "Draft a structured report grounded only in the retrieved context.",
    "proposal": "Draft a proposal grounded only in the retrieved context.",
    "policy": "Draft a policy document grounded only in the retrieved context.",
    "brief": "Draft a concise brief grounded only in the retrieved context.",
    "draft": "Draft a document grounded only in the retrieved context.",
}


async def generate_document(query: str, document_type: str) -> dict:
    retrieval = await client.post_json(
        "/query",
        {
            "query": query,
            "mode": "hybrid",
            "include_references": True,
            "include_chunk_content": True,
        },
    )
    references = retrieval.get("references", [])
    context_parts: list[str] = []
    for ref in references:
        file_path = ref.get("file_path", "")
        content = ref.get("content") or []
        joined = "\n\n".join(content)
        if joined:
            context_parts.append(f"Source: {file_path}\n{joined}")
    context = "\n\n".join(context_parts) if context_parts else retrieval.get("response", "")
    prompt = (
        f"{PROMPTS[document_type]}\n\n"
        f"User request:\n{query}\n\n"
        f"Context:\n{context}\n\n"
        "Return only the requested document."
    )
    llm = await client.post_json(
        "/query",
        {
            "query": prompt,
            "mode": "bypass",
            "include_references": False,
        },
    )
    return {
        "document_type": document_type,
        "title": document_type.title(),
        "document": llm.get("response", ""),
        "citations": references,
    }
