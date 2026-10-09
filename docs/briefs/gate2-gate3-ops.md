# Brief: Gate 2 (quality run) and Gate 3 (ops readiness), see docs/ops/release-plan-first-use.md

To: ops-hermes@thinkpad-ops. Do the parts IN ORDER, one at a time. Production stays untouched except where stated.
Never call `pipeline_status`. Data stays on the Thinkpad; reply to me with counts/paths, no document text.

## Part A (Gate 2): run the probe set through the production path
Input: ~/rag-project/probe/probe-set-v1.jsonl (36 probes). Production gateway as OpenWebUI uses it (the Ollama-compatible
bridge, current image local/rag-gateway:p23, flags unchanged). Reuse your c21 harness where it fits.
1. Run all 36 probes, 3 independent draws (same config, no restarts between). Follow-up probes must send the preceding
   question and its answer from the SAME draw as history. Save raw outputs to ~/rag-project/probe/runs/rc1-draw{1,2,3}.jsonl
   (probe id, answer text, latency, HTTP status, empty flag, `empty_retries_used`, rewrite/keyword fallback enums from logs).
2. Score each answer with automatic checks only (no outside models): (a) non-empty, (b) key facts from `expected_answer`
   present (script the check, e.g. key numbers/names/terms from expected_answer and evidence), (c) for `unanswerable` probes,
   answer states it is not in the documents rather than inventing. If a local judge model is used, name it and say so.
3. Write ~/rag-project/probe/runs/rc1-quality-report.md and copy to docs/ops/runs/rc1-quality-report.md in the repo checkout
   (do not commit; I will). Include: per-type pass rate (mean and min/max across the 3 draws), empty/5xx/refusal counts,
   fallback counts, latency p50/p95, list of probe ids that failed in >=2 draws (id + one-line reason, no doc text), and
   draw-to-draw disagreement count. State plainly that all probes are GSK and mostly txt.
Done when the report exists and the three raw files have 36 rows each.

## Part B (Gate 3.2): backup and restore drill (after Part A finishes; do not run during Part A)
1. Back up `lightrag_store/` and `state/` (compose stack may stay up; if a consistent copy needs a stop, STOP and ask me).
   Prefer a read-consistent method (SQLite `.backup` for the state DB; for lightrag_store copy while quiescent).
   Destination: ~/rag-project/backups/rc1-<timestamp>/ with a checksum manifest.
2. Restore into a scratch directory (NOT the live paths) and verify: manifest checksums match; the state DB opens and
   row counts equal the live DB; lightrag_store file list/sizes equal the live ones. Do not point any container at the scratch copy.
3. Write the exact backup and restore commands you used, size, and duration to docs/ops/runs/rc1-backup-restore.md (do not commit).
Done when the restore verification passes or you report precisely what failed.

## Part C (Gate 3.4): cleanup
`docker rm -f rag-gateway-p22-S0 rag-gateway-p22-S1` if still present (these are test containers only; confirm names first,
touch nothing else). Report `docker ps` names before and after.

## Not in this brief
Reboot recovery drill and oMLX memory policy: operator-gated, I will ask separately.
## Reply
`rig send orch-lead@graphraggle` after Part A with the report path and headline numbers; again after B and C.
