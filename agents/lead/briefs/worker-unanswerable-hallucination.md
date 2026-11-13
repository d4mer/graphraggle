# Worker brief — unanswerable-question hallucination (8 of 12 on the GL-08 baseline), investigation first

Base: branch from current `origin/master` (check `0 0` first), own branch/worktree, no push. You do NOT read corpus or LightRAG content; you may read
`scripts/`, `app/`, tests, and the synthetic generator `scripts/make_synthetic_company.py`. Start only after the ingest-fixes brief is handed back.

## What was measured
Hermes ran 55 synthetic probes through the gateway (mode=mix). On the 12 unanswerable probes (a planted fact code that exists in no document) the system REFUSED 4 and
HALLUCINATED 8: the answer asserted an outcome for a code that appears nowhere. Scoped (27/28) and cross-company (12/15) recall were fine. The real-corpus refusal
rate has been high (>=80%, 100% on the v3 held-out), so synthetic unanswerables may be harder (fact-code lookalikes) than the real set; do not assume either.

## Task (report, then propose; no behaviour change without my go)
1. Find where refusal/grounding is decided today: the query path in the gateway and the LightRAG prompt/config it sets (system prompt, `only_need_context`, citations,
   any "no relevant context" handling). Describe the actual flow in a short note with file:line references.
2. Using only synthetic fixtures, reproduce the failure shape: question about an invented code absent from the corpus while near-miss codes exist. State which stage lets it
   through (retrieval returns near-miss chunks and the LLM bridges the gap, vs. a prompt that does not demand verbatim support).
3. Propose 2-3 candidate mitigations ranked by cost and risk (e.g. require the asked-for identifier verbatim in the evidence before answering; a grounding check on the
   answer; prompt wording) with how each would be measured on this probe set and on the real held-out sets without regressing recall.
Hand back a report + any test-only harness; the baseline pair for comparison is Hermes' stack2 run (counts 4 refused / 8 hallucinated of 12).
