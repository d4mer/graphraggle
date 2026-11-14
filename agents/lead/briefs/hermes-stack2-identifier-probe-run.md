# Hermes brief — stack2 run of the identifier-bearing answerable probes (M1 guard test), 2026-11-14

Same setup as your STACK2 M1 GUARD RUN of today, different probe set. STACK2 ONLY, production untouched (docker-inspect + .env-mtime snapshot start vs end, diff empty). Synthetic data, you may read it.
Probes: ~/briefs/probes-id/{Northwind Chemicals,Contoso Pharma}/manifest.json (+ edge-variants.json, RUN-NOTES.md) copied from the lead. 114 rows (57 per company): 39 identifier_answerable, edge variants,
two-code rows, 6 control_unanswerable per company. Every row has `expected_gate` (fire / not_fire / no_identifier) and an `id` (idn-...).

## Setup (identical to today's guard run, then restore)
Stack2 baseline pair unchanged (lightrag v1.5.7 unpatched, RELATED_CHUNK_NUMBER=1, RERANK_TIMEOUT=600, COMPANY_AWARE_DEDUPE OFF, worker2 stopped, no ingest). Gateway2 on
local/rag-gateway:structure-cutover-3 (3b3914a7b5fa), GROUNDING_CHECK_ASKED_IDENTIFIER=true, fresh empty QUERY_LOG_PATH. Back up .env/compose.yml, snapshot store + state DB (sha256), clear
llm_response_cache (back it up first). Recreate with `compose up -d --no-deps --force-recreate`.

## Run
Your runner (probe_baseline.py), gateway :8021, mode=mix, sequential, 5 s pause, 900 s guard, stop at first 5xx. Point it at the two probe manifests (all buckets; the answerable rows are in `scoped`, controls in
`unanswerable`). Record per row: label (your vocabulary mapped to the scorer's as before), `request_id`, `company`, and the probe `id`. Then run the scorer at tag structure-cutover-3:
`python scripts/score_grounding_run.py --manifest <NW manifest.json> --manifest <CP manifest.json> --results <results.jsonl> --log <query-log capture>`.

## Report (counts only)
1. The scorer's full report.
2. THE ACCEPTANCE TABLE (this is what the lead needs): for every row where the gate FIRED, say whether its id is in that company's `expected_fire_ids` (edge-variants.json). Report: fired and expected
   (by design), fired and NOT expected (a genuine false refusal: list the ids, I will look at them), expected-to-fire but did not fire (list ids), and `no_identifier` rows (unmeasured, list count).
   Note that `would_newly_refuse_answerable` will read about 10 by design; do not treat that as the result.
3. For each fired-and-not-expected row: the gate fields (`grounding_asked`, `grounding_unsupported`, `grounding_evidence_pass`, `grounding_references_without_content`) and, counts only, whether the asked code
   appears in that row's returned references (yes/no). Do not paste reference text.
4. Latency p50/p95, 5xx and rerank-fallback counts (gateway + LightRAG logs since marks), label tallies by probe_kind.
5. Copy results.jsonl, query-log capture and scorer output to ~/briefs/out/ with an `idn` prefix.

## Teardown
Restore .env/compose.yml (flag + QUERY_LOG_PATH removed, gateway image back to :structure-cutover-2), stop stack2, prove store/state DB unchanged (170/1735/2298, 32 state rows).

## Rules
One bare command per approval; stop on first failure and report; do not retry a denied approval. Report in structure-cutover.md section "STACK2 IDENTIFIER PROBE RUN 2026-11-14", STATUS line 1.
