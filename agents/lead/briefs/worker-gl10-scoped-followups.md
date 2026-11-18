# Worker brief: two residuals in the GL-10 scoped pass (you reported both)

Both matter for real Open WebUI use. Code-only, offline, strict fakes with real shapes. Branch from current master (structure-cutover-8, 5642ca9).

1. conversation_history: the scoped generation must carry the same conversation_history the normal /query path would (Open WebUI follow-ups: "and what about their second contract?" must resolve against the earlier turn). Check the retrieval leg already uses it; make the generation leg use it too, bounded the same way the normal path bounds it. Test: a follow-up request carries the history into the generation payload; a single-turn request is unchanged byte for byte.
2. A 4xx/5xx or timeout on the scoped GENERATION call must not surface as a 500. Degrade like the retrieval legs do: log scoped_pass_generation_error (class and status only, no text), fall back to the existing citation-enforcement path on the normal /query result, and never return other-company text. If even the fallback fails, return the honest decline message. Test each branch.

Also report (no change needed unless you find a fault): with COMPANY_SCOPE_ENFORCEMENT=retrieval and a question that names NO company, confirm the pass never runs and the request is byte-identical to today.

Hand back with test counts. Acceptance is the suite plus these tests; Hermes will not re-run for this row alone.
