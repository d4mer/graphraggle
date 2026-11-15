# Hermes brief — stack2 re-run of the 114 identifier probes with the FIXED gate evidence reader (tag structure-cutover-4)

SEND ONLY AFTER the GL-13/GL-13b scored production run has reported (same sigma model; do not run both at once).

Why: your 2nd reference-shape probe showed LightRAG returns chunk text at references[].content as a LIST of strings and that the shipped gate read only strings. The worker fixed the reader (commit 51e3c39,
tag structure-cutover-4, bundle ~/briefs/structure-cutover-4.bundle, sha256 in the lead's message; verify it and `git bundle verify`; build from the tag only). Gate still flag-only. This re-run is the first
true measurement of false refusals on identifier-bearing answerable probes. STACK2 ONLY, production untouched (docker-inspect + .env-mtime snapshot start/end, diff empty).

## Do (same as your STACK2 IDENTIFIER PROBE RUN 2026-11-14, with one change)
1. Build local/rag-gateway:structure-cutover-4 from the tag (additive; keep -3 for rollback). Stack2 baseline pair unchanged (unpatched LightRAG, R=1, RT=600, dedupe OFF, worker2 stopped, no ingest).
   gateway2 on -4 with GROUNDING_CHECK_ASKED_IDENTIFIER=true and a fresh empty QUERY_LOG_PATH; clear llm_response_cache (back it up first); recreate with --force-recreate.
2. Same 114 probes (~/briefs/probes-id), same runner and labels, request_id + company + probe id per row, 900 s guard. New this time: the query log carries `grounding_references_with_content` and
   `grounding_references_content_shapes` (string/list/empty/absent counts). Report their totals over the 114 rows so we can see the evidence reached the gate.
3. Score with scripts/score_grounding_run.py at tag -4, then report the ACCEPTANCE TABLE exactly as before: fired-and-expected, fired-and-NOT-expected (ids), expected-but-did-not-fire (ids), no_identifier count,
   would_newly_refuse_answerable, hallucinations caught/missed, and for every fired-but-unexpected row: grounding_asked, grounding_unsupported, evidence_reference_count, references_with_content, and whether the asked code appears in
   that row's returned references (yes/no, from the log/shape counts only, no reference text). Offline prediction with visible evidence: about 10 refusals, all expected (6 hyphen-as-space + 4 one-absent two-code); the
   worker's model is a prediction, not a measurement.
4. Latency p50/p95, 5xx, rerank-fallback counts. Copy results.jsonl, query log, scorer output to ~/briefs/out/ with prefix `idn2`.
5. Teardown as before: restore .env/compose (gateway image back to -2), stop stack2, store/state DB unchanged (170/1735/2298, 32 rows), production diff empty.

## Rules
One bare command per approval; stop on first failure; do not retry a denied approval; do not message the lead mid-turn. Report in structure-cutover.md section "STACK2 IDENTIFIER RERUN (FIXED GATE) 2026-11-15", STATUS line 1.
