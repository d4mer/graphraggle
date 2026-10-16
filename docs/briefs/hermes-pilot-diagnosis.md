# Hermes brief: pilot diagnosis (read-only, content-free)

**Status: lead request, READ-ONLY. No ingest, no restart, no .env edit, stack2 left as is.
Content never leaves the host: counts, shapes with letters masked, numbers only.**
Report to `~/rag-project/docs/ops/runs/pilot-diagnosis.md`.

Why: the P1 arm logged `transcript_prep doc 1/2: skipped (no-recurring-speaker(2 labels))`,
so P1 was only applied to half the pilot, and the pilot corpus is ~16 chunks in total.

1. T1 (Cross-GBU) label shape: what do the 2 labels look like as masked shapes
   (e.g. `(Xxxx Xxxx)`, with/without trailing digits/time), how many lines carry each,
   and do they alternate? Run the `prepare_transcript` detector from
   ~/rag-project/scratch/chunkrun (stdlib, read-only, no network) on T1 and print only
   its result fields (applied, reason, turns, speakers, counts). Same for T2.
2. Chunk counts: from stack2 LightRAG (read-only GET), number of chunks per doc in the C0
   and (if still stored) P1 state. If only P1's store remains, say so.
3. Retrieval breadth: effective TOP_K, CHUNK_TOP_K, MAX_TOTAL_TOKENS, MAX_ENTITY_TOKENS,
   MAX_RELATION_TOKENS, rerank min score actually in force on stack2 (names and values).
   From the P1 and C0 `answers.jsonl` only: number of source/reference entries per answer
   (min/median/max) and number of distinct chunk ids cited across all 41 answers.
4. Per-question verdict transitions C0 -> P1 as counts only (correct->correct, wrong->correct,
   correct->wrong, ...), split by whether the question's source doc is T1, T2 or neither.
5. Answer length (chars) p50 for the two arms and the number of answers with a Sources block.
