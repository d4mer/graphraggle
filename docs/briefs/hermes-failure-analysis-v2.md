# Brief - failure analysis of the V2 baseline (loop4). Files only; NO LightRAG/gateway/sigma/oMLX calls.

Context: ~/rag-project/probe/loop4/V2-baseline/{answers.jsonl,verdicts.jsonl,summary.json} hold the run on probe set v2
(solvay-dev-v2.jsonl, solvay-heldout-v2.jsonl in ~/rag-project/probe/). Result: dev 82.5%, held-out 85.0% (target 90), hallucination 5.0% (target <5),
refusal accuracy 66.7% (target 80). Nothing else is running; the store holds the 6 transcripts built by this run, so you may read
~/rag-project/lightrag_store read-only (copy files you need to /tmp first). Source transcripts: ~/rag-project/staging/Solvay (or source_docs).

## Task (you are the only agent allowed to read answers; no question/answer/transcript text in messages to the lead)
1. For every DEV and HELD-OUT probe whose verdict is not correct/refused_ok, read question, expected_answer, evidence, system answer, judge verdict+reason, and the transcript text.
2. Classify each into exactly one cause: A judge_wrong; B probe_bad; C not_in_graph (evidence phrase absent from stored chunks in kv_store_text_chunks.json);
   D retrieval_miss (evidence in a stored chunk but answer lacks it or refused although answerable); E synthesis_wrong (material retrieved but mis-combined/dropped/contradicted);
   F other (one-line why). Tag also: garbled_ASR_term_involved yes/no.
3. Special section on REFUSAL and HALLUCINATION: for each unanswerable probe the system answered (instead of refusing), and each hallucinated verdict, say whether the answer
   invented a fact, borrowed a neighbouring real fact, or the probe was actually answerable (probe_bad). Counts only to the lead.
4. Judge reliability: number of failures you class A; and agreement on 5 random CORRECT verdicts (x/5) plus 3 random refused_ok verdicts (x/3).
5. Question TYPES that fail most (single-fact / multi-fact / follow-up / combine / unanswerable).
6. Propose up to 5 concrete, generic fixes ranked by expected gain (prompt/retrieval/chunking/refusal-threshold/probe edits), each tied to the count of failures it would address. No transcript content.
7. Write ONE heredoc report (not write_file) to ~/rag-project/docs/ops/runs/failure-analysis-v2.md; per-question detail to ~/rag-project/probe/loop4/failure-detail.jsonl (stays on Thinkpad).
8. Message the lead only: file paths, counts per cause (dev, held-out), refusal/hallucination breakdown, judge agreement, failing types, proposed fixes.

Constraints: read-only except report, detail file, /tmp copies; no docker, rm/mv/chmod/chown; unlisted approval dialogs: do not accept, report.
