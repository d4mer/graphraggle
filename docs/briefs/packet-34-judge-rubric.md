# Packet 34 - overnight_loop judge rubric v2 (code only; you do NOT need any transcript data for this)

Hermes' failure analysis of 165 graded answers found that 2 of 9 "failures" were the JUDGE being wrong (a correct refusal graded "wrong"; a clean refusal graded "hallucinated" because a real-but-adjacent entity was named),
and 4 were bad probes. Fix the judge prompt (constant JUDGE_PROMPT in scripts/overnight_loop.py, plus anything that builds it) and add tests. Worktree: `git worktree add ../graphraggle-p34 -b packet-34-judge master`.

New rules the prompt must state explicitly (keep the JSON-only output contract and the six verdict words):
1. For questions whose expected_answer is "not in the documents": a clear statement that the information is not available/not found in the provided context = refused_ok EVEN IF the answer names real entities or mentions nearby facts, as long as it does not assert the requested
   fact itself. Verdict hallucinated ONLY if the answer asserts a specific value/name/date/decision that answers the question and is not supported by the evidence/expected_answer.
2. For answerable questions: accept paraphrases, different wording, extra correct detail and different ordering. "correct" if all key facts in expected_answer are present and nothing contradicts the evidence. "partial" if some key facts are present and none contradict. "wrong" if key facts are missing/contradicted.
   A refusal ("not in the documents") on an answerable question is refused_wrong, unless the question itself is unanswerable from the evidence (then refused_ok).
3. Speaker attribution: do not mark an answer wrong merely because it attributes a statement to a different speaker label or omits who said it, unless the question explicitly asks who said it.
4. Judge only against the evidence quote and expected_answer given; never against outside knowledge. Extra detail not in the evidence is not an error unless it contradicts the evidence.
5. The reason field must be one sentence and must name which rule decided it (e.g. "rule1 clean refusal").
Implementation notes: keep prompt size modest (the judge is a reasoning model; JUDGE_MAX_TOKENS stays 4000); keep format placeholders working (question, expected_answer, evidence, answer).
Tests: assert the prompt contains each rule's key phrase; the parse/format round-trip still works with braces in the content (use str.format-safe escaping already used); existing judge tests pass; full suite passes.
Report: commit hash, test count. No data in the report.
